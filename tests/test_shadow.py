from __future__ import annotations

from pathlib import Path
from typing import Any

from jira_workbench.shadow import (
    add_comment,
    commit_shadow,
    load_shadow,
    push_shadows,
    render_diff,
    set_status_change,
    set_field,
    shadow_path,
    shadow_status,
)
from jira_workbench.sync import SyncConfig, read_json, sync_project, write_json
from test_sync import FakeRunner


class PushJiraClient:
    def __init__(self, updated: str | dict[str, str]) -> None:
        self.updated = updated
        self.add_comment_calls: list[tuple[str, str]] = []
        self.issue_update_calls: list[tuple[str, dict[str, Any], dict[Any, Any] | None, bool | None]] = []
        self.refreshed_comments: dict[str, Any] = {}
        self.refreshed_issues: dict[str, Any] = {}
        self.transition_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, dict[str, Any], bool]] = []

    def get_issue(self, issue_id_or_key: str, fields: str | list | tuple | set | None = None, **kwargs: Any) -> Any:
        updated = self.updated.get(issue_id_or_key) if isinstance(self.updated, dict) else self.updated
        if fields == "*all":
            return self.refreshed_issues.get(
                issue_id_or_key,
                {
                    "key": issue_id_or_key,
                    "fields": {
                        "updated": f"{updated}-refreshed",
                        "summary": f"Refreshed {issue_id_or_key}",
                        "components": [{"name": "api-team"}],
                        "customfield_10071": {"value": "helm-chart"},
                    },
                },
            )
        return {"key": issue_id_or_key, "fields": {"updated": updated}}

    def get_all_resolutions(self) -> list[dict[str, str]]:
        return [
            {"id": "10000", "name": "Done"},
            {"id": "10001", "name": "Won't Do"},
        ]

    def issue_get_comments(self, issue_id: str) -> Any:
        return self.refreshed_comments.get(issue_id, {"comments": []})

    def issue_transition(self, issue_key: str, status: str) -> None:
        self.transition_calls.append((issue_key, status))

    def issue_update(
        self,
        issue_key: str,
        fields: str | dict[str, Any],
        update: dict[Any, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.issue_update_calls.append(
            (issue_key, fields if isinstance(fields, dict) else {"fields": fields}, update, kwargs.get("notify_users"))
        )

    def issue_add_comment(self, issue_key: str, comment: str, visibility: dict[str, Any] | None = None) -> None:
        self.add_comment_calls.append((issue_key, comment))

    def update_issue_field(self, key: str, fields: dict[str, Any], notify_users: bool = True) -> None:
        self.update_calls.append((key, fields, notify_users))


class FailingUpdateJiraClient(PushJiraClient):
    def update_issue_field(self, key: str, fields: dict[str, Any], notify_users: bool = True) -> None:
        raise RuntimeError("Operation value must be a string")


class FailingRefreshJiraClient(PushJiraClient):
    def get_issue(self, issue_id_or_key: str, fields: str | list | tuple | set | None = None, **kwargs: Any) -> Any:
        if fields == "*all":
            raise RuntimeError("refresh failed")
        return super().get_issue(issue_id_or_key, fields=fields, **kwargs)


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
    client = PushJiraClient("2026-07-20T99:99:99.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.blocked == 1
    assert client.update_calls == []


def test_push_applies_supported_fields_and_comments(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")
    add_comment(jira_dir, "SAT-1", "Local comment")
    commit_shadow(jira_dir, "SAT-1")
    client = PushJiraClient("2026-07-20T00:00:01.000+0000")
    client.refreshed_issues["SAT-1"] = {
        "key": "SAT-1",
        "fields": {
            "updated": "2026-07-22T10:30:00.000+0000",
            "description": "Remote refreshed description",
            "summary": "Remote refreshed summary",
            "components": [{"name": "api-team"}],
            "customfield_10071": {"value": "helm-chart"},
            "attachment": [{"id": "att-1", "filename": "note.txt"}],
        },
    }
    client.refreshed_comments["SAT-1"] = {"comments": [{"id": "comment-1", "body": "Remote comment"}]}

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.pushed == 1
    assert client.update_calls == [
        (
            "SAT-1",
            {"description": "Local description"},
            False,
        )
    ]
    assert client.add_comment_calls == [("SAT-1", "Local comment")]
    assert not shadow_path(jira_dir, "SAT-1").exists()
    assert shadow_status(jira_dir) == []
    refreshed_issue = read_json(jira_dir / "components/api-team/SAT-1/issue.json")
    assert refreshed_issue["fields"]["summary"] == "Remote refreshed summary"
    assert read_json(jira_dir / "components/api-team/SAT-1/comments.json") == {
        "comments": [{"id": "comment-1", "body": "Remote comment"}]
    }
    assert read_json(jira_dir / "components/api-team/SAT-1/attachments.json") == [
        {"id": "att-1", "filename": "note.txt"}
    ]


def test_push_shadows_reports_progress_before_each_item(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "summary", "Changed")
    events: list[str] = []
    client = PushJiraClient("2026-07-20T00:00:01.000+0000")

    push_shadows(jira_dir, ["SAT-1"], client, progress=events.append)

    assert events[0] == "SAT-1: pushing (1/1)"
    assert events[-1] == "SAT-1: pushed"


def test_push_serializes_structured_supported_fields(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "labels", ["one", "two"])
    set_field(jira_dir, "SAT-1", "priority", {"name": "High"})
    client = PushJiraClient("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.pushed == 1
    assert client.update_calls == [
        ("SAT-1", {"labels": ["one", "two"], "priority": {"name": "High"}}, False)
    ]


def test_push_serializes_single_value_custom_field_as_jira_option(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "customfield_10071", {"value": "helm-chart"})
    client = PushJiraClient("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.pushed == 1
    assert client.update_calls == [("SAT-1", {"customfield_10071": {"value": "helm-chart"}}, False)]


def test_push_refresh_uses_configured_component_field(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "summary", "Changed")
    client = PushJiraClient("2026-07-20T00:00:01.000+0000")
    client.refreshed_issues["SAT-1"] = {
        "key": "SAT-1",
        "fields": {
            "updated": "2026-07-22T10:35:00.000+0000",
            "summary": "Changed",
            "customfield_10071": {"value": "helm-chart"},
        },
    }

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None, component_field="customfield_10071")

    assert result.pushed == 1
    assert (jira_dir / "components/helm-chart/SAT-1/issue.json").exists()
    assert not (jira_dir / "components/api-team/SAT-1/shadow.json").exists()


def test_push_wraps_jira_update_errors_with_key_and_fields(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")
    client = FailingUpdateJiraClient("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.failed == 1
    message = result.errors[0]
    assert "SAT-1" in message
    assert "description" in message
    assert "description=\"Local description\"" in message
    assert "Operation value must be a string" in message


def test_push_fixversion_error_includes_attempted_version_and_hint(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "fixVersions", [{"name": "missing version"}])
    client = FailingUpdateJiraClient("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.failed == 1
    message = result.errors[0]
    assert "SAT-1: Jira update failed for fields [fixVersions]" in message
    assert "fixVersions=missing version" in message
    assert "verify the fixVersion exists in Jira and the value matches exactly" in message


def test_push_keeps_shadow_when_post_push_refresh_fails(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "summary", "Changed")
    client = FailingRefreshJiraClient("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.failed == 1
    assert "refresh failed" in result.errors[0]
    assert shadow_path(jira_dir, "SAT-1").exists()


def test_push_shadows_continues_after_item_error(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")
    set_field(jira_dir, "SAT-2", "summary", "Changed summary")

    class FailsFirstUpdateJiraClient(PushJiraClient):
        def update_issue_field(self, key: str, fields: dict[str, Any], notify_users: bool = True) -> None:
            if key == "SAT-1":
                raise RuntimeError("first failed")
            super().update_issue_field(key, fields, notify_users)

    events: list[str] = []
    client = FailsFirstUpdateJiraClient(
        {
            "SAT-1": "2026-07-20T00:00:01.000+0000",
            "SAT-2": str(load_shadow(jira_dir, "SAT-2").get("baseUpdated")),
        }
    )

    result = push_shadows(jira_dir, ["SAT-1", "SAT-2"], client, progress=events.append)

    assert result.failed == 1
    assert result.pushed == 1
    assert any("SAT-1: failed:" in event for event in events)
    assert ("SAT-2", {"summary": "Changed summary"}, False) in client.update_calls


def test_push_shadows_orders_parent_before_child(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-371/issue.json",
        {"key": "SAT-371", "fields": {"updated": "2026-07-20T00:00:01.000+0000"}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-593/issue.json",
        {"key": "SAT-593", "fields": {"updated": "2026-07-20T00:00:02.000+0000"}},
    )
    set_field(tmp_path, "SAT-371", "type", "Epic")
    set_field(tmp_path, "SAT-593", "parent", {"key": "SAT-371"})
    client = PushJiraClient(
        {
            "SAT-371": "2026-07-20T00:00:01.000+0000",
            "SAT-593": "2026-07-20T00:00:02.000+0000",
        }
    )
    events: list[str] = []

    result = push_shadows(tmp_path, ["SAT-593", "SAT-371"], client, progress=events.append)

    assert result.pushed == 2
    assert events.index("SAT-371: pushing (1/2)") < events.index("SAT-593: pushing (2/2)")
    assert client.update_calls[0] == ("SAT-371", {"issuetype": {"name": "Epic"}}, False)
    assert client.update_calls[1] == ("SAT-593", {"parent": {"key": "SAT-371"}}, False)


def test_push_shadows_blocks_child_when_parent_dependency_does_not_push(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-371/issue.json",
        {"key": "SAT-371", "fields": {"updated": "2026-07-20T00:00:01.000+0000"}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-593/issue.json",
        {"key": "SAT-593", "fields": {"updated": "2026-07-20T00:00:02.000+0000"}},
    )
    set_field(tmp_path, "SAT-371", "type", "Epic")
    set_field(tmp_path, "SAT-593", "parent", {"key": "SAT-371"})
    client = PushJiraClient(
        {
            "SAT-371": "2026-07-21T00:00:01.000+0000",
            "SAT-593": "2026-07-20T00:00:02.000+0000",
        }
    )
    events: list[str] = []

    result = push_shadows(tmp_path, ["SAT-593", "SAT-371"], client, progress=events.append)

    assert result.pushed == 0
    assert result.blocked == 2
    assert "SAT-371: skipped, remote changed since local edits" in events
    assert "SAT-593: blocked, dependency did not push: SAT-371" in events
    assert client.update_calls == []


def test_push_shadows_allows_child_when_unmodified_parent_is_epic(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-371/issue.json",
        {"key": "SAT-371", "fields": {"issuetype": {"name": "Epic"}, "updated": "2026-07-20T00:00:01.000+0000"}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-593/issue.json",
        {"key": "SAT-593", "fields": {"updated": "2026-07-20T00:00:02.000+0000"}},
    )
    set_field(tmp_path, "SAT-593", "parent", {"key": "SAT-371"})
    client = PushJiraClient({"SAT-593": "2026-07-20T00:00:02.000+0000"})

    result = push_shadows(tmp_path, ["SAT-593"], client, progress=None)

    assert result.pushed == 1
    assert client.update_calls == [("SAT-593", {"parent": {"key": "SAT-371"}}, False)]


def test_push_shadows_blocks_child_when_unmodified_parent_is_not_epic(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-371/issue.json",
        {"key": "SAT-371", "fields": {"issuetype": {"name": "Story"}, "updated": "2026-07-20T00:00:01.000+0000"}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-593/issue.json",
        {"key": "SAT-593", "fields": {"updated": "2026-07-20T00:00:02.000+0000"}},
    )
    set_field(tmp_path, "SAT-593", "parent", {"key": "SAT-371"})
    client = PushJiraClient({"SAT-593": "2026-07-20T00:00:02.000+0000"})
    events: list[str] = []

    result = push_shadows(tmp_path, ["SAT-593"], client, progress=events.append)

    assert result.pushed == 0
    assert result.blocked == 1
    assert "SAT-593: blocked, parent SAT-371 is Story, not Epic" in events
    assert client.update_calls == []


def test_push_shadows_blocks_child_when_parent_has_unselected_shadow(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-371/issue.json",
        {"key": "SAT-371", "fields": {"issuetype": {"name": "Story"}, "updated": "2026-07-20T00:00:01.000+0000"}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-593/issue.json",
        {"key": "SAT-593", "fields": {"updated": "2026-07-20T00:00:02.000+0000"}},
    )
    set_field(tmp_path, "SAT-371", "type", "Epic")
    set_field(tmp_path, "SAT-593", "parent", {"key": "SAT-371"})
    client = PushJiraClient({"SAT-593": "2026-07-20T00:00:02.000+0000"})
    events: list[str] = []

    result = push_shadows(tmp_path, ["SAT-593"], client, progress=events.append)

    assert result.pushed == 0
    assert result.blocked == 1
    assert "SAT-593: blocked, parent SAT-371 has unpushed local changes and is not included in this push" in events
    assert client.update_calls == []


def test_push_transitions_status_sets_resolution_and_adds_comment(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "status", "Close")
    set_status_change(jira_dir, "SAT-1", resolution="Won't Do")
    add_comment(jira_dir, "SAT-1", "Closed because this is no longer strategic.")
    client = PushJiraClient("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.pushed == 1
    assert client.transition_calls == [("SAT-1", "Close")]
    assert client.update_calls == [("SAT-1", {"resolution": {"id": "10001"}}, False)]
    assert client.add_comment_calls == [("SAT-1", "Closed because this is no longer strategic.")]


def test_push_removes_parent_with_jira_parent_update_operation(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "parent", None)
    client = PushJiraClient("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.pushed == 1
    assert client.issue_update_calls == [
        ("SAT-1", {}, {"parent": [{"set": {"none": True}}]}, False)
    ]


def test_push_refuses_unsupported_fields(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "fix_version", "1.2.3")
    commit_shadow(jira_dir, "SAT-1")
    client = PushJiraClient("2026-07-20T00:00:01.000+0000")

    result = push_shadows(jira_dir, ["SAT-1"], client, progress=None)

    assert result.failed == 1
    assert "fix_version" in result.errors[0]
