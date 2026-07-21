from __future__ import annotations

import difflib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .sync import find_existing_issue, read_json, updated_at, write_json


SUPPORTED_PUSH_FIELDS = {
    "assignee": "--assignee",
    "description": "--description",
    "labels": "--labels",
    "summary": "--summary",
    "type": "--type",
}


class ShadowError(RuntimeError):
    pass


class Runner(Protocol):
    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        pass

    def run(self, args: list[str], *, allow_failure: bool = False) -> str:
        pass


Progress = Callable[[str], None]


@dataclass(frozen=True)
class PushResult:
    pushed: int
    skipped: int
    blocked: int


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_dir(jira_dir: Path, key: str) -> Path:
    issue_path = find_existing_issue(jira_dir / "components", key)
    if issue_path is None:
        raise ShadowError(f"work item {key} is not synced locally")
    return issue_path.parent


def issue_path(jira_dir: Path, key: str) -> Path:
    return issue_dir(jira_dir, key) / "issue.json"


def shadow_path(jira_dir: Path, key: str) -> Path:
    return issue_dir(jira_dir, key) / "shadow.json"


def load_shadow(jira_dir: Path, key: str) -> dict[str, Any] | None:
    path = shadow_path(jira_dir, key)
    if not path.exists():
        return None
    value = read_json(path)
    if not isinstance(value, dict):
        raise ShadowError(f"shadow file for {key} is not a JSON object")
    return value


def new_shadow(jira_dir: Path, key: str) -> dict[str, Any]:
    issue = read_json(issue_path(jira_dir, key))
    if not isinstance(issue, dict):
        raise ShadowError(f"local issue file for {key} is not a JSON object")
    return {
        "key": key,
        "baseUpdated": updated_at(issue),
        "state": "working",
        "fields": {},
        "comments": [],
        "createdAt": now(),
        "updatedAt": now(),
    }


def save_shadow(jira_dir: Path, key: str, shadow: dict[str, Any]) -> None:
    shadow["updatedAt"] = now()
    write_json(shadow_path(jira_dir, key), shadow)


def delete_shadow(jira_dir: Path, key: str) -> None:
    path = shadow_path(jira_dir, key)
    if path.exists():
        path.unlink()


def ensure_shadow(jira_dir: Path, key: str) -> dict[str, Any]:
    return load_shadow(jira_dir, key) or new_shadow(jira_dir, key)


def set_field(jira_dir: Path, key: str, field: str, value: str) -> dict[str, Any]:
    shadow = ensure_shadow(jira_dir, key)
    fields = shadow.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ShadowError(f"shadow fields for {key} are not a JSON object")
    fields[field] = value
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def add_comment(jira_dir: Path, key: str, body: str) -> dict[str, Any]:
    shadow = ensure_shadow(jira_dir, key)
    comments = shadow.setdefault("comments", [])
    if not isinstance(comments, list):
        raise ShadowError(f"shadow comments for {key} are not a JSON array")
    comments.append(
        {
            "id": f"local-{uuid4().hex}",
            "body": body,
            "createdAt": now(),
            "state": "working",
        }
    )
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def commit_shadow(jira_dir: Path, key: str) -> dict[str, Any]:
    shadow = load_shadow(jira_dir, key)
    if shadow is None:
        raise ShadowError(f"work item {key} has no local shadow changes")
    shadow["state"] = "committed"
    shadow["committedAt"] = now()
    for comment in shadow.get("comments", []):
        if isinstance(comment, dict) and comment.get("state") == "working":
            comment["state"] = "committed"
    save_shadow(jira_dir, key, shadow)
    return shadow


def all_shadow_keys(jira_dir: Path) -> list[str]:
    components = jira_dir / "components"
    if not components.exists():
        return []
    return sorted(path.parent.name for path in components.glob("*/*/shadow.json"))


def shadow_status(jira_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for key in all_shadow_keys(jira_dir):
        shadow = load_shadow(jira_dir, key)
        if shadow is None:
            continue
        fields = shadow.get("fields", {})
        comments = shadow.get("comments", [])
        rows.append(
            {
                "key": key,
                "state": shadow.get("state", "working"),
                "fields": len(fields) if isinstance(fields, dict) else 0,
                "comments": len(comments) if isinstance(comments, list) else 0,
                "baseUpdated": shadow.get("baseUpdated"),
            }
        )
    return rows


def field_value(issue: dict[str, Any], field: str) -> Any:
    fields = issue.get("fields", {})
    if not isinstance(fields, dict):
        return None
    if field == "type":
        field = "issuetype"
    value = fields.get(field)
    if isinstance(value, dict) and "name" in value:
        return value["name"]
    return value


def render_diff(jira_dir: Path, key: str) -> str:
    shadow = load_shadow(jira_dir, key)
    if shadow is None:
        return f"{key}: no local shadow changes"
    issue = read_json(issue_path(jira_dir, key))
    if not isinstance(issue, dict):
        raise ShadowError(f"local issue file for {key} is not a JSON object")

    lines = [f"{key} ({shadow.get('state', 'working')})"]
    fields = shadow.get("fields", {})
    if isinstance(fields, dict):
        for field in sorted(fields):
            before = json.dumps(field_value(issue, field), indent=2, sort_keys=True).splitlines()
            after = json.dumps(fields[field], indent=2, sort_keys=True).splitlines()
            lines.append(f"\nfield: {field}")
            lines.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"remote/{field}",
                    tofile=f"shadow/{field}",
                    lineterm="",
                )
            )

    comments = shadow.get("comments", [])
    if isinstance(comments, list) and comments:
        lines.append("\ncomments:")
        for comment in comments:
            if isinstance(comment, dict):
                state = comment.get("state", "working")
                lines.append(f"+ [{state}] {comment.get('body', '')}")
    return "\n".join(lines)


def unsupported_fields(shadow: dict[str, Any]) -> list[str]:
    fields = shadow.get("fields", {})
    if not isinstance(fields, dict):
        return []
    return sorted(field for field in fields if field not in SUPPORTED_PUSH_FIELDS)


def remote_updated(runner: Runner, key: str) -> str | None:
    payload = runner.json(["jira", "workitem", "view", key, "--fields", "updated", "--json"])
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ShadowError(f"could not read remote updated value for {key}")
    return updated_at(payload)


def push_key(
    jira_dir: Path,
    key: str,
    runner: Runner,
    *,
    dry_run: bool = False,
    progress: Progress | None = print,
) -> str:
    shadow = load_shadow(jira_dir, key)
    if shadow is None:
        return "skipped"

    current_updated = remote_updated(runner, key)
    if shadow.get("baseUpdated") != current_updated:
        if progress:
            progress(f"{key}: skipped, remote changed since local edits")
        return "blocked"

    unsupported = unsupported_fields(shadow)
    if unsupported:
        raise ShadowError(
            f"{key} has fields that cannot be pushed yet: {', '.join(unsupported)}. "
            f"Supported push fields: {', '.join(sorted(SUPPORTED_PUSH_FIELDS))}"
        )

    if dry_run:
        if progress:
            progress(f"{key}: would push")
        return "pushed"

    fields = shadow.get("fields", {})
    if isinstance(fields, dict) and fields:
        args = ["jira", "workitem", "edit", "--key", key, "--yes"]
        for field, value in sorted(fields.items()):
            args.extend([SUPPORTED_PUSH_FIELDS[field], str(value)])
        runner.run(args)

    comments = shadow.get("comments", [])
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict) and comment.get("state") != "pushed":
                runner.run(
                    [
                        "jira",
                        "workitem",
                        "comment",
                        "create",
                        "--key",
                        key,
                        "--body",
                        str(comment.get("body", "")),
                    ]
                )

    delete_shadow(jira_dir, key)
    if progress:
        progress(f"{key}: pushed")
    return "pushed"


def push_shadows(
    jira_dir: Path,
    keys: list[str],
    runner: Runner,
    *,
    dry_run: bool = False,
    progress: Progress | None = print,
) -> PushResult:
    selected = keys or all_shadow_keys(jira_dir)
    pushed = skipped = blocked = 0
    for key in selected:
        result = push_key(jira_dir, key, runner, dry_run=dry_run, progress=progress)
        if result == "pushed":
            pushed += 1
        elif result == "blocked":
            blocked += 1
        else:
            skipped += 1
    return PushResult(pushed=pushed, skipped=skipped, blocked=blocked)
