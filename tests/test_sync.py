from __future__ import annotations

from pathlib import Path
from typing import Any

from jira_workbench.sync import (
    SyncConfig,
    SyncError,
    build_manifest,
    component_slug,
    issue_key_sort_key,
    normalize_issue,
    normalize_work_items,
    read_json,
    sync_project,
    write_json,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        self.calls.append(args)
        if args[:4] == ["jira", "project", "view", "--key"]:
            return {
                "key": args[4],
                "versions": [
                    {"id": "10001", "name": "helm-chart-sa 3.3.0", "released": True},
                    {"id": "10002", "name": "helm-chart-sa 3.4.0", "released": False},
                ]
            }
        if args[:3] == ["jira", "workitem", "search"]:
            return {
                "issues": [
                    {
                        "key": "SAT-1",
                        "fields": {"updated": "2026-07-20T00:00:01.000+0000"},
                    },
                    {
                        "key": "SAT-2",
                        "fields": {"updated": "2026-07-20T00:00:02.000+0000"},
                    },
                ]
            }
        if args[:3] == ["jira", "workitem", "view"]:
            key = args[3]
            component = "API Team" if key == "SAT-1" else None
            return {
                "key": key,
                "fields": {
                    "summary": f"Summary for {key}",
                    "status": {"name": "Open"},
                    "priority": {"name": "Medium"},
                    "assignee": {"displayName": "Serge Colle"},
                    "issuetype": {"name": "Task"},
                    "updated": f"2026-07-20T00:00:0{key[-1]}.000+0000",
                    "fixVersions": [{"name": "helm-chart-sa 3.4.4"}] if key == "SAT-1" else [],
                    "parent": {
                        "key": "SAT-100",
                        "fields": {"summary": "Feature Epic"},
                    }
                    if key == "SAT-1"
                    else None,
                    "customfield_10071": {"value": component} if component else None,
                },
            }
        if args[:4] == ["jira", "workitem", "comment", "list"]:
            return [{"body": "comment"}]
        if args[:4] == ["jira", "workitem", "attachment", "list"]:
            return []
        raise AssertionError(args)


class FakeRunnerWithoutIndexUpdated(FakeRunner):
    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        if args[:3] == ["jira", "workitem", "search"]:
            self.calls.append(args)
            return {"issues": [{"key": "SAT-1"}, {"key": "SAT-2"}]}
        return super().json(args, allow_failure=allow_failure)


class FakeApiIndexClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def jql(
        self,
        jql: str,
        fields: str | list[str] = "*all",
        start: int = 0,
        limit: int | None = None,
        expand: str | None = None,
        validate_query: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"jql": jql, "fields": fields, "start": start, "limit": limit})
        return {
            "startAt": 0,
            "maxResults": 100,
            "total": 2,
            "issues": [
                {
                    "key": "SAT-1",
                    "fields": {"updated": "2026-07-20T00:00:01.000+0000"},
                },
                {
                    "key": "SAT-2",
                    "fields": {"updated": "2026-07-20T00:00:02.000+0000"},
                },
            ],
        }


class FailingApiIndexClient(FakeApiIndexClient):
    def jql(
        self,
        jql: str,
        fields: str | list[str] = "*all",
        start: int = 0,
        limit: int | None = None,
        expand: str | None = None,
        validate_query: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("api unavailable")


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


def test_component_slug_uses_native_components_by_default() -> None:
    issue = {"fields": {"components": [{"name": "Helm Chart"}, {"name": "Cloud"}]}}
    assert component_slug(issue, "components") == "helm-chart--cloud"


def test_issue_key_sort_key_sorts_by_numeric_suffix() -> None:
    keys = ["SAT-100", "SAT-9", "SAT-10", "SAT-2"]

    assert sorted(keys, key=issue_key_sort_key) == ["SAT-2", "SAT-9", "SAT-10", "SAT-100"]


def test_sync_project_writes_component_layout_and_manifest(tmp_path: Path) -> None:
    runner = FakeRunner()
    progress: list[str] = []
    result = sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        runner,
        progress=progress.append,
    )

    assert result.work_item_count == 2
    assert result.changed_count == 2
    assert result.skipped_count == 0
    assert result.version_count == 2
    assert ["jira", "project", "view", "--key", "SAT", "--json"] in runner.calls
    assert [
        "jira",
        "workitem",
        "search",
        "--jql",
        "project=SAT ORDER BY key",
        "--fields",
        "issuetype,key,updated",
        "--paginate",
        "--json",
    ] in runner.calls
    assert ["jira", "workitem", "view", "SAT-1", "--fields", "*all", "--json"] in runner.calls
    assert (tmp_path / "meta/versions.json").exists()
    assert (tmp_path / "components/api-team/SAT-1/issue.json").exists()
    assert (tmp_path / "components/_unassigned/SAT-2/issue.json").exists()
    assert (tmp_path / "manifest.json").exists()
    manifest = read_json(tmp_path / "manifest.json")
    assert manifest["workItems"][0]["fixVersion"] == "helm-chart-sa 3.4.4"
    assert manifest["workItems"][0]["priority"] == "Medium"
    assert manifest["workItems"][0]["assignee"] == "Serge Colle"
    assert manifest["workItems"][0]["epic"] == "SAT-100"
    assert "[3/5] Syncing changed issues... 1/2 SAT-1 changed=0 unchanged=0" in progress
    assert "[3/5] Syncing changed issues... 2/2 SAT-2 changed=1 unchanged=0" in progress


def test_sync_project_skips_unchanged_items(tmp_path: Path) -> None:
    runner = FakeRunner()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)
    sync_project(config, runner, progress=None)

    runner.calls = []
    second = sync_project(config, runner, progress=None)

    assert second.changed_count == 0
    assert second.skipped_count == 2
    assert not any(call[:3] == ["jira", "workitem", "view"] for call in runner.calls)


def test_sync_project_falls_back_to_view_when_index_has_no_updated(tmp_path: Path) -> None:
    runner = FakeRunnerWithoutIndexUpdated()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)
    sync_project(config, runner, progress=None)

    runner.calls = []
    second = sync_project(config, runner, progress=None)

    assert second.changed_count == 0
    assert second.skipped_count == 2
    assert ["jira", "workitem", "view", "SAT-1", "--fields", "*all", "--json"] in runner.calls
    assert ["jira", "workitem", "view", "SAT-2", "--fields", "*all", "--json"] in runner.calls


def test_sync_project_uses_api_for_project_index(tmp_path: Path) -> None:
    runner = FakeRunner()
    api_client = FakeApiIndexClient()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)

    sync_project(config, runner, progress=None, api_client=api_client)

    assert api_client.calls == [
        {
            "jql": "project=SAT ORDER BY key",
            "fields": ["issuetype", "key", "updated"],
            "start": 0,
            "limit": 100,
        }
    ]
    assert not any(call[:3] == ["jira", "workitem", "search"] for call in runner.calls)


def test_sync_project_falls_back_to_acli_index_when_api_index_fails(tmp_path: Path) -> None:
    runner = FakeRunner()
    progress: list[str] = []
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)

    sync_project(config, runner, progress=progress.append, api_client=FailingApiIndexClient())

    assert any(call[:3] == ["jira", "workitem", "search"] for call in runner.calls)
    assert "API project index failed, falling back to acli: api unavailable" in progress


def test_sync_project_preserves_shadow_when_refreshing_changed_issue(tmp_path: Path) -> None:
    runner = FakeRunner()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)
    sync_project(config, runner, progress=None)
    issue_path = tmp_path / "components/api-team/SAT-1/issue.json"
    issue = read_json(issue_path)
    issue["fields"]["updated"] = "2026-07-19T00:00:01.000+0000"
    write_json(issue_path, issue)
    shadow = {
        "key": "SAT-1",
        "baseUpdated": "2026-07-19T00:00:01.000+0000",
        "state": "working",
        "fields": {"summary": "local summary"},
        "comments": [],
    }
    write_json(tmp_path / "components/api-team/SAT-1/shadow.json", shadow)

    result = sync_project(config, runner, progress=None)

    assert result.changed_count == 1
    assert read_json(tmp_path / "components/api-team/SAT-1/shadow.json") == shadow


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


def test_build_manifest_orders_work_items_by_issue_number(tmp_path: Path) -> None:
    for key in ("SAT-100", "SAT-9", "SAT-10"):
        write_json(
            tmp_path / f"components/helm-chart/{key}/issue.json",
            {"key": key, "fields": {"summary": key, "status": {"name": "To Do"}}},
        )

    manifest = build_manifest(tmp_path)

    assert [item["key"] for item in manifest["workItems"]] == ["SAT-9", "SAT-10", "SAT-100"]
    assert manifest["components"][0]["workItems"] == ["SAT-9", "SAT-10", "SAT-100"]
