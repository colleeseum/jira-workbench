"""Ask Claude or Codex about the locally synced data, from the web UI.

Both CLIs are invoked as subprocesses of the already-logged-in local user --
jira-wb never reads, stores, or passes either engine's own credentials;
`claude`/`codex` each transparently reuse their own already-persisted local
login (`~/.claude/`, `~/.codex/`). Read-only by construction: Claude via an
explicit --tools allow-list (no Edit/Write/Bash), Codex via --sandbox
read-only (OS-enforced, a stronger guarantee than an omitted-tool
allow-list). See the "Ask Claude/Codex about your local data" plan for the
full design rationale and the real CLI behavior this was verified against.

Multi-turn: each engine's own conversation/session id (returned in its
first response) can be passed back in as `resume_id` to continue the same
conversation with full prior context -- verified for real against both
CLIs, not assumed from their docs.

Streaming: both CLIs' own line-delimited JSON event stream (Claude's
`--output-format stream-json --verbose`, Codex's `--json`) is read
incrementally via Popen -- see the "Show live progress on the Assistant
page" plan -- so a caller can show what the agent is doing (which file
it's reading, which command it's running) while it works, not just the
final answer once it's done.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ENGINES = ("claude", "codex")
DEFAULT_TIMEOUT_SECONDS = 180
# A generous but bounded cap -- this becomes one argv element (or the body
# of a resumed turn), never shell-interpreted, so this isn't a safety
# measure against injection (argv-list subprocess calls have no shell to
# inject into) -- just a sane ceiling against an accidentally enormous
# paste.
MAX_QUESTION_LENGTH = 4000

# Prepended only to a *fresh* conversation's first question -- a resumed
# turn's engine session already has this context from its own first turn.
CONTEXT_PREFIX = (
    "You are analyzing a local, read-only mirror of Jira and Freshdesk data synced by jira-wb, "
    "rooted at the current directory. Jira issues live under "
    "components/<component-slug>/<ISSUE-KEY>/issue.json (a raw Jira REST issue object) and the "
    "sibling comments.json. Freshdesk tickets live under freshdesk/tickets/<numeric-id>/ticket.json "
    "and the sibling conversations.json. This data is read-only -- you have no write access here "
    "and should not attempt to create, edit, or delete anything.\n\n"
)

# Duck-typed like subprocess.Popen: needs .stdout (iterable of lines),
# .stderr (readable once), .wait(), .returncode, .kill(). Not literally
# typed as subprocess.Popen so a test fake doesn't have to subclass it.
PopenFactory = Callable[..., Any]
OnStep = Callable[[str], None]

_NO_OP_STEP: OnStep = lambda step: None  # noqa: E731


class AssistantResult:
    __slots__ = ("engine", "answer", "error", "session_id", "duration_seconds")

    def __init__(
        self, *, engine: str, answer: str, error: str | None, session_id: str | None, duration_seconds: float
    ) -> None:
        self.engine = engine
        self.answer = answer
        self.error = error
        self.session_id = session_id
        self.duration_seconds = duration_seconds

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AssistantResult):
            return NotImplemented
        return (
            self.engine == other.engine
            and self.answer == other.answer
            and self.error == other.error
            and self.session_id == other.session_id
        )

    def __repr__(self) -> str:
        return (
            f"AssistantResult(engine={self.engine!r}, answer={self.answer!r}, error={self.error!r}, "
            f"session_id={self.session_id!r}, duration_seconds={self.duration_seconds!r})"
        )


def _error_result(engine: str, error: str, *, duration_seconds: float = 0.0) -> AssistantResult:
    return AssistantResult(engine=engine, answer="", error=error, session_id=None, duration_seconds=duration_seconds)


def ask(
    engine: str,
    jira_dir: Path,
    question: str,
    *,
    resume_id: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    popen: PopenFactory = subprocess.Popen,
) -> AssistantResult:
    """ask_streaming with intermediate steps discarded -- for a caller that
    only wants the final answer."""
    return ask_streaming(engine, jira_dir, question, resume_id=resume_id, timeout=timeout, on_step=_NO_OP_STEP, popen=popen)


def ask_streaming(
    engine: str,
    jira_dir: Path,
    question: str,
    *,
    resume_id: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    on_step: OnStep = _NO_OP_STEP,
    popen: PopenFactory = subprocess.Popen,
) -> AssistantResult:
    """Asks `engine` (see ENGINES) a question, scoped to `jira_dir`, calling
    `on_step(<short human-readable string>)` for each recognized event as
    the engine's own CLI streams them, before returning the final result.

    A web request should never 500 just because the user picked a CLI
    that isn't installed on this machine, asked something that took too
    long, or typed something invalid -- every failure mode here comes
    back as a normal AssistantResult with `.error` set, never a raised
    exception.
    """
    if engine not in ENGINES:
        return _error_result(engine, f"unknown engine: {engine!r}")
    question = question.strip()
    if not question:
        return _error_result(engine, "question must not be empty")
    if len(question) > MAX_QUESTION_LENGTH:
        return _error_result(engine, f"question is too long (max {MAX_QUESTION_LENGTH} characters)")

    prompt = question if resume_id else CONTEXT_PREFIX + question
    ask_engine = _ask_claude if engine == "claude" else _ask_codex
    start = time.monotonic()
    try:
        return ask_engine(jira_dir, prompt, resume_id, timeout, popen, on_step)
    except FileNotFoundError:
        return _error_result(
            engine,
            f'the "{engine}" CLI isn\'t installed (or not on PATH) on this machine',
            duration_seconds=time.monotonic() - start,
        )
    except subprocess.TimeoutExpired:
        return _error_result(engine, f"timed out after {timeout}s waiting for {engine}", duration_seconds=time.monotonic() - start)


def _run_streaming(
    argv: list[str], *, cwd: Path, timeout: int, popen: PopenFactory, on_line: Callable[[str], None]
) -> tuple[int, str]:
    """Launches argv via `popen`, calling on_line(line) for each stdout
    line as it's produced, and enforcing `timeout` manually -- Popen has
    no built-in timeout the way subprocess.run does. A background
    threading.Timer (not a per-line elapsed check) is what makes this
    correct even if the process hangs producing *no* output at all: a
    per-line check would never fire in that case, since it only runs
    after a line actually arrives.

    stderr is read in one shot after the process exits, not drained
    concurrently with stdout -- a known, accepted simplification: if a
    child ever wrote enough stderr to fill its OS pipe buffer while
    blocked on a full stdout pipe, this could deadlock. Neither CLI does
    that in practice (their diagnostic stderr output is small), so a
    second reader thread isn't worth the complexity it would add.

    Returns (returncode, stderr_text). Raises subprocess.TimeoutExpired if
    the timer fires before the process exits.
    """
    process = popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        process.kill()

    timer = threading.Timer(timeout, _on_timeout)
    timer.start()
    try:
        for line in process.stdout:
            on_line(line)
        process.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(argv, timeout)
    stderr_text = process.stderr.read() if process.stderr else ""
    return process.returncode, stderr_text


def _ask_claude(
    jira_dir: Path, prompt: str, resume_id: str | None, timeout: int, popen: PopenFactory, on_step: OnStep
) -> AssistantResult:
    start = time.monotonic()
    argv = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--tools", "Read,Grep,Glob"]
    if resume_id:
        argv += ["--resume", resume_id]
    final_payload: dict[str, Any] | None = None

    def handle_line(line: str) -> None:
        nonlocal final_payload
        event = _parse_json_object(line)
        if event is None:
            return
        if event.get("type") == "result":
            final_payload = event
            return
        step = _claude_step(event)
        if step:
            on_step(step)

    returncode, stderr_text = _run_streaming(argv, cwd=jira_dir, timeout=timeout, popen=popen, on_line=handle_line)
    duration = time.monotonic() - start
    if returncode != 0:
        return _error_result("claude", _tail(stderr_text or "claude exited with an error"), duration_seconds=duration)
    if final_payload is None:
        return _error_result("claude", "claude produced no result", duration_seconds=duration)
    session_id = final_payload.get("session_id")
    if final_payload.get("is_error"):
        return AssistantResult(
            engine="claude",
            answer="",
            error=str(final_payload.get("result") or "claude reported an error"),
            session_id=session_id,
            duration_seconds=duration,
        )
    return AssistantResult(
        engine="claude", answer=str(final_payload.get("result") or ""), error=None, session_id=session_id, duration_seconds=duration
    )


def _claude_step(event: dict[str, Any]) -> str | None:
    if event.get("type") != "assistant":
        return None
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "tool_use":
            name = item.get("name")
            tool_input = item.get("input") or {}
            if name == "Read":
                return f"Reading {tool_input.get('file_path', '')}"
            if name == "Grep":
                return f"Searching for {tool_input.get('pattern', '')!r}"
            if name == "Glob":
                return f"Listing {tool_input.get('pattern', '')}"
            return f"Using {name}"
        if kind == "thinking":
            return "Thinking…"
    return None


def _ask_codex(
    jira_dir: Path, prompt: str, resume_id: str | None, timeout: int, popen: PopenFactory, on_step: OnStep
) -> AssistantResult:
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = Path(tmp_dir) / "answer.txt"
        if resume_id:
            # Flags stay before the "resume" subcommand -- verified for
            # real; codex's own arg parser rejects them placed after.
            argv = [
                "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "-o", str(output_path), "--json", "resume", resume_id, prompt,
            ]
        else:
            argv = [
                "codex", "exec", prompt, "--sandbox", "read-only", "-C", str(jira_dir),
                "--skip-git-repo-check", "-o", str(output_path), "--json",
            ]
        thread_id: str | None = None

        def handle_line(line: str) -> None:
            nonlocal thread_id
            event = _parse_json_object(line)
            if event is None:
                return
            if event.get("type") == "thread.started":
                raw_id = event.get("thread_id")
                thread_id = str(raw_id) if raw_id else None
                return
            step = _codex_step(event)
            if step:
                on_step(step)

        returncode, stderr_text = _run_streaming(argv, cwd=jira_dir, timeout=timeout, popen=popen, on_line=handle_line)
        duration = time.monotonic() - start
        if returncode != 0:
            return _error_result("codex", _tail(stderr_text or "codex exited with an error"), duration_seconds=duration)
        answer = output_path.read_text().strip() if output_path.exists() else ""
    if not answer:
        return _error_result("codex", "codex produced no output", duration_seconds=duration)
    return AssistantResult(engine="codex", answer=answer, error=None, session_id=thread_id, duration_seconds=duration)


def _codex_step(event: dict[str, Any]) -> str | None:
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    event_type = event.get("type")
    if kind == "command_execution" and event_type == "item.started":
        return f"Running: {item.get('command') or ''}"
    if kind == "agent_message" and event_type == "item.completed":
        text = str(item.get("text") or "").strip()
        return _tail(text, 160) if text else None
    return None


def _parse_json_object(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _tail(text: str, limit: int = 2000) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text
