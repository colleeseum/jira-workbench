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


class FakeJiraClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def get_project_versions(self, key: str) -> Any:
        self.calls.append(("get_project_versions", key))
        return [
            {"id": "10001", "name": "helm-chart-sa 3.3.0", "released": True},
            {"id": "10002", "name": "helm-chart-sa 3.4.0", "released": False},
        ]

    def enhanced_jql_get_list_of_tickets(
        self,
        jql: str,
        fields: str | list[str] = "*all",
        limit: int | None = None,
        expand: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(("enhanced_jql_get_list_of_tickets", jql, fields, limit))
        return [
            {"key": "SAT-1", "fields": {"updated": "2026-07-20T00:00:01.000+0000"}},
            {"key": "SAT-2", "fields": {"updated": "2026-07-20T00:00:02.000+0000"}},
        ]

    def get_issue(
        self,
        issue_id_or_key: str,
        fields: str | list | tuple | set | None = None,
        properties: str | None = None,
        update_history: bool = True,
        expand: str | None = None,
    ) -> Any:
        self.calls.append(("get_issue", issue_id_or_key, fields))
        key = issue_id_or_key
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
                "parent": (
                    {"key": "SAT-100", "fields": {"summary": "Feature Epic"}} if key == "SAT-1" else None
                ),
                "customfield_10071": {"value": component} if component else None,
                "attachment": [{"filename": "notes.txt"}] if key == "SAT-1" else [],
            },
        }

    def issue_get_comments(self, issue_id: str) -> Any:
        self.calls.append(("issue_get_comments", issue_id))
        return {"comments": [{"body": "comment"}]} if issue_id == "SAT-1" else {"comments": []}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        raise AssertionError("comment pagination should not be exercised by these tests")

    def resource_url(self, resource: str, api_root: str = "rest/api", api_version: str | int = "latest") -> str:
        return f"https://example.atlassian.net/rest/api/3/{resource}"

    def get_all_fields(self) -> Any:
        self.calls.append(("get_all_fields",))
        return [
            {"id": "customfield_10071", "name": "Component Team", "custom": True},
            {"id": "customfield_10082", "name": "Customers SAT", "custom": True},
        ]


class FakeJiraClientWithoutIndexUpdated(FakeJiraClient):
    def enhanced_jql_get_list_of_tickets(
        self,
        jql: str,
        fields: str | list[str] = "*all",
        limit: int | None = None,
        expand: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(("enhanced_jql_get_list_of_tickets", jql, fields, limit))
        return [{"key": "SAT-1"}, {"key": "SAT-2"}]


def test_normalize_work_items_accepts_common_shapes() -> None:
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
    client = FakeJiraClient()
    progress: list[str] = []
    result = sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        client,
        progress=progress.append,
    )

    assert result.work_item_count == 2
    assert result.changed_count == 2
    assert result.skipped_count == 0
    assert result.version_count == 2
    assert ("get_project_versions", "SAT") in client.calls
    assert ("enhanced_jql_get_list_of_tickets", "project=SAT ORDER BY key", ["issuetype", "key", "updated"], None) in client.calls
    assert ("get_issue", "SAT-1", "*all") in client.calls
    assert (tmp_path / "meta/versions.json").exists()
    assert (tmp_path / "components/api-team/SAT-1/issue.json").exists()
    assert (tmp_path / "components/_unassigned/SAT-2/issue.json").exists()
    assert read_json(tmp_path / "components/api-team/SAT-1/attachments.json") == [{"filename": "notes.txt"}]
    assert read_json(tmp_path / "components/api-team/SAT-1/comments.json") == {"comments": [{"body": "comment"}]}
    assert (tmp_path / "manifest.json").exists()
    manifest = read_json(tmp_path / "manifest.json")
    assert manifest["workItems"][0]["fixVersion"] == "helm-chart-sa 3.4.4"
    assert manifest["workItems"][0]["priority"] == "Medium"
    assert manifest["workItems"][0]["assignee"] == "Serge Colle"
    assert manifest["workItems"][0]["epic"] == "SAT-100"
    assert "[3/5] Syncing changed issues... 1/2 SAT-1 changed=0 unchanged=0" in progress
    assert "[3/5] Syncing changed issues... 2/2 SAT-2 changed=1 unchanged=0" in progress


def test_sync_project_caches_field_names(tmp_path: Path) -> None:
    from jira_workbench.metadata import load_field_names

    client = FakeJiraClient()
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        client,
        progress=None,
    )

    assert ("get_all_fields",) in client.calls
    assert load_field_names(tmp_path) == {
        "customfield_10071": "Component Team",
        "customfield_10082": "Customers SAT",
    }


def test_sync_project_tolerates_field_name_fetch_failure(tmp_path: Path) -> None:
    class NoFieldsClient(FakeJiraClient):
        def get_all_fields(self) -> Any:
            raise RuntimeError("boom")

    progress: list[str] = []
    result = sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        NoFieldsClient(),
        progress=progress.append,
    )

    assert result.work_item_count == 2  # sync still completes
    assert any("field name refresh skipped" in line for line in progress)


def test_sync_project_skips_unchanged_items(tmp_path: Path) -> None:
    client = FakeJiraClient()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)
    sync_project(config, client, progress=None)

    client.calls = []
    second = sync_project(config, client, progress=None)

    assert second.changed_count == 0
    assert second.skipped_count == 2
    assert not any(call[0] == "get_issue" for call in client.calls)


def test_sync_project_force_refetches_even_when_updated_matches(tmp_path: Path) -> None:
    client = FakeJiraClient()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)
    sync_project(config, client, progress=None)

    client.calls = []
    forced_config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path, force=True)
    second = sync_project(forced_config, client, progress=None)

    assert second.changed_count == 2
    assert second.skipped_count == 0
    assert ("get_issue", "SAT-1", "*all") in client.calls
    assert ("get_issue", "SAT-2", "*all") in client.calls


def test_sync_project_falls_back_to_view_when_index_has_no_updated(tmp_path: Path) -> None:
    client = FakeJiraClientWithoutIndexUpdated()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)
    sync_project(config, client, progress=None)

    client.calls = []
    second = sync_project(config, client, progress=None)

    assert second.changed_count == 0
    assert second.skipped_count == 2
    assert ("get_issue", "SAT-1", "*all") in client.calls
    assert ("get_issue", "SAT-2", "*all") in client.calls


def test_sync_project_preserves_shadow_when_refreshing_changed_issue(tmp_path: Path) -> None:
    client = FakeJiraClient()
    config = SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path)
    sync_project(config, client, progress=None)
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

    result = sync_project(config, client, progress=None)

    assert result.changed_count == 1
    assert read_json(tmp_path / "components/api-team/SAT-1/shadow.json") == shadow


def test_build_manifest_reads_existing_issue_layout(tmp_path: Path) -> None:
    client = FakeJiraClient()
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        client,
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
