from __future__ import annotations

from pathlib import Path
from typing import Any

from jira_workbench.sync import (
    SyncConfig,
    SyncError,
    build_manifest,
    component_slug,
    normalize_issue,
    normalize_work_items,
    sync_project,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        self.calls.append(args)
        if args[:3] == ["jira", "workitem", "search"]:
            return {"issues": [{"key": "SAT-1"}, {"key": "SAT-2"}]}
        if args[:3] == ["jira", "workitem", "view"]:
            key = args[3]
            component = "API Team" if key == "SAT-1" else None
            return {
                "key": key,
                "fields": {
                    "summary": f"Summary for {key}",
                    "status": {"name": "Open"},
                    "issuetype": {"name": "Task"},
                    "updated": f"2026-07-20T00:00:0{key[-1]}.000+0000",
                    "customfield_10071": {"value": component} if component else None,
                },
            }
        if args[:4] == ["jira", "workitem", "comment", "list"]:
            return [{"body": "comment"}]
        if args[:4] == ["jira", "workitem", "attachment", "list"]:
            return []
        raise AssertionError(args)


def test_normalize_work_items_accepts_common_acli_shapes() -> None:
    assert normalize_work_items([{"key": "A-1"}]) == [{"key": "A-1"}]
    assert normalize_work_items({"values": [{"key": "A-1"}]}) == [{"key": "A-1"}]
    assert normalize_work_items({"issues": [{"key": "A-1"}]}) == [{"key": "A-1"}]


def test_normalize_issue_accepts_single_item_list() -> None:
    assert normalize_issue([{"key": "A-1"}], "A-1") == {"key": "A-1"}


def test_normalize_issue_rejects_unexpected_shape() -> None:
    try:
        normalize_issue([], "A-1")
    except SyncError as exc:
        assert "Jira work item A-1 returned unexpected JSON" in str(exc)
    else:
        raise AssertionError("expected SyncError")


def test_component_slug_matches_original_shell_behavior() -> None:
    issue = {"fields": {"customfield_10071": {"value": "API Team / Core"}}}
    assert component_slug(issue, "customfield_10071") == "api-team---core"


def test_sync_project_writes_component_layout_and_manifest(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        runner,
        progress=None,
    )

    assert result.work_item_count == 2
    assert result.changed_count == 2
    assert result.skipped_count == 0
    assert ["jira", "workitem", "view", "SAT-1", "--fields", "*all", "--json"] in runner.calls
    assert (tmp_path / "components/api-team/SAT-1/issue.json").exists()
    assert (tmp_path / "components/_unassigned/SAT-2/issue.json").exists()
    assert (tmp_path / "manifest.json").exists()


def test_sync_project_skips_unchanged_items(tmp_path: Path) -> None:
    runner = FakeRunner()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)
    sync_project(config, runner, progress=None)

    second = sync_project(config, runner, progress=None)

    assert second.changed_count == 0
    assert second.skipped_count == 2


def test_build_manifest_reads_existing_issue_layout(tmp_path: Path) -> None:
    runner = FakeRunner()
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        runner,
        progress=None,
    )

    manifest = build_manifest(tmp_path)

    assert manifest["workItemCount"] == 2
    assert manifest["components"][0]["component"] == "_unassigned"
    assert manifest["workItems"][0]["path"].startswith("components/")
