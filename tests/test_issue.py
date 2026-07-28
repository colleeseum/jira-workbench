from __future__ import annotations

import pytest

from jira_workbench.issue import (
    IssueError,
    build_create_fields,
    create_issue,
    fetch_all_field_names,
    fetch_current_user,
    fetch_issue_edit_fields,
    fetch_issue_type_fields,
    issue_component_value,
    resolve_reporter,
)

CREATEMETA = {
    "projects": [
        {
            "key": "SAT",
            "issuetypes": [
                {
                    "name": "Improvement",
                    "fields": {"summary": {}, "description": {}, "project": {}, "issuetype": {}, "reporter": {}},
                },
                {
                    "name": "Bug",
                    "fields": {
                        "summary": {},
                        "project": {},
                        "issuetype": {},
                        "parent": {},
                        "priority": {},
                        "customfield_10071": {},
                    },
                },
            ],
        }
    ]
}


class FakeIssueClient:
    def __init__(
        self, *, createmeta=None, myself=None, created=None, editmeta=None, all_fields=None, raise_on=None
    ) -> None:
        self.createmeta = createmeta if createmeta is not None else CREATEMETA
        self._myself = myself if myself is not None else {"accountId": "me"}
        self.created = created if created is not None else {"key": "SAT-900"}
        self.editmeta = editmeta
        self.all_fields = all_fields
        self.raise_on = raise_on or set()
        self.created_fields: dict[str, object] | None = None

    def issue_createmeta(self, project: str) -> dict[str, object]:
        if "issue_createmeta" in self.raise_on:
            raise RuntimeError("boom")
        return self.createmeta

    def myself(self) -> dict[str, object]:
        if "myself" in self.raise_on:
            raise RuntimeError("boom")
        return self._myself

    def issue_create(self, fields: dict[str, object]) -> dict[str, object]:
        if "issue_create" in self.raise_on:
            raise RuntimeError("boom")
        self.created_fields = fields
        return self.created

    def issue_editmeta(self, key: str) -> dict[str, object]:
        if "issue_editmeta" in self.raise_on:
            raise RuntimeError("boom")
        return self.editmeta

    def get_all_fields(self) -> object:
        if "get_all_fields" in self.raise_on:
            raise RuntimeError("boom")
        return self.all_fields


def test_fetch_issue_type_fields_returns_fields_by_type_name() -> None:
    client = FakeIssueClient()

    result = fetch_issue_type_fields(client, "SAT")

    assert set(result) == {"Improvement", "Bug"}
    assert "reporter" in result["Improvement"]
    assert "parent" in result["Bug"]


def test_fetch_issue_type_fields_wraps_client_exception() -> None:
    client = FakeIssueClient(raise_on={"issue_createmeta"})

    with pytest.raises(IssueError):
        fetch_issue_type_fields(client, "SAT")


def test_fetch_issue_type_fields_handles_malformed_response() -> None:
    client = FakeIssueClient(createmeta={"projects": "not-a-list"})

    with pytest.raises(IssueError):
        fetch_issue_type_fields(client, "SAT")


def test_fetch_issue_type_fields_skips_malformed_issue_type_entries() -> None:
    client = FakeIssueClient(
        createmeta={"projects": [{"issuetypes": ["not-a-dict", {"name": "Task"}, {"fields": {}}]}]}
    )

    result = fetch_issue_type_fields(client, "SAT")

    assert result == {}


def test_fetch_issue_edit_fields_returns_fields_by_id() -> None:
    client = FakeIssueClient(
        editmeta={
            "fields": {
                "duedate": {"name": "Due date", "schema": {"type": "date"}},
                "customfield_10082": {
                    "name": "Customers SAT",
                    "schema": {"type": "array", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:labels"},
                },
            }
        }
    )

    result = fetch_issue_edit_fields(client, "SAT-138")

    assert set(result) == {"duedate", "customfield_10082"}
    assert result["customfield_10082"]["name"] == "Customers SAT"


def test_fetch_issue_edit_fields_wraps_client_exception() -> None:
    client = FakeIssueClient(raise_on={"issue_editmeta"})

    with pytest.raises(IssueError):
        fetch_issue_edit_fields(client, "SAT-138")


def test_fetch_issue_edit_fields_raises_when_response_missing_fields() -> None:
    client = FakeIssueClient(editmeta={"not": "the right shape"})

    with pytest.raises(IssueError):
        fetch_issue_edit_fields(client, "SAT-138")


def test_fetch_all_field_names_returns_id_to_name_map() -> None:
    client = FakeIssueClient(
        all_fields=[
            {"id": "customfield_10082", "name": "Customers SAT", "custom": True},
            {"id": "priority", "name": "Priority", "custom": False},
            {"id": "customfield_bad"},  # missing name -- skipped
            "not-a-dict",  # skipped
        ]
    )

    result = fetch_all_field_names(client)

    assert result == {"customfield_10082": "Customers SAT", "priority": "Priority"}


def test_fetch_all_field_names_wraps_client_exception() -> None:
    client = FakeIssueClient(raise_on={"get_all_fields"})

    with pytest.raises(IssueError):
        fetch_all_field_names(client)


def test_fetch_all_field_names_raises_when_response_is_not_a_list() -> None:
    client = FakeIssueClient(all_fields={"not": "a list"})

    with pytest.raises(IssueError):
        fetch_all_field_names(client)


def test_fetch_current_user_returns_full_response() -> None:
    client = FakeIssueClient(myself={"accountId": "abc123", "displayName": "Jane Doe"})

    assert fetch_current_user(client) == {"accountId": "abc123", "displayName": "Jane Doe"}


def test_fetch_current_user_raises_when_account_id_missing() -> None:
    client = FakeIssueClient(myself={"displayName": "Jane Doe"})

    with pytest.raises(IssueError):
        fetch_current_user(client)


def test_fetch_current_user_wraps_client_exception() -> None:
    client = FakeIssueClient(raise_on={"myself"})

    with pytest.raises(IssueError):
        fetch_current_user(client)


def test_resolve_reporter_returns_account_id() -> None:
    client = FakeIssueClient(myself={"accountId": "abc123"})

    assert resolve_reporter(client) == {"accountId": "abc123"}


def test_resolve_reporter_raises_when_account_id_missing() -> None:
    client = FakeIssueClient(myself={})

    with pytest.raises(IssueError):
        resolve_reporter(client)


def test_resolve_reporter_wraps_client_exception() -> None:
    client = FakeIssueClient(raise_on={"myself"})

    with pytest.raises(IssueError):
        resolve_reporter(client)


def test_issue_component_value_uses_native_components_shape() -> None:
    assert issue_component_value("components", "helm-chart") == [{"name": "helm-chart"}]


def test_issue_component_value_uses_custom_field_shape() -> None:
    assert issue_component_value("customfield_10071", "helm-chart") == {"value": "helm-chart"}


def test_build_create_fields_includes_optional_fields_when_supported() -> None:
    create_fields = {
        "parent": {},
        "priority": {},
        "customfield_10071": {},
        "reporter": {},
        "fixVersions": {},
        "labels": {},
        "assignee": {},
    }

    fields = build_create_fields(
        project="SAT",
        issue_type="Bug",
        summary="Fix it",
        description="Steps",
        component_field="customfield_10071",
        component="helm-chart",
        parent="SAT-100",
        priority="High",
        fix_version="2026.07",
        labels=["infra", "helm"],
        assignee={"accountId": "assignee-id"},
        create_fields=create_fields,
        reporter={"accountId": "me"},
    )

    assert fields == {
        "project": {"key": "SAT"},
        "issuetype": {"name": "Bug"},
        "summary": "Fix it",
        "description": "Steps",
        "reporter": {"accountId": "me"},
        "parent": {"key": "SAT-100"},
        "priority": {"name": "High"},
        "customfield_10071": {"value": "helm-chart"},
        "fixVersions": [{"name": "2026.07"}],
        "labels": ["infra", "helm"],
        "assignee": {"accountId": "assignee-id"},
    }


def test_build_create_fields_rejects_fix_version_when_type_cannot_set_it() -> None:
    with pytest.raises(IssueError):
        build_create_fields(
            project="SAT",
            issue_type="Improvement",
            summary="Fix it",
            description="",
            component_field="components",
            component=None,
            parent=None,
            priority=None,
            fix_version="2026.07",
            create_fields={},
            reporter=None,
        )


def test_build_create_fields_rejects_labels_when_type_cannot_set_it() -> None:
    with pytest.raises(IssueError):
        build_create_fields(
            project="SAT",
            issue_type="Improvement",
            summary="Fix it",
            description="",
            component_field="components",
            component=None,
            parent=None,
            priority=None,
            labels=["infra"],
            create_fields={},
            reporter=None,
        )


def test_build_create_fields_rejects_assignee_when_type_cannot_set_it() -> None:
    with pytest.raises(IssueError):
        build_create_fields(
            project="SAT",
            issue_type="Improvement",
            summary="Fix it",
            description="",
            component_field="components",
            component=None,
            parent=None,
            priority=None,
            assignee={"accountId": "someone"},
            create_fields={},
            reporter=None,
        )


def test_build_create_fields_omits_unset_optional_fields() -> None:
    fields = build_create_fields(
        project="SAT",
        issue_type="Improvement",
        summary="Fix it",
        description="",
        component_field="components",
        component=None,
        parent=None,
        priority=None,
        create_fields={},
        reporter=None,
    )

    assert fields == {"project": {"key": "SAT"}, "issuetype": {"name": "Improvement"}, "summary": "Fix it"}


def test_build_create_fields_rejects_parent_when_type_cannot_set_it() -> None:
    with pytest.raises(IssueError):
        build_create_fields(
            project="SAT",
            issue_type="Improvement",
            summary="Fix it",
            description="",
            component_field="components",
            component=None,
            parent="SAT-100",
            priority=None,
            create_fields={},
            reporter=None,
        )


def test_build_create_fields_rejects_priority_when_type_cannot_set_it() -> None:
    with pytest.raises(IssueError):
        build_create_fields(
            project="SAT",
            issue_type="Improvement",
            summary="Fix it",
            description="",
            component_field="components",
            component=None,
            parent=None,
            priority="High",
            create_fields={},
            reporter=None,
        )


def test_build_create_fields_rejects_component_when_type_cannot_set_it() -> None:
    with pytest.raises(IssueError):
        build_create_fields(
            project="SAT",
            issue_type="Improvement",
            summary="Fix it",
            description="",
            component_field="components",
            component="helm-chart",
            parent=None,
            priority=None,
            create_fields={},
            reporter=None,
        )


def test_create_issue_returns_new_key() -> None:
    client = FakeIssueClient(created={"key": "SAT-901"})

    key = create_issue(client, {"summary": "Fix it"})

    assert key == "SAT-901"
    assert client.created_fields == {"summary": "Fix it"}


def test_create_issue_wraps_client_exception() -> None:
    client = FakeIssueClient(raise_on={"issue_create"})

    with pytest.raises(IssueError):
        create_issue(client, {"summary": "Fix it"})


def test_create_issue_raises_when_response_missing_key() -> None:
    client = FakeIssueClient(created={})

    with pytest.raises(IssueError):
        create_issue(client, {"summary": "Fix it"})
