from __future__ import annotations

from pathlib import Path
from typing import Any

from jira_workbench.shadow import (
    ShadowError,
    add_comment,
    commit_shadow,
    push_shadows,
    render_diff,
    set_field,
    shadow_path,
    shadow_status,
)
from jira_workbench.sync import SyncConfig, sync_project
from test_sync import FakeRunner


class PushRunner:
    def __init__(self, updated: str) -> None:
        self.updated = updated
        self.run_calls: list[list[str]] = []
        self.json_calls: list[list[str]] = []

    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        self.json_calls.append(args)
        return {"key": args[3], "fields": {"updated": self.updated}}

    def run(self, args: list[str], *, allow_failure: bool = False) -> str:
        self.run_calls.append(args)
        return ""


def synced_jira_dir(tmp_path: Path) -> Path:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeRunner(),
        progress=None,
    )
    return tmp_path


def test_shadow_set_comment_diff_and_commit(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)

    set_field(jira_dir, "SAT-1", "description", "Local description")
    add_comment(jira_dir, "SAT-1", "Local comment")
    diff = render_diff(jira_dir, "SAT-1")
    commit_shadow(jira_dir, "SAT-1")
    rows = shadow_status(jira_dir)

    assert "field: description" in diff
    assert "+ [working] Local comment" in diff
    assert rows[0]["key"] == "SAT-1"
    assert rows[0]["state"] == "committed"


def test_push_skips_remote_changed_item(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")
    commit_shadow(jira_dir, "SAT-1")
    runner = PushRunner("2026-07-20T99:99:99.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], runner, progress=None)

    assert result.blocked == 1
    assert runner.run_calls == []


def test_push_applies_supported_fields_and_comments(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")
    add_comment(jira_dir, "SAT-1", "Local comment")
    commit_shadow(jira_dir, "SAT-1")
    runner = PushRunner("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], runner, progress=None)

    assert result.pushed == 1
    assert [
        "jira",
        "workitem",
        "edit",
        "--key",
        "SAT-1",
        "--yes",
        "--description",
        "Local description",
    ] in runner.run_calls
    assert [
        "jira",
        "workitem",
        "comment",
        "create",
        "--key",
        "SAT-1",
        "--body",
        "Local comment",
    ] in runner.run_calls
    assert not shadow_path(jira_dir, "SAT-1").exists()
    assert shadow_status(jira_dir) == []


def test_push_refuses_unsupported_fields(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "fix_version", "1.2.3")
    commit_shadow(jira_dir, "SAT-1")
    runner = PushRunner("2026-07-20T00:00:01.000+0000")

    try:
        push_shadows(jira_dir, ["SAT-1"], runner, progress=None)
    except ShadowError as exc:
        assert "fix_version" in str(exc)
    else:
        raise AssertionError("expected ShadowError")
