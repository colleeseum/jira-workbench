from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

from jira_workbench.assistant import (
    CONTEXT_PREFIX,
    MAX_QUESTION_LENGTH,
    _claude_step,
    _codex_step,
    ask,
    ask_streaming,
)


class _FakeStderr:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class _HangingStdout:
    """Blocks forever on iteration until `stop_event` is set (by
    FakePopen.kill(), itself triggered by _run_streaming's own timeout
    watchdog) -- lets the real timeout path be exercised fast, without an
    actual multi-second sleep in the test."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event

    def __iter__(self) -> "_HangingStdout":
        return self

    def __next__(self) -> str:
        self._stop_event.wait()
        raise StopIteration


class FakePopen:
    """Stands in for subprocess.Popen -- records the call and, once
    invoked, plays back scripted stdout lines (or hangs until killed),
    scripted stderr, and a scripted return code."""

    def __init__(self, lines: list[str] | None = None, *, returncode: int = 0, stderr: str = "", hang: bool = False) -> None:
        self._lines = lines or []
        self.returncode = returncode
        self._stderr_text = stderr
        self._hang = hang
        self._stop_event = threading.Event()
        self.killed = False
        self.calls: list[dict[str, Any]] = []
        self.stdout = _HangingStdout(self._stop_event) if hang else iter(self._lines)
        self.stderr = _FakeStderr(stderr)

    def __call__(self, argv: list[str], **kwargs: Any) -> "FakePopen":
        self.calls.append({"argv": argv, **kwargs})
        return self

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._stop_event.set()


def _claude_line(event: dict[str, Any]) -> str:
    return json.dumps(event) + "\n"


def _claude_tool_use(name: str, **input_fields: Any) -> str:
    return _claude_line(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": input_fields}]}}
    )


def _claude_result(*, result: str = "an answer", is_error: bool = False, session_id: str = "sess-1") -> str:
    return _claude_line({"type": "result", "subtype": "success", "is_error": is_error, "result": result, "session_id": session_id})


def test_ask_streaming_rejects_an_unknown_engine(tmp_path: Path) -> None:
    result = ask_streaming("gpt5", tmp_path, "hello")

    assert result.error == "unknown engine: 'gpt5'"


def test_ask_streaming_rejects_an_empty_question(tmp_path: Path) -> None:
    result = ask_streaming("claude", tmp_path, "   ")

    assert result.error == "question must not be empty"


def test_ask_streaming_rejects_a_too_long_question(tmp_path: Path) -> None:
    result = ask_streaming("claude", tmp_path, "x" * (MAX_QUESTION_LENGTH + 1))

    assert result.error == f"question is too long (max {MAX_QUESTION_LENGTH} characters)"


def test_ask_claude_fresh_turn_parses_the_answer_and_session_id(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_result(result="Sample answer", session_id="abc-123")])

    result = ask_streaming("claude", tmp_path, "what is this?", popen=popen)

    assert result.error is None
    assert result.answer == "Sample answer"
    assert result.session_id == "abc-123"
    assert result.engine == "claude"


def test_ask_claude_reports_steps_as_they_stream(tmp_path: Path) -> None:
    popen = FakePopen(
        lines=[
            _claude_tool_use("Read", file_path="issue.json"),
            _claude_tool_use("Grep", pattern="PLAT-"),
            _claude_result(),
        ]
    )
    steps: list[str] = []

    ask_streaming("claude", tmp_path, "what is this?", on_step=steps.append, popen=popen)

    assert steps == ["Reading issue.json", "Searching for 'PLAT-'"]


def test_ask_claude_uses_stream_json_and_verbose(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_result()])

    ask_streaming("claude", tmp_path, "what is this?", popen=popen)

    argv = popen.calls[0]["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv


def test_ask_claude_prepends_context_prefix_on_a_fresh_turn(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_result()])

    ask_streaming("claude", tmp_path, "what is this?", popen=popen)

    prompt = popen.calls[0]["argv"][2]
    assert prompt.startswith(CONTEXT_PREFIX)
    assert prompt.endswith("what is this?")


def test_ask_claude_resume_turn_omits_context_prefix_and_adds_resume_flag(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_result()])

    ask_streaming("claude", tmp_path, "follow up", resume_id="abc-123", popen=popen)

    argv = popen.calls[0]["argv"]
    assert argv[2] == "follow up"
    assert argv[argv.index("--resume") + 1] == "abc-123"


def test_ask_claude_uses_a_read_only_tool_allow_list(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_result()])

    ask_streaming("claude", tmp_path, "what is this?", popen=popen)

    argv = popen.calls[0]["argv"]
    tools = argv[argv.index("--tools") + 1]
    assert tools == "Read,Grep,Glob"


def test_ask_claude_runs_with_jira_dir_as_cwd(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_result()])

    ask_streaming("claude", tmp_path, "what is this?", popen=popen)

    assert popen.calls[0]["cwd"] == tmp_path


def test_ask_claude_reports_an_engine_side_error(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_result(result="I can't help with that", is_error=True, session_id="s1")])

    result = ask_streaming("claude", tmp_path, "do something bad", popen=popen)

    assert result.error == "I can't help with that"
    assert result.session_id == "s1"


def test_ask_claude_reports_a_non_zero_exit(tmp_path: Path) -> None:
    popen = FakePopen(lines=[], returncode=1, stderr="boom")

    result = ask_streaming("claude", tmp_path, "what is this?", popen=popen)

    assert result.error == "boom"


def test_ask_claude_reports_missing_result_event(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_tool_use("Read", file_path="x")])

    result = ask_streaming("claude", tmp_path, "what is this?", popen=popen)

    assert result.error == "claude produced no result"


def test_ask_reports_the_cli_not_being_installed(tmp_path: Path) -> None:
    class RaisingPopen:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError()

    result = ask_streaming("codex", tmp_path, "what is this?", popen=RaisingPopen())

    assert result.error == 'the "codex" CLI isn\'t installed (or not on PATH) on this machine'


def test_ask_streaming_reports_a_timeout_without_a_real_wait(tmp_path: Path) -> None:
    popen = FakePopen(hang=True)

    result = ask_streaming("claude", tmp_path, "what is this?", timeout=0.05, popen=popen)

    assert result.error == "timed out after 0.05s waiting for claude"
    assert popen.killed is True


def _codex_line(event: dict[str, Any]) -> str:
    return json.dumps(event) + "\n"


def _codex_thread_started(thread_id: str = "thread-1") -> str:
    return _codex_line({"type": "thread.started", "thread_id": thread_id})


def _codex_command(command: str = "sed -n '1,10p' file.json") -> str:
    return _codex_line({"type": "item.started", "item": {"type": "command_execution", "command": command}})


def _codex_agent_message(text: str = "Looking at the file now.") -> str:
    return _codex_line({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


class _CodexFakePopen(FakePopen):
    """Also writes the requested answer to the -o output file path codex
    would have written to -- codex's real answer comes back via that
    file, not stdout."""

    def __init__(self, lines: list[str], *, answer: str = "an answer", **kwargs: Any) -> None:
        super().__init__(lines=lines, **kwargs)
        self._answer = answer

    def __call__(self, argv: list[str], **kwargs: Any) -> "_CodexFakePopen":
        super().__call__(argv, **kwargs)
        if self.returncode == 0:
            Path(argv[argv.index("-o") + 1]).write_text(self._answer)
        return self


def test_ask_codex_fresh_turn_parses_the_answer_and_thread_id(tmp_path: Path) -> None:
    popen = _CodexFakePopen(lines=[_codex_thread_started("thread-abc")], answer="Sample codex answer")

    result = ask_streaming("codex", tmp_path, "what is this?", popen=popen)

    assert result.error is None
    assert result.answer == "Sample codex answer"
    assert result.session_id == "thread-abc"


def test_ask_codex_reports_steps_as_they_stream(tmp_path: Path) -> None:
    popen = _CodexFakePopen(
        lines=[_codex_thread_started(), _codex_command("sed -n '1,10p' file.json"), _codex_agent_message("Looking now.")]
    )
    steps: list[str] = []

    ask_streaming("codex", tmp_path, "what is this?", on_step=steps.append, popen=popen)

    assert steps == ["Running: sed -n '1,10p' file.json", "Looking now."]


def test_ask_codex_fresh_turn_includes_prompt_and_sandbox_flags(tmp_path: Path) -> None:
    popen = _CodexFakePopen(lines=[_codex_thread_started()])

    ask_streaming("codex", tmp_path, "what is this?", popen=popen)

    argv = popen.calls[0]["argv"]
    assert argv[2] == CONTEXT_PREFIX + "what is this?"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("-C") + 1] == str(tmp_path)


def test_ask_codex_resume_turn_uses_resume_subcommand_with_flags_first(tmp_path: Path) -> None:
    popen = _CodexFakePopen(lines=[_codex_thread_started("thread-abc")])

    ask_streaming("codex", tmp_path, "follow up", resume_id="thread-abc", popen=popen)

    argv = popen.calls[0]["argv"]
    assert "-C" not in argv
    resume_index = argv.index("resume")
    assert argv[resume_index + 1] == "thread-abc"
    assert argv[resume_index + 2] == "follow up"
    assert argv.index("--sandbox") < resume_index


def test_ask_codex_reports_a_non_zero_exit(tmp_path: Path) -> None:
    popen = _CodexFakePopen(lines=[], returncode=1, stderr="codex boom")

    result = ask_streaming("codex", tmp_path, "what is this?", popen=popen)

    assert result.error == "codex boom"


def test_ask_codex_reports_no_output(tmp_path: Path) -> None:
    popen = _CodexFakePopen(lines=[_codex_thread_started()], answer="")

    result = ask_streaming("codex", tmp_path, "what is this?", popen=popen)

    assert result.error == "codex produced no output"


def test_claude_step_recognizes_read_grep_glob_and_thinking() -> None:
    assert _claude_step({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.json"}}]}}) == "Reading a.json"
    assert _claude_step({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}]}}) == "Searching for 'x'"
    assert _claude_step({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Glob", "input": {"pattern": "*.json"}}]}}) == "Listing *.json"
    assert _claude_step({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}}) == "Thinking…"


def test_claude_step_ignores_non_assistant_and_text_only_events() -> None:
    assert _claude_step({"type": "result", "result": "done"}) is None
    assert _claude_step({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}) is None


def test_codex_step_recognizes_command_execution_and_agent_message() -> None:
    assert _codex_step({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}}) == "Running: ls"
    assert _codex_step({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}) == "hi"


def test_codex_step_ignores_command_completion_and_started_agent_message() -> None:
    # Only the *started* command and the *completed* message are surfaced
    # -- avoids a near-duplicate step for the same underlying event.
    assert _codex_step({"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}}) is None
    assert _codex_step({"type": "item.started", "item": {"type": "agent_message", "text": "hi"}}) is None


def test_ask_is_ask_streaming_with_steps_discarded(tmp_path: Path) -> None:
    popen = FakePopen(lines=[_claude_tool_use("Read", file_path="x"), _claude_result(result="final")])

    result = ask("claude", tmp_path, "what is this?", popen=popen)

    assert result.answer == "final"


def test_ask_streaming_reports_a_generic_subprocess_timeout(tmp_path: Path) -> None:
    class TimeoutPopen:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=5)

    result = ask_streaming("claude", tmp_path, "what is this?", timeout=5, popen=TimeoutPopen())

    assert result.error == "timed out after 5s waiting for claude"
