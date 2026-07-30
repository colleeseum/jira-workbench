from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import DataTable, Input, SelectionList, Static, TextArea

import jira_workbench.service
from jira_workbench.config import load_config
from jira_workbench.metadata import load_versions
from jira_workbench.shadow import add_comment, load_shadow, set_field
from jira_workbench.sync import SyncConfig, build_manifest, read_json, sync_project, write_json
from jira_workbench.view import editable_detail_fields
from jira_workbench.tui.app import JiraWorkbenchApp
from jira_workbench.tui.screens.detail import DetailScreen
from jira_workbench.tui.screens.diff_view import SideBySideDiffScreen
from jira_workbench.tui.screens.filters import FiltersScreen
from jira_workbench.tui.screens.help import HelpScreen
from jira_workbench.tui.screens.index import IndexScreen
from jira_workbench.tui.screens.meta import BoardsScreen, LabelsScreen, MetaScreen, VersionsScreen
from jira_workbench.tui.screens.push_all import PushAllProgressScreen
from jira_workbench.tui.screens.push_review import PushReviewScreen
from test_sync import FakeJiraClient


class SimplePushClient:
    def __init__(self, updated: str) -> None:
        self.updated = updated
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def get_issue(self, issue_id_or_key: str, fields: Any = None, **kwargs: Any) -> Any:
        if fields == "*all":
            return {
                "key": issue_id_or_key,
                "fields": {
                    "updated": f"{self.updated}-refreshed",
                    "summary": "New local summary",
                    "customfield_10071": {"value": "helm-chart"},
                },
            }
        return {"key": issue_id_or_key, "fields": {"updated": self.updated}}

    def get_all_resolutions(self) -> list[dict[str, str]]:
        return []

    def issue_get_comments(self, issue_id: str) -> Any:
        return {"comments": []}

    def issue_transition(self, issue_key: str, status: str) -> None:
        pass

    def issue_update(self, issue_key: str, fields: Any, update: Any = None, **kwargs: Any) -> None:
        pass

    def issue_add_comment(self, issue_key: str, comment: str, visibility: Any = None) -> None:
        pass

    def update_issue_field(self, key: str, fields: dict[str, Any], notify_users: bool = True) -> None:
        self.update_calls.append((key, fields))


class SlowSimplePushClient(SimplePushClient):
    """Adds a small delay so tests can observe the push-in-progress state."""

    def get_issue(self, issue_id_or_key: str, fields: Any = None, **kwargs: Any) -> Any:
        time.sleep(0.05)
        return super().get_issue(issue_id_or_key, fields=fields, **kwargs)


def synced_jira_dir(tmp_path: Path) -> Path:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    return tmp_path


def test_app_is_project_read_only_reflects_the_registry_written_at_sync(tmp_path: Path) -> None:
    from jira_workbench.config import ProjectSettings
    from jira_workbench.metadata import write_project_registry

    write_project_registry(
        tmp_path,
        (ProjectSettings(key="SAT", default=True), ProjectSettings(key="OTHERPROJ", read_only=True)),
    )

    app = JiraWorkbenchApp(tmp_path, component_field="customfield_10071")

    assert app.is_project_read_only("OTHERPROJ") is True
    assert app.is_project_read_only("SAT") is False
    assert app.is_project_read_only(None) is False
    assert app.is_project_read_only("UNKNOWN") is False


@pytest.mark.asyncio
async def test_index_screen_lists_synced_items(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        assert table.row_count == 2
        assert screen._row_keys == ["SAT-1", "SAT-2"]


def _write_assignee_test_item(tmp_path: Path, key: str, assignee_name: str | None) -> None:
    fields: dict[str, Any] = {"summary": key, "issuetype": {"name": "Task"}, "status": {"name": "To Do"}}
    if assignee_name is not None:
        fields["assignee"] = {"displayName": assignee_name}
    write_json(tmp_path / f"components/_unassigned/{key}/issue.json", {"key": key, "fields": fields})


@pytest.mark.asyncio
async def test_index_screen_shows_first_name_only_when_assignee_first_names_are_unique(tmp_path: Path) -> None:
    _write_assignee_test_item(tmp_path, "SAT-1", "Alex Epic")
    _write_assignee_test_item(tmp_path, "SAT-2", "Jordan Chen")
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-1", "Assignee")) == "Alex"
        assert str(table.get_cell("SAT-2", "Assignee")) == "Jordan"


@pytest.mark.asyncio
async def test_index_screen_shows_full_name_when_assignee_first_names_collide(tmp_path: Path) -> None:
    _write_assignee_test_item(tmp_path, "SAT-1", "Alex Smith")
    _write_assignee_test_item(tmp_path, "SAT-2", "Alex Jones")
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-1", "Assignee")) == "Alex Smith"
        assert str(table.get_cell("SAT-2", "Assignee")) == "Alex Jones"


@pytest.mark.asyncio
async def test_enter_opens_detail_screen_with_fields(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.key == "SAT-1"
        table = app.screen.query_one(DataTable)
        field_keys = [table.get_row_at(i) for i in range(table.row_count)]
        assert table.row_count > 0
        assert "Summary for SAT-1" in str(field_keys)


class DevStatusClient:
    def __init__(
        self,
        *,
        summary=None,
        pr_detail=None,
        branch_detail=None,
        raise_error: bool = False,
        editmeta=None,
        raise_on_editmeta: bool = False,
        all_fields=None,
    ) -> None:
        self.summary = summary
        self.pr_detail = pr_detail
        self.branch_detail = branch_detail
        self.raise_error = raise_error
        self.editmeta = editmeta if editmeta is not None else {"fields": {}}
        self.raise_on_editmeta = raise_on_editmeta
        self.all_fields = all_fields if all_fields is not None else []
        self.summary_calls = 0
        self.editmeta_calls = 0
        self.get_all_fields_calls = 0

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "rest/dev-status/1.0/issue/summary":
            self.summary_calls += 1
        if self.raise_error:
            raise RuntimeError("boom")
        if path == "rest/dev-status/1.0/issue/summary":
            return self.summary
        if path == "rest/dev-status/1.0/issue/detail":
            data_type = params.get("dataType") if params else None
            if data_type == "pullrequest":
                return self.pr_detail
            if data_type == "branch":
                return self.branch_detail
        raise AssertionError(f"unexpected call: {path} {params}")

    def issue_editmeta(self, key: str) -> Any:
        self.editmeta_calls += 1
        if self.raise_on_editmeta:
            raise RuntimeError("boom")
        return self.editmeta

    def get_all_fields(self) -> Any:
        self.get_all_fields_calls += 1
        return self.all_fields


APP_TYPE = "oAuth-com.github.integration.production"

LINKED_SUMMARY = {
    "summary": {
        "pullrequest": {"overall": {"count": 1}, "byInstanceType": {APP_TYPE: {"count": 1}}},
        "branch": {"overall": {"count": 1}, "byInstanceType": {APP_TYPE: {"count": 1}}},
    }
}
LINKED_PR_DETAIL = {
    "detail": [
        {
            "pullRequests": [
                {
                    "id": "#15",
                    "name": "Support for hardened image",
                    "status": "OPEN",
                    "url": "https://github.com/stardog-oss/kube-stardog-stack/pull/15",
                    "repositoryName": "stardog-oss/kube-stardog-stack",
                    "repositoryUrl": "https://github.com/stardog-oss/kube-stardog-stack",
                }
            ]
        }
    ]
}
LINKED_BRANCH_DETAIL = {
    "detail": [
        {
            "branches": [
                {
                    "name": "SAT-1-fix",
                    "url": "https://github.com/stardog-oss/kube-stardog-stack/tree/SAT-1-fix",
                    "repository": {"name": "stardog-oss/kube-stardog-stack", "url": "https://github.com/stardog-oss/kube-stardog-stack"},
                }
            ]
        }
    ]
}
EMPTY_SUMMARY = {
    "summary": {
        "pullrequest": {"overall": {"count": 0}, "byInstanceType": {}},
        "branch": {"overall": {"count": 0}, "byInstanceType": {}},
    }
}


def _set_issue_id(jira_dir: Path, key: str, issue_id: str) -> None:
    (issue_path,) = jira_dir.glob(f"components/*/{key}/issue.json")
    issue = read_json(issue_path)
    issue["id"] = issue_id
    write_json(issue_path, issue)


def _set_issue_fields(jira_dir: Path, key: str, fields: dict[str, Any]) -> None:
    (issue_path,) = jira_dir.glob(f"components/*/{key}/issue.json")
    issue = read_json(issue_path)
    issue.setdefault("fields", {}).update(fields)
    write_json(issue_path, issue)


def _app_with_client(jira_dir: Path, client: Any) -> JiraWorkbenchApp:
    app = JiraWorkbenchApp(
        jira_dir,
        component_field="customfield_10071",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )
    app.get_api_client = lambda: client  # type: ignore[method-assign]
    return app


@pytest.mark.asyncio
async def test_detail_screen_shows_development_panel_for_linked_pr_and_branch(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    client = DevStatusClient(summary=LINKED_SUMMARY, pr_detail=LINKED_PR_DETAIL, branch_detail=LINKED_BRANCH_DETAIL)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        dev_widget = app.screen.query_one("#detail-dev", Static)
        content = str(dev_widget.visual)
        assert dev_widget.display is True
        assert "Support for hardened image" in content
        assert "SAT-1-fix" in content


@pytest.mark.asyncio
async def test_detail_screen_hides_development_panel_when_nothing_linked(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    client = DevStatusClient(summary=EMPTY_SUMMARY)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        dev_widget = app.screen.query_one("#detail-dev", Static)
        assert dev_widget.display is False


@pytest.mark.asyncio
async def test_detail_screen_hides_development_panel_without_api_config(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")  # no jira_url/email/token

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        dev_widget = app.screen.query_one("#detail-dev", Static)
        assert dev_widget.display is False


@pytest.mark.asyncio
async def test_detail_screen_survives_dev_status_fetch_failure(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    client = DevStatusClient(raise_error=True)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        dev_widget = app.screen.query_one("#detail-dev", Static)
        assert dev_widget.display is False


@pytest.mark.asyncio
async def test_detail_screen_blocks_field_edits_but_allows_comments_for_a_read_only_project(
    tmp_path: Path,
) -> None:
    from jira_workbench.config import ProjectSettings
    from jira_workbench.metadata import write_project_registry

    jira_dir = synced_jira_dir(tmp_path)
    write_project_registry(jira_dir, (ProjectSettings(key="SAT", read_only=True),))
    client = DevStatusClient()
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("priority"))
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, DetailScreen)  # blocked, no editor opened
        assert load_shadow(jira_dir, "SAT-1") is None

        table.move_cursor(row=table.get_row_index("comments"))
        await pilot.press("enter")
        await pilot.pause()

        from jira_workbench.tui.screens.comments import CommentsScreen

        assert isinstance(app.screen, CommentsScreen)


CUSTOM_LABELS_FIELD_EDITMETA = {
    "fields": {
        "duedate": {"name": "Due date", "schema": {"type": "date"}},
        "customfield_10082": {
            "name": "Customers SAT",
            "schema": {"type": "array", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:labels"},
        },
    }
}


@pytest.mark.asyncio
async def test_detail_screen_remembers_custom_field_names_from_edit_meta(tmp_path: Path) -> None:
    from jira_workbench.metadata import load_field_names

    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    client = DevStatusClient(editmeta=CUSTOM_LABELS_FIELD_EDITMETA)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

    # Persisted locally so an offline CLI `shadow report` run afterward can
    # show the friendly name too, without ever making this API call itself.
    assert load_field_names(jira_dir)["customfield_10082"] == "Customers SAT"


@pytest.mark.asyncio
async def test_detail_screen_remembers_field_names_from_the_whole_instance_not_just_this_issue(
    tmp_path: Path,
) -> None:
    from jira_workbench.metadata import load_field_names

    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    # SAT-1's own edit screen doesn't have customfield_20000 -- but the
    # instance-wide field list does, and that's the one that should win.
    client = DevStatusClient(
        editmeta={"fields": {}},
        all_fields=[{"id": "customfield_20000", "name": "Escalation Owner"}],
    )
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

    assert client.get_all_fields_calls == 1
    assert load_field_names(jira_dir)["customfield_20000"] == "Escalation Owner"


@pytest.mark.asyncio
async def test_detail_screen_caches_dev_status_and_edit_fields_across_reopens(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    client = DevStatusClient(summary=EMPTY_SUMMARY)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert client.summary_calls == 1
        assert client.editmeta_calls == 1
        assert client.get_all_fields_calls == 1

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, IndexScreen)

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        # Reopening the same issue in the same session reuses the cached
        # dev-status/edit-meta/all-fields results instead of fetching them
        # again.
        assert client.summary_calls == 1
        assert client.editmeta_calls == 1
        assert client.get_all_fields_calls == 1


@pytest.mark.asyncio
async def test_detail_screen_shows_epic_children_from_index_items_without_scanning_disk(
    tmp_path: Path, monkeypatch
) -> None:
    # Regression: opening Detail on a parentless issue used to always
    # re-scan every locally synced issue from disk (child_issues) just to
    # find its children -- even though IndexScreen already has every item's
    # shadow-correct "epic" field in memory. Opening from the index must
    # reuse that instead.
    import jira_workbench.view as view_module

    write_json(
        tmp_path / "components/helm-chart/SAT-740/issue.json",
        {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0", "issuetype": {"name": "Epic"}}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-9/issue.json",
        {
            "key": "SAT-9",
            "fields": {
                "summary": "First child",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "parent": {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0"}},
            },
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    def fail_child_issues(*args, **kwargs):
        raise AssertionError("child_issues should not be called when Detail is opened from the index")

    monkeypatch.setattr(view_module, "child_issues", fail_child_issues)

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        table = index.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-740"))
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, DetailScreen)
        header = str(app.screen.query_one("#detail-header").render())
        assert "Epic: SAT-740 RELEASE_V1.2.0" in header
        assert "SAT-9" in header


@pytest.mark.asyncio
async def test_initial_key_opens_detail_with_index_items_without_scanning_disk(tmp_path: Path, monkeypatch) -> None:
    # `jira-wb view SAT-740`-style launch (App's own initial_key) must get
    # the same fast path as opening Detail by hand from the index -- it used
    # to push DetailScreen before IndexScreen's items were ever loaded, so
    # it always fell back to the slow whole-tree child_issues scan for the
    # very first issue shown.
    import jira_workbench.view as view_module

    write_json(
        tmp_path / "components/helm-chart/SAT-740/issue.json",
        {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0", "issuetype": {"name": "Epic"}}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-9/issue.json",
        {
            "key": "SAT-9",
            "fields": {
                "summary": "First child",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "parent": {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0"}},
            },
        },
    )
    build_manifest(tmp_path)

    def fail_child_issues(*args, **kwargs):
        raise AssertionError("child_issues should not be called when opened via initial_key")

    monkeypatch.setattr(view_module, "child_issues", fail_child_issues)

    app = JiraWorkbenchApp(tmp_path, component_field="components", initial_key="SAT-740")

    async with app.run_test():
        assert isinstance(app.screen, DetailScreen)
        header = str(app.screen.query_one("#detail-header").render())
        assert "Epic: SAT-740 RELEASE_V1.2.0" in header
        assert "SAT-9" in header


@pytest.mark.asyncio
async def test_detail_screen_shows_due_date_and_custom_labels_field_once_edit_meta_loads(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    _set_issue_fields(jira_dir, "SAT-1", {"customfield_10082": ["acme"]})
    client = DevStatusClient(editmeta=CUSTOM_LABELS_FIELD_EDITMETA)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("duedate", "value")) == "(none)"
        assert str(table.get_cell("customfield_10082", "value")) == " acme "


@pytest.mark.asyncio
async def test_detail_screen_renders_labels_fix_versions_and_components_as_pills(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_fields(jira_dir, "SAT-1", {"labels": ["urgent", "flaky"]})
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("fixVersions", "value")) == " helm-chart-sa 3.4.4 "
        assert str(table.get_cell("customfield_10071", "value")) == " API Team "
        assert str(table.get_cell("labels", "value")) == " urgent   flaky "


def test_option_picker_screen_hides_toggle_binding_unless_on_toggle_is_given() -> None:
    from jira_workbench.tui.widgets.prompts import OptionPickerScreen

    plain = OptionPickerScreen("Priority:", ["High", "Low"])
    assert plain.check_action("toggle_extra", ()) is False

    toggleable = OptionPickerScreen("Version:", ["a"], on_toggle=lambda include: ["a", "b"])
    assert toggleable.check_action("toggle_extra", ()) is True


@pytest.mark.asyncio
async def test_detail_screen_version_picker_is_scoped_to_project_and_component_filter(tmp_path: Path) -> None:
    from jira_workbench.tui.widgets.prompts import OptionPickerScreen

    jira_dir = synced_jira_dir(tmp_path)  # SAT-1's component is "API Team"
    write_json(
        jira_dir / "meta/SAT/versions.json",
        {
            "versions": [
                {"id": "1", "name": "api-team-sa 2026.07"},
                {"id": "2", "name": "unrelated-sa 2026.07"},
            ]
        },
    )
    write_json(jira_dir / "meta/PLAT/versions.json", {"versions": [{"id": "3", "name": "PLAT 2026.08"}]})
    app = JiraWorkbenchApp(
        jira_dir,
        component_field="customfield_10071",
        version_filters_by_component={"SAT": {"api team": "api-team-sa"}},
    )

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("fixVersions"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, OptionPickerScreen)
        assert picker._options == ["(none)", "api-team-sa 2026.07"]


@pytest.mark.asyncio
async def test_detail_screen_version_picker_toggle_reveals_released_but_not_archived(tmp_path: Path) -> None:
    from jira_workbench.tui.widgets.prompts import OptionPickerScreen

    jira_dir = synced_jira_dir(tmp_path)  # SAT-1
    write_json(
        jira_dir / "meta/SAT/versions.json",
        {
            "versions": [
                {"id": "1", "name": "helm-chart-sa 3.4.2", "released": True},
                {"id": "2", "name": "helm-chart-sa 3.4.4", "archived": True},
                {"id": "3", "name": "helm-chart-sa 3.5.0", "released": False, "archived": False},
            ]
        },
    )
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("fixVersions"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, OptionPickerScreen)
        # Default: only the unreleased-and-unarchived version.
        assert picker._options == ["(none)", "helm-chart-sa 3.5.0"]

        await pilot.press("ctrl+r")
        await pilot.pause()

        # Toggled: released version revealed, archived one still hidden.
        assert picker._options == ["(none)", "helm-chart-sa 3.4.2", "helm-chart-sa 3.5.0"]

        await pilot.press("ctrl+r")
        await pilot.pause()

        # Toggled back off.
        assert picker._options == ["(none)", "helm-chart-sa 3.5.0"]

        # The checkbox is a real, focusable widget -- clickable with the
        # mouse and reachable via Tab, not just the ctrl+r shortcut.
        await pilot.click("#picker-toggle")
        await pilot.pause()
        assert picker._options == ["(none)", "helm-chart-sa 3.4.2", "helm-chart-sa 3.5.0"]
        await pilot.click("#picker-toggle")
        await pilot.pause()
        assert picker._options == ["(none)", "helm-chart-sa 3.5.0"]

        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.focus()
        filter_input.value = "helm-chart-sa 3.5.0"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, DetailScreen)
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("fixVersions", "value")) == " helm-chart-sa 3.5.0 "


@pytest.mark.asyncio
async def test_detail_screen_fix_version_pill_shows_current_name_not_stale_embedded_one(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_fields(jira_dir, "SAT-1", {"fixVersions": [{"id": "10000", "name": "helm-chart-sa 3.4.4"}]})
    write_json(jira_dir / "meta/SAT/versions.json", {"versions": [{"id": "10000", "name": "helm-chart-sa 3.5.0"}]})
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("fixVersions", "value")) == " helm-chart-sa 3.5.0 "


@pytest.mark.asyncio
async def test_detail_screen_shows_linked_issues_as_read_only_pills(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_fields(
        jira_dir,
        "SAT-1",
        {
            "issuelinks": [
                {"type": {"outward": "blocks", "inward": "is blocked by"}, "outwardIssue": {"key": "SAT-900"}},
                {"type": {"outward": "blocks", "inward": "is blocked by"}, "inwardIssue": {"key": "SAT-100"}},
            ]
        },
    )
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("link:blocks", "value")) == " SAT-900 "
        assert str(table.get_cell("link:is blocked by", "value")) == " SAT-100 "

        table.move_cursor(row=table.get_row_index("link:blocks"))
        await pilot.press("enter")
        await pilot.pause()

        # Read-only for now -- selecting it just notifies, no editor opens.
        assert isinstance(app.screen, DetailScreen)


@pytest.mark.asyncio
async def test_detail_screen_has_no_linked_issues_row_when_there_are_none(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        table = app.screen.query_one(DataTable)
        with pytest.raises(Exception):
            table.get_row_index("link:blocks")


@pytest.mark.asyncio
async def test_detail_screen_sets_and_clears_due_date(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    client = DevStatusClient(editmeta=CUSTOM_LABELS_FIELD_EDITMETA)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("duedate"))
        await pilot.press("enter")
        await pilot.pause()

        prompt = app.screen.query_one(Input)
        prompt.value = "2026-08-01"
        await pilot.press("enter")
        await pilot.pause()

        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert shadow["fields"]["duedate"] == "2026-08-01"

        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("duedate", "value")) == "2026-08-01"

        table.move_cursor(row=table.get_row_index("duedate"))
        await pilot.press("enter")
        await pilot.pause()
        prompt = app.screen.query_one(Input)
        # Blank submission is indistinguishable from Esc-cancel at the widget
        # level (TextPromptScreen dismisses both as None) -- same as every
        # other "(none)" convention in this app, typing it explicitly is how
        # you clear a field, not leaving the prompt blank.
        prompt.value = "(none)"
        await pilot.press("enter")
        await pilot.pause()

        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert shadow["fields"]["duedate"] is None


@pytest.mark.asyncio
async def test_detail_screen_rejects_invalid_due_date_format(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    client = DevStatusClient(editmeta=CUSTOM_LABELS_FIELD_EDITMETA)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("duedate"))
        await pilot.press("enter")
        await pilot.pause()

        prompt = app.screen.query_one(Input)
        prompt.value = "not-a-date"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, DetailScreen)
        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is None


@pytest.mark.asyncio
async def test_detail_screen_edits_custom_labels_type_field_via_picker(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    _set_issue_fields(jira_dir, "SAT-1", {"customfield_10082": ["acme"]})
    client = DevStatusClient(editmeta=CUSTOM_LABELS_FIELD_EDITMETA)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("customfield_10082"))
        await pilot.press("enter")
        await pilot.pause()

        from jira_workbench.tui.widgets.prompts import LabelsPickerScreen

        picker = app.screen
        assert isinstance(picker, LabelsPickerScreen)
        selection_list = picker.query_one(SelectionList)
        assert selection_list.selected == ["acme"]
        selection_list.select("globex")  # a genuinely new label typed via the "add" input isn't needed here
        picker.query_one("#new-label-input", Input).focus()
        await pilot.pause()

        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, DetailScreen)
        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert sorted(shadow["fields"]["customfield_10082"]) == ["acme", "globex"]


@pytest.mark.asyncio
async def test_labels_picker_saves_a_typed_new_label_even_without_pressing_enter_first(tmp_path: Path) -> None:
    # Regression: Ctrl+S (or the Save button) used to only look at the
    # SelectionList's own selection, silently dropping whatever was still
    # sitting, uncommitted, in the "add a new label" input if the user typed
    # a name and went straight to Save instead of pressing Enter first.
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    _set_issue_fields(jira_dir, "SAT-1", {"customfield_10082": ["acme"]})
    client = DevStatusClient(editmeta=CUSTOM_LABELS_FIELD_EDITMETA)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("customfield_10082"))
        await pilot.press("enter")
        await pilot.pause()

        from jira_workbench.tui.widgets.prompts import LabelsPickerScreen

        picker = app.screen
        assert isinstance(picker, LabelsPickerScreen)
        # The "add a new label" input has focus by default (same convention
        # as OptionPickerScreen's own filter input) -- typing goes straight
        # there with no extra click/Tab needed.
        assert app.focused is picker.query_one("#new-label-input", Input)
        picker.query_one("#new-label-input", Input).value = "urgent"

        await pilot.press("ctrl+s")  # no Enter first
        await pilot.pause()

        assert isinstance(app.screen, DetailScreen)
        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert sorted(shadow["fields"]["customfield_10082"]) == ["acme", "urgent"]


@pytest.mark.asyncio
async def test_detail_screen_degrades_gracefully_when_edit_meta_fetch_fails(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    client = DevStatusClient(raise_on_editmeta=True)
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        table = app.screen.query_one(DataTable)
        # Neither dynamic field shows up when the live fetch fails...
        with pytest.raises(Exception):
            table.get_row_index("duedate")

        # ...but native Labels editing is unaffected -- it's always considered
        # editable regardless of whether edit metadata was ever fetched.
        assert "labels" in editable_detail_fields("customfield_10071")


@pytest.mark.asyncio
async def test_detail_screen_without_api_config_has_no_dynamic_fields(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")  # no jira_url/email/token

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)

        table = app.screen.query_one(DataTable)
        with pytest.raises(Exception):
            table.get_row_index("duedate")


@pytest.mark.asyncio
async def test_mouse_click_on_row_opens_detail(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        table = app.screen.query_one(DataTable)
        # x=5 lands inside the Key column's text, past the narrow leading
        # type-icon column and its padding -- a single click there must
        # open the item even though the default cursor starts in a
        # different column (see ClickableRowDataTable).
        await pilot.click(DataTable, offset=(5, table.header_height))
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)


@pytest.mark.asyncio
async def test_mouse_click_on_second_row_opens_it_on_first_click(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        table = app.screen.query_one(DataTable)
        # Row 1 (SAT-2) is neither the default cursor's row nor column --
        # a single click must still open it, not just move the cursor there.
        await pilot.click(DataTable, offset=(5, table.header_height + 1))
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.key == "SAT-2"


async def _send_real_click(app: JiraWorkbenchApp, widget, offset: tuple[int, int]) -> None:
    # pilot.click() deliberately bypasses App.on_event (see its own
    # docstring: "This method bypasses the normal event processing in
    # App.on_event") -- it constructs MouseDown/MouseUp and forwards them
    # straight to the screen. That's exactly the code path this test needs
    # to NOT use, since the focus-restoring-click fix lives in on_event
    # itself. This helper instead drives a MouseDown+MouseUp pair through
    # app.on_event(...) directly, matching how a real terminal driver's
    # input actually flows in production.
    from textual import events

    x, y = widget.region.offset + offset
    kwargs = dict(
        widget=widget,
        x=x,
        y=y,
        delta_x=0,
        delta_y=0,
        button=1,
        shift=False,
        meta=False,
        ctrl=False,
        screen_x=x,
        screen_y=y,
    )
    await app.on_event(events.MouseDown(**kwargs))
    await app.on_event(events.MouseUp(**kwargs))


@pytest.mark.asyncio
async def test_click_that_restores_terminal_focus_does_not_also_act_on_the_row(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        table = app.screen.query_one(DataTable)
        app.app_focus = False  # simulate the terminal window not having OS focus

        await _send_real_click(app, table, (5, table.header_height))
        await pilot.pause()

        # the click restored focus, but must not ALSO have opened the row
        assert app.app_focus is True
        assert isinstance(app.screen, IndexScreen)

        # a second, now-genuinely-focused click on the same spot behaves normally
        await _send_real_click(app, table, (5, table.header_height))
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.key == "SAT-1"


@pytest.mark.asyncio
async def test_edit_summary_field_updates_shadow_and_index_marker(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        detail = app.screen
        assert isinstance(detail, DetailScreen)
        table = detail.query_one(DataTable)
        table.cursor_coordinate = table.cursor_coordinate.__class__(row=0, column=0)
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert row_key.value == "summary"
        await pilot.press("enter")
        await pilot.pause()

        prompt = app.screen
        input_widget = prompt.query_one(Input)
        input_widget.value = "New local summary"
        await pilot.press("enter")
        await pilot.pause()

        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert shadow["fields"]["summary"] == "New local summary"

        await pilot.press("escape")
        await pilot.pause()
        index = app.screen
        assert isinstance(index, IndexScreen)
        index_table = index.query_one(DataTable)
        assert index_table.get_cell("SAT-1", "Key") == "SAT-1*"


@pytest.mark.asyncio
async def test_edit_summary_field_save_button_updates_shadow(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        table = app.screen.query_one(DataTable)
        table.cursor_coordinate = table.cursor_coordinate.__class__(row=0, column=0)
        await pilot.press("enter")
        await pilot.pause()

        input_widget = app.screen.query_one(Input)
        input_widget.value = "Saved via button"
        await pilot.click("#save-button")
        await pilot.pause()

        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert shadow["fields"]["summary"] == "Saved via button"
        assert isinstance(app.screen, DetailScreen)


@pytest.mark.asyncio
async def test_revert_confirm_cancel_button_makes_no_change(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "summary", "Local edit")
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("r")
        await pilot.pause()

        await pilot.click("#cancel-button")
        await pilot.pause()

        assert isinstance(app.screen, DetailScreen)
        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert shadow["fields"]["summary"] == "Local edit"


class RefreshFromJiraClient:
    def __init__(self, *, fresh_summary: str) -> None:
        self.fresh_summary = fresh_summary
        self.get_issue_calls = 0

    def get_issue(self, issue_id_or_key: str, fields: Any = None, **kwargs: Any) -> Any:
        self.get_issue_calls += 1
        return {
            "key": issue_id_or_key,
            "fields": {
                "summary": self.fresh_summary,
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "customfield_10071": {"value": "API Team"},
            },
        }


@pytest.mark.asyncio
async def test_detail_screen_refresh_from_jira_pulls_fresh_data_and_keeps_shadow(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description edit")
    client = RefreshFromJiraClient(fresh_summary="Renamed upstream in Jira")
    app = _app_with_client(jira_dir, client)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("R")
        await pilot.pause()

        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("summary", "value")) == "Renamed upstream in Jira"

    assert client.get_issue_calls == 1
    shadow = load_shadow(jira_dir, "SAT-1")
    assert shadow is not None
    assert shadow["fields"]["description"] == "Local description edit"


@pytest.mark.asyncio
async def test_detail_preview_lines_defaults_to_ten_for_each_expandable_field(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    issue_path = jira_dir / "components/api-team/SAT-1/issue.json"
    issue = json.loads(issue_path.read_text())
    issue["fields"]["description"] = "\n".join(f"line {i}" for i in range(40))
    issue_path.write_text(json.dumps(issue))
    add_comment(jira_dir, "SAT-1", "\n".join(f"comment line {i}" for i in range(40)))
    # Description and Comments each get the full preview_lines budget --
    # they don't split it, even when both are shown.
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        table = app.screen.query_one(DataTable)
        assert table.ordered_rows[table.get_row_index("description")].height == 10
        assert table.ordered_rows[table.get_row_index("comments")].height == 10


@pytest.mark.asyncio
async def test_detail_preview_lines_configurable_via_app(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    issue_path = jira_dir / "components/api-team/SAT-1/issue.json"
    issue = json.loads(issue_path.read_text())
    issue["fields"]["description"] = "\n".join(f"line {i}" for i in range(40))
    issue_path.write_text(json.dumps(issue))
    add_comment(jira_dir, "SAT-1", "\n".join(f"comment line {i}" for i in range(40)))
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071", preview_lines=20)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        table = app.screen.query_one(DataTable)
        assert table.ordered_rows[table.get_row_index("description")].height == 20
        assert table.ordered_rows[table.get_row_index("comments")].height == 20


@pytest.mark.asyncio
async def test_view_field_shows_full_text_read_only_without_editing(tmp_path: Path) -> None:
    from textual.widgets import TextArea

    from jira_workbench.tui.widgets.prompts import TextViewScreen

    jira_dir = synced_jira_dir(tmp_path)
    issue_path = jira_dir / "components/api-team/SAT-1/issue.json"
    issue = json.loads(issue_path.read_text())
    long_description = "\n".join(f"line {i}" for i in range(40))
    issue["fields"]["description"] = long_description
    issue_path.write_text(json.dumps(issue))
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        detail = app.screen
        assert isinstance(detail, DetailScreen)
        table = detail.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("description"))

        await pilot.press("x")
        await pilot.pause()

        assert isinstance(app.screen, TextViewScreen)
        text_area = app.screen.query_one(TextArea)
        assert text_area.read_only is True
        # Full content is present -- not silently truncated at a fixed line count.
        assert "line 0" in text_area.text
        assert "line 39" in text_area.text

        await pilot.click("#close-button")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        # Purely read-only: viewing the field must not create a shadow.
        assert load_shadow(jira_dir, "SAT-1") is None


@pytest.mark.asyncio
async def test_enter_on_description_opens_viewer_then_edit_then_save_in_place(tmp_path: Path) -> None:
    from textual.widgets import TextArea

    from jira_workbench.tui.widgets.prompts import TextViewScreen

    jira_dir = synced_jira_dir(tmp_path)
    issue_path = jira_dir / "components/api-team/SAT-1/issue.json"
    issue = json.loads(issue_path.read_text())
    issue["fields"]["description"] = "original description"
    issue_path.write_text(json.dumps(issue))
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("description"))

        # Enter (not just 'x') opens the same viewer, starting read-only.
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TextViewScreen)
        text_area = app.screen.query_one(TextArea)
        assert text_area.read_only is True

        # Clicking Edit switches the SAME screen/textarea to editable, in place.
        await pilot.click("#primary-button")
        await pilot.pause()
        assert isinstance(app.screen, TextViewScreen)
        assert text_area.read_only is False
        assert app.screen.query_one("#primary-button").label == "Save (Ctrl+S)"

        # Button.press() ignores a second click while its 0.2s "active" flash
        # from the first click is still showing -- wait it out before clicking again.
        await pilot.pause(0.25)
        text_area.text = "edited description"
        await pilot.click("#primary-button")
        await pilot.pause()

        # Back at Detail without a trip through the index list.
        assert isinstance(app.screen, DetailScreen)
        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert shadow["fields"]["description"] == "edited description"


@pytest.mark.asyncio
async def test_comments_screen_lists_add_edit_delete_undelete(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.comments import CommentsScreen

    jira_dir = synced_jira_dir(tmp_path)
    comments_path = jira_dir / "components/api-team/SAT-1/comments.json"
    write_json(
        comments_path,
        {
            "comments": [
                {
                    "id": "10001",
                    "author": {"displayName": "Jane"},
                    "created": "2026-01-01T00:00:00.000+0000",
                    "body": "Original comment",
                }
            ]
        },
    )
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("comments"))

        # Enter opens the per-comment screen, not a single blob.
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, CommentsScreen)
        comments_screen = app.screen
        comments_table = comments_screen.query_one(DataTable)
        assert comments_table.row_count == 1
        assert comments_screen._rows[0]["body"] == "Original comment"
        assert comments_screen._rows[0]["state"] == "synced"

        # Add a new (local, unpushed) comment.
        await pilot.press("n")
        await pilot.pause()
        input_widget = app.screen.query_one(TextArea)
        input_widget.text = "New local comment"
        await pilot.click("#save-button")
        await pilot.pause()
        assert comments_table.row_count == 2
        assert any(row["state"] == "local-new" and row["body"] == "New local comment" for row in comments_screen._rows)

        # Edit the already-synced remote comment via the same View screen used for
        # Description: opens read-only with Edit/Close, clicking Edit switches the
        # same screen to Save/Close in place -- queued, not mutated in place.
        from jira_workbench.tui.widgets.prompts import TextViewScreen

        comments_table.move_cursor(
            row=next(i for i, row in enumerate(comments_screen._rows) if row["id"] == "10001")
        )
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TextViewScreen)
        view_textarea = app.screen.query_one("#view-textarea", TextArea)
        assert view_textarea.read_only is True

        await pilot.click("#primary-button")
        await pilot.pause()
        assert view_textarea.read_only is False
        await pilot.pause(0.25)  # let the button's press-flash clear before clicking it again
        view_textarea.text = "Edited comment"
        await pilot.click("#primary-button")
        await pilot.pause()

        assert isinstance(app.screen, CommentsScreen)
        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow is not None
        assert shadow["commentEdits"] == {"10001": "Edited comment"}
        edited_row = next(row for row in comments_screen._rows if row["id"] == "10001")
        assert edited_row["state"] == "edited"
        assert edited_row["body"] == "Edited comment"

        # Delete it -- queued for deletion on push, not removed locally.
        comments_table.move_cursor(
            row=next(i for i, row in enumerate(comments_screen._rows) if row["id"] == "10001")
        )
        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#confirm-button")
        await pilot.pause()
        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow["commentDeletes"] == ["10001"]
        # Deleting clears any pending edit for the same comment.
        assert "10001" not in shadow.get("commentEdits", {})
        deleted_row = next(row for row in comments_screen._rows if row["id"] == "10001")
        assert deleted_row["state"] == "pending-delete"

        # Undelete puts it back to normal.
        comments_table.move_cursor(
            row=next(i for i, row in enumerate(comments_screen._rows) if row["id"] == "10001")
        )
        await pilot.press("d")
        await pilot.pause()
        shadow = load_shadow(jira_dir, "SAT-1")
        assert shadow.get("commentDeletes", []) == []

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)


@pytest.mark.asyncio
async def test_help_screen_opens_and_closes(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("h")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, IndexScreen)


@pytest.mark.asyncio
async def test_resize_does_not_crash(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.resize_terminal(40, 12)
        await pilot.pause()
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert isinstance(app.screen, IndexScreen)


def _write_collapse_test_items(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {"summary": "Helm one", "issuetype": {"name": "Task"}, "status": {"name": "To Do"}},
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {"summary": "Helm two", "issuetype": {"name": "Task"}, "status": {"name": "To Do"}},
        },
    )
    write_json(
        tmp_path / "components/terraform/SAT-3/issue.json",
        {
            "key": "SAT-3",
            "fields": {"summary": "Terraform one", "issuetype": {"name": "Task"}, "status": {"name": "To Do"}},
        },
    )
    build_manifest(tmp_path)


@pytest.mark.asyncio
async def test_toggle_lane_collapses_and_expands_component_swimlane(tmp_path: Path) -> None:
    _write_collapse_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "component"
        screen._rebuild_table()
        await pilot.pause()

        table = screen.query_one(DataTable)
        assert table.row_count == 5  # 2 lane headers + 3 items

        table.move_cursor(row=table.get_row_index("__lane__::helm-chart"))
        await pilot.press("z")
        await pilot.pause()

        assert table.row_count == 3  # helm-chart's 2 items now hidden, terraform lane untouched
        assert str(table.get_cell("__lane__::helm-chart", "Summary")) == "▶ helm-chart  (2)"
        # cursor stays on the collapsed lane's own header row
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert row_key.value == "__lane__::helm-chart"

        await pilot.press("z")
        await pilot.pause()
        assert table.row_count == 5
        assert str(table.get_cell("__lane__::helm-chart", "Summary")) == "▼ helm-chart"


@pytest.mark.asyncio
async def test_toggle_lane_from_an_item_row_collapses_its_own_lane(tmp_path: Path) -> None:
    _write_collapse_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "component"
        screen._rebuild_table()
        await pilot.pause()

        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-1"))
        await pilot.press("z")
        await pilot.pause()

        assert table.row_count == 3
        assert str(table.get_cell("__lane__::helm-chart", "Summary")) == "▶ helm-chart  (2)"


@pytest.mark.asyncio
async def test_toggle_lane_is_noop_without_swimlane_grouping(tmp_path: Path) -> None:
    _write_collapse_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        assert screen.current_swimlane == "none"
        table = screen.query_one(DataTable)
        before = table.row_count

        await pilot.press("z")
        await pilot.pause()

        assert table.row_count == before


@pytest.mark.asyncio
async def test_cycle_swimlane_resets_collapsed_lanes(tmp_path: Path) -> None:
    _write_collapse_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "component"
        screen._rebuild_table()
        await pilot.pause()
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("__lane__::helm-chart"))
        await pilot.press("z")
        await pilot.pause()
        assert screen._collapsed_lanes

        await pilot.press("S")
        await pilot.pause()

        assert screen._collapsed_lanes == set()


@pytest.mark.asyncio
async def test_toggle_all_lanes_collapses_and_expands_every_lane(tmp_path: Path) -> None:
    _write_collapse_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "component"
        screen._rebuild_table()
        await pilot.pause()
        table = screen.query_one(DataTable)
        assert table.row_count == 5  # 2 lane headers + 3 items

        await pilot.press("Z")
        await pilot.pause()

        assert table.row_count == 2  # only the two lane headers remain
        assert str(table.get_cell("__lane__::helm-chart", "Summary")) == "▶ helm-chart  (2)"
        assert str(table.get_cell("__lane__::terraform", "Summary")) == "▶ terraform  (1)"

        await pilot.press("Z")
        await pilot.pause()

        assert table.row_count == 5
        assert str(table.get_cell("__lane__::helm-chart", "Summary")) == "▼ helm-chart"


@pytest.mark.asyncio
async def test_toggle_all_lanes_collapses_remaining_lanes_when_one_already_collapsed(tmp_path: Path) -> None:
    # "collapse all" should fire (not be mistaken for "expand all") when only
    # some lanes are currently collapsed, not just when none are.
    _write_collapse_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "component"
        screen._rebuild_table()
        await pilot.pause()
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("__lane__::helm-chart"))
        await pilot.press("z")
        await pilot.pause()
        assert table.row_count == 3

        await pilot.press("Z")
        await pilot.pause()

        assert table.row_count == 2
        assert str(table.get_cell("__lane__::terraform", "Summary")) == "▶ terraform  (1)"


@pytest.mark.asyncio
async def test_toggle_all_lanes_is_noop_without_swimlane_grouping(tmp_path: Path) -> None:
    _write_collapse_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        assert screen.current_swimlane == "none"
        table = screen.query_one(DataTable)
        before = table.row_count

        await pilot.press("Z")
        await pilot.pause()

        assert table.row_count == before


@pytest.mark.asyncio
async def test_index_table_shows_blank_component_for_unassigned_items(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/_unassigned/SAT-9/issue.json",
        {"key": "SAT-9", "fields": {"summary": "No component", "issuetype": {"name": "Task"}, "status": {"name": "To Do"}}},
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        await pilot.pause()

        assert str(table.get_cell("SAT-9", "Component")) == ""


def test_index_screen_seeds_filters_from_constructor_params() -> None:
    screen = IndexScreen(
        component="helm-chart",
        fix_version="2026.07",
        assignee="you@example.com",
        board="SAT board",
        board_scope="active",
        pattern="prometheus",
        active=False,
        swimlane="component",
    )

    assert screen.field_filters == {
        "component": ["helm-chart"],
        "fixVersion": ["2026.07"],
        "assignee": ["you@example.com"],
    }
    assert screen.board == "SAT board"
    assert screen.board_scope == "active"
    assert screen.current_filter == "prometheus"
    assert screen.active_only is False
    assert screen.current_swimlane == "component"


def test_index_screen_ignores_board_scope_without_a_board() -> None:
    screen = IndexScreen(board_scope="active")

    assert screen.board is None
    assert screen.board_scope is None


@pytest.mark.asyncio
async def test_save_filter_writes_current_state_and_round_trips(tmp_path: Path) -> None:
    _write_collapse_test_items(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text('jira_api_token = "secret"\n[view]\n# keep me\npreview_lines = 10\n')
    app = JiraWorkbenchApp(tmp_path, component_field="components", config_path=config_path)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.field_filters = {"component": ["helm-chart"], "project": ["SAT"], "status": ["To Do", "In Progress"]}
        screen.board = "SAT board"
        screen.board_scope = "active"
        screen.current_filter = "prometheus"
        screen.active_only = False
        screen.current_swimlane = "component"

        await pilot.press("s")
        await pilot.pause()

    text = config_path.read_text()
    assert "# keep me" in text
    assert 'jira_api_token = "secret"' in text

    config = load_config(config_path)
    assert config.view_component == ("helm-chart",)
    assert config.view_project == ("SAT",)
    assert config.view_status == ("To Do", "In Progress")
    assert config.view_board == "SAT board"
    assert config.view_board_scope == "active"
    assert config.view_filter == "prometheus"
    assert config.view_swimlane == "component"
    assert config.view_active is False
    assert config.view_preview_lines == 10

    restored = IndexScreen(
        project=config.view_project,
        status=config.view_status,
        component=config.view_component,
        fix_version=config.view_fix_version,
        assignee=config.view_assignee,
        board=config.view_board,
        board_scope=config.view_board_scope,
        pattern=config.view_filter,
        active=config.view_active,
        swimlane=config.view_swimlane,
    )
    assert restored.field_filters == {
        "component": ["helm-chart"],
        "project": ["SAT"],
        "status": ["To Do", "In Progress"],
    }
    assert restored.board == "SAT board"
    assert restored.board_scope == "active"
    assert restored.current_filter == "prometheus"
    assert restored.active_only is False
    assert restored.current_swimlane == "component"


@pytest.mark.asyncio
async def test_save_filter_clears_previously_saved_values_when_unset(tmp_path: Path) -> None:
    _write_collapse_test_items(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[view]\nboard = "SAT board"\nboard_scope = "active"\n')
    app = JiraWorkbenchApp(tmp_path, component_field="components", config_path=config_path)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        assert screen.board is None

        await pilot.press("s")
        await pilot.pause()

    config = load_config(config_path)
    assert config.view_board is None
    assert config.view_board_scope is None


CREATE_TYPES_FULL = {
    "projects": [
        {
            "key": "SAT",
            "issuetypes": [
                {
                    "name": "Task",
                    "fields": {
                        "summary": {},
                        "description": {},
                        "project": {},
                        "issuetype": {},
                        "components": {},
                        "fixVersions": {},
                        "parent": {},
                        "priority": {},
                        "labels": {},
                        "assignee": {},
                        "reporter": {},
                    },
                },
                {"name": "Bug", "fields": {"summary": {}, "project": {}, "issuetype": {}}},
            ],
        }
    ]
}


class FakeCreateIssueClient:
    def __init__(self, *, createmeta=CREATE_TYPES_FULL, new_key: str = "SAT-900") -> None:
        self.createmeta = createmeta
        self.new_key = new_key
        self.created_fields: dict[str, Any] | None = None
        self.create_called = False
        self.createmeta_calls = 0
        self.myself_calls = 0

    def issue_createmeta(self, project: str) -> dict[str, Any]:
        self.createmeta_calls += 1
        return self.createmeta

    def myself(self) -> dict[str, Any]:
        self.myself_calls += 1
        return {"accountId": "me"}

    def issue_create(self, fields: dict[str, Any]) -> dict[str, Any]:
        self.create_called = True
        self.created_fields = fields
        return {"key": self.new_key}

    def get_issue(self, issue_id_or_key: str, fields: Any = None, **kwargs: Any) -> Any:
        components = (self.created_fields or {}).get("components", [])
        return {
            "key": self.new_key,
            "fields": {
                "summary": (self.created_fields or {}).get("summary", "New task"),
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": components,
            },
        }


def _write_epic_test_items(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-5/issue.json",
        {
            "key": "SAT-5",
            "fields": {
                "summary": "Migrate to helm 4",
                "issuetype": {"name": "Epic"},
                "status": {"name": "To Do"},
                "priority": {"name": "High"},
                "fixVersions": [{"name": "2026.07"}],
                "labels": ["infra", "helm"],
                "assignee": {"displayName": "Alex Epic", "accountId": "acc-epic"},
                "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Epic description"}]}]},
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-6/issue.json",
        {
            "key": "SAT-6",
            "fields": {
                "summary": "Chart values",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "parent": {"key": "SAT-5"},
                "labels": ["k8s"],
            },
        },
    )
    build_manifest(tmp_path)


def _issue_create_app(tmp_path: Path, client: FakeCreateIssueClient) -> JiraWorkbenchApp:
    app = JiraWorkbenchApp(
        tmp_path,
        component_field="components",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
        project="SAT",
    )
    app.get_api_client = lambda: client  # type: ignore[method-assign]
    return app


@pytest.mark.asyncio
async def test_new_issue_screen_caches_createmeta_and_current_user_across_reopens(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, IssueCreateScreen)
        assert client.createmeta_calls == 1
        assert client.myself_calls == 1

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, IndexScreen)

        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, IssueCreateScreen)

        # Same project, same session -- reuses the cached createmeta/current
        # user instead of fetching them again.
        assert client.createmeta_calls == 1
        assert client.myself_calls == 1


@pytest.mark.asyncio
async def test_new_issue_blocked_for_a_read_only_project(tmp_path: Path) -> None:
    from jira_workbench.config import ProjectSettings
    from jira_workbench.metadata import write_project_registry

    _write_epic_test_items(tmp_path)
    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("c")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)  # blocked, no create screen opened
        assert client.createmeta_calls == 0


@pytest.mark.asyncio
async def test_new_issue_screen_type_summary_description_have_no_default(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))  # a child of the epic, not the epic itself

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        assert create_screen.selected_type == ""
        assert create_screen.values["summary"] == ""
        assert create_screen.values["description"] == ""

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_screen_prefills_other_fields_from_epic_under_cursor(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))  # a child of the epic, not the epic itself

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        assert create_screen.values["component"] == "helm-chart"  # the epic's own component
        assert create_screen.values["version"] == "2026.07"
        assert create_screen.values["priority"] == "High"
        assert create_screen.values["labels"] == "infra, helm"
        assert create_screen.values["assignee"] == "Alex Epic"
        assert create_screen.values["reporter"] == "me"  # currently logged-in user, not from the epic
        assert create_screen.values["parent"] == "SAT-5"
        assert create_screen._row_value("status") == "To Do"

        assert str(create_screen._row_value("component")) == " helm-chart "
        assert str(create_screen._row_value("version")) == " 2026.07 "
        assert str(create_screen._row_value("labels")) == " infra   helm "

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_screen_status_row_is_fixed_and_not_editable(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_table = create_screen.query_one(DataTable)
        create_table.move_cursor(row=create_table.get_row_index("status"))
        await pilot.press("enter")
        await pilot.pause()

        # Pressing Enter on Status is a no-op -- still on the same screen, no picker opened.
        assert isinstance(app.screen, IssueCreateScreen)
        assert create_screen._row_value("status") == "To Do"

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_labels_picker_preselects_current_labels(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen
    from jira_workbench.tui.widgets.prompts import LabelsPickerScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))  # epic SAT-5 has labels "infra", "helm"

        await pilot.press("c")
        await pilot.pause()
        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_screen.selected_type = "Task"
        create_screen._rebuild_rows()
        await pilot.pause()

        create_table = create_screen.query_one(DataTable)
        create_table.move_cursor(row=create_table.get_row_index("labels"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, LabelsPickerScreen)
        selection_list = picker.query_one(SelectionList)
        assert sorted(selection_list.selected) == ["helm", "infra"]
        assert "k8s" in picker._known  # a label used elsewhere locally, just not on this epic

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_labels_picker_toggle_updates_value(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen
    from jira_workbench.tui.widgets.prompts import LabelsPickerScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("c")
        await pilot.pause()
        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_screen.selected_type = "Task"
        create_screen._rebuild_rows()
        await pilot.pause()

        create_table = create_screen.query_one(DataTable)
        create_table.move_cursor(row=create_table.get_row_index("labels"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, LabelsPickerScreen)
        selection_list = picker.query_one(SelectionList)
        selection_list.deselect("infra")  # untoggle one of the two preselected labels
        selection_list.select("k8s")  # and pick an existing-but-unselected one instead

        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, IssueCreateScreen)
        assert sorted(part.strip() for part in create_screen.values["labels"].split(",")) == ["helm", "k8s"]

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_labels_picker_adds_a_new_label_explicitly(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen
    from jira_workbench.tui.widgets.prompts import LabelsPickerScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("c")
        await pilot.pause()
        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_screen.selected_type = "Task"
        create_screen._rebuild_rows()
        await pilot.pause()

        create_table = create_screen.query_one(DataTable)
        create_table.move_cursor(row=create_table.get_row_index("labels"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, LabelsPickerScreen)
        new_label_input = picker.query_one("#new-label-input", Input)
        new_label_input.focus()
        await pilot.pause()
        new_label_input.value = "brand-new-label"
        await pilot.press("enter")
        await pilot.pause()

        selection_list = picker.query_one(SelectionList)
        assert "brand-new-label" in selection_list.selected

        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, IssueCreateScreen)
        assert "brand-new-label" in create_screen.values["labels"]

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_labels_picker_cancel_keeps_previous_value(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen
    from jira_workbench.tui.widgets.prompts import LabelsPickerScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("c")
        await pilot.pause()
        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_screen.selected_type = "Task"
        create_screen._rebuild_rows()
        await pilot.pause()
        before = create_screen.values["labels"]

        create_table = create_screen.query_one(DataTable)
        create_table.move_cursor(row=create_table.get_row_index("labels"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, LabelsPickerScreen)
        picker.query_one(SelectionList).deselect_all()

        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, IssueCreateScreen)
        assert create_screen.values["labels"] == before


@pytest.mark.asyncio
async def test_new_issue_screen_full_flow_creates_and_refreshes_locally(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        # Type, Summary, and Description are required and have no default --
        # everything else is already prefilled from the epic (SAT-5).
        create_screen.selected_type = "Task"
        create_screen.values["summary"] = "Bump chart version"
        create_screen.values["description"] = "Steps to bump the chart version"
        create_screen._rebuild_rows()

        await pilot.press("p")
        await pilot.pause()
        await pilot.press("y")  # confirm
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert "SAT-900" in app.screen._row_keys

    assert client.create_called is True
    assert client.created_fields["project"] == {"key": "SAT"}
    assert client.created_fields["issuetype"] == {"name": "Task"}
    assert client.created_fields["summary"] == "Bump chart version"
    assert client.created_fields["description"] == "Steps to bump the chart version"
    assert client.created_fields["components"] == [{"name": "helm-chart"}]
    assert client.created_fields["fixVersions"] == [{"name": "2026.07"}]
    assert client.created_fields["priority"] == {"name": "High"}
    assert client.created_fields["labels"] == ["infra", "helm"]
    assert client.created_fields["assignee"] == {"accountId": "acc-epic"}
    assert client.created_fields["parent"] == {"key": "SAT-5"}
    assert client.created_fields["reporter"] == {"accountId": "me"}
    assert "status" not in client.created_fields  # never a create-screen field
    assert (tmp_path / "components/helm-chart/SAT-900/issue.json").exists()


def _write_clone_source_item(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-6/issue.json",
        {
            "key": "SAT-6",
            "fields": {
                "summary": "Chart values",
                "description": "Steps to bump chart values",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "priority": {"name": "High"},
                "components": [{"name": "helm-chart"}],
                "fixVersions": [{"name": "2026.07"}],
                "labels": ["k8s"],
                "assignee": {"displayName": "Alex", "accountId": "acc-1"},
            },
        },
    )
    build_manifest(tmp_path)


@pytest.mark.asyncio
async def test_index_clone_action_prefills_form_and_creates_under_source_project(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen
    from jira_workbench.tui.widgets.prompts import ConfirmScreen

    _write_clone_source_item(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("C")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        assert create_screen.selected_type == "Task"
        assert create_screen.values["summary"] == "Chart values (clone)"
        assert create_screen.values["description"] == "Steps to bump chart values"
        assert create_screen.values["component"] == "helm-chart"
        assert create_screen.values["version"] == "2026.07"
        assert create_screen.values["priority"] == "High"
        assert create_screen.values["labels"] == "k8s"
        assert create_screen.values["assignee"] == "Alex"

        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)

    assert client.create_called is True
    assert client.created_fields["project"] == {"key": "SAT"}
    assert client.created_fields["summary"] == "Chart values (clone)"
    assert client.created_fields["description"] == "Steps to bump chart values"
    assert (tmp_path / "components/helm-chart/SAT-900/issue.json").exists()


@pytest.mark.asyncio
async def test_index_clone_blocked_for_a_read_only_project(tmp_path: Path) -> None:
    from jira_workbench.config import ProjectSettings
    from jira_workbench.metadata import write_project_registry

    _write_clone_source_item(tmp_path)
    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("C")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)  # blocked, no create screen opened
        assert client.createmeta_calls == 0


@pytest.mark.asyncio
async def test_detail_clone_action_opens_prefilled_form_and_new_detail_for_created_key(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen
    from jira_workbench.tui.widgets.prompts import ConfirmScreen

    _write_clone_source_item(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, DetailScreen)

        await pilot.press("C")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        assert create_screen.values["summary"] == "Chart values (clone)"
        assert create_screen._context_label == "Clone of SAT-6"

        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()

        new_detail = app.screen
        assert isinstance(new_detail, DetailScreen)
        assert new_detail.key == "SAT-900"

    assert client.create_called is True


async def _pick_option(pilot, needle: str) -> None:
    screen = pilot.app.screen
    input_widget = screen.query_one("#picker-filter", Input)
    input_widget.value = needle
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_screen_version_picker_is_scoped_to_project_and_component_filter(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen
    from jira_workbench.tui.widgets.prompts import OptionPickerScreen

    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    build_manifest(tmp_path)
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {"versions": [{"id": "1", "name": "helm-chart-sa 3.4.0"}, {"id": "2", "name": "unrelated-sa 1.0.0"}]},
    )
    write_json(tmp_path / "meta/PLAT/versions.json", {"versions": [{"id": "3", "name": "PLAT 2026.08"}]})
    client = FakeCreateIssueClient()
    app = JiraWorkbenchApp(
        tmp_path,
        component_field="components",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
        project="SAT",
        version_filters_by_component={"SAT": {"helm-chart": "helm-chart-sa"}},
    )
    app.get_api_client = lambda: client  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-1"))

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_table = create_screen.query_one(DataTable)
        create_table.move_cursor(row=create_table.get_row_index("type"))
        await pilot.press("enter")
        await pilot.pause()
        await _pick_option(pilot, "Task")

        create_table.move_cursor(row=create_table.get_row_index("component"))
        await pilot.press("enter")
        await pilot.pause()
        await _pick_option(pilot, "helm-chart")
        assert create_screen.values["component"] == "helm-chart"

        create_table.move_cursor(row=create_table.get_row_index("version"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, OptionPickerScreen)
        assert picker._options == ["(none)", "helm-chart-sa 3.4.0"]


@pytest.mark.asyncio
async def test_new_issue_screen_component_picker_uses_configured_custom_field_not_native(tmp_path: Path) -> None:
    # Regression test: the Component picker used to call component_options()
    # unconditionally, which always sources native Jira "components" -- ignoring
    # component_field entirely. When a custom field is configured (as SAT does),
    # it must offer the custom field's own observed values instead, and must NOT
    # offer a value that only exists on the native field (e.g. a component created
    # there by mistake before the custom field was adopted).
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "customfield_10071": {"value": "helm-chart"},
                "components": [{"name": "stray-native-component"}],
            },
        },
    )
    build_manifest(tmp_path)
    createmeta = {
        "projects": [
            {
                "key": "SAT",
                "issuetypes": [
                    {
                        "name": "Task",
                        "fields": {
                            "summary": {},
                            "description": {},
                            "project": {},
                            "issuetype": {},
                            "customfield_10071": {},
                        },
                    }
                ],
            }
        ]
    }
    client = FakeCreateIssueClient(createmeta=createmeta)
    app = JiraWorkbenchApp(
        tmp_path,
        component_field="customfield_10071",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
        project="SAT",
    )
    app.get_api_client = lambda: client  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-1"))

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_table = create_screen.query_one(DataTable)
        create_table.move_cursor(row=create_table.get_row_index("type"))
        await pilot.press("enter")
        await pilot.pause()
        await _pick_option(pilot, "Task")

        create_table.move_cursor(row=create_table.get_row_index("component"))
        await pilot.press("enter")
        await pilot.pause()

        from jira_workbench.tui.widgets.prompts import OptionPickerScreen

        picker = app.screen
        assert isinstance(picker, OptionPickerScreen)
        assert picker._options == ["(none)", "helm-chart"]
        assert "stray-native-component" not in picker._options


@pytest.mark.asyncio
async def test_new_issue_screen_version_picker_toggle_reveals_released_but_not_archived(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen
    from jira_workbench.tui.widgets.prompts import OptionPickerScreen

    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    build_manifest(tmp_path)
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {
            "versions": [
                {"id": "1", "name": "helm-chart-sa 3.4.2", "released": True},
                {"id": "2", "name": "helm-chart-sa 3.4.4", "archived": True},
                {"id": "3", "name": "helm-chart-sa 3.5.0", "released": False, "archived": False},
            ]
        },
    )
    client = FakeCreateIssueClient()
    app = JiraWorkbenchApp(
        tmp_path,
        component_field="components",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
        project="SAT",
    )
    app.get_api_client = lambda: client  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-1"))

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_table = create_screen.query_one(DataTable)
        create_table.move_cursor(row=create_table.get_row_index("type"))
        await pilot.press("enter")
        await pilot.pause()
        await _pick_option(pilot, "Task")

        create_table.move_cursor(row=create_table.get_row_index("version"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, OptionPickerScreen)
        assert picker._options == ["(none)", "helm-chart-sa 3.5.0"]

        await pilot.press("ctrl+r")
        await pilot.pause()

        assert picker._options == ["(none)", "helm-chart-sa 3.4.2", "helm-chart-sa 3.5.0"]


@pytest.mark.asyncio
async def test_new_issue_screen_choosing_type_shows_its_fields_then_changing_it_hides_them(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-6"))

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_table = create_screen.query_one(DataTable)
        assert create_table.row_count == 4  # Summary, Description, Type, Status -- no Type chosen yet

        create_table.move_cursor(row=create_table.get_row_index("type"))
        await pilot.press("enter")
        await pilot.pause()
        await _pick_option(pilot, "Task")

        assert isinstance(app.screen, IssueCreateScreen)
        # + Component, Version, Priority, Labels, Assignee, Reporter, Parent
        assert create_table.row_count == 11

        create_table.move_cursor(row=create_table.get_row_index("type"))
        await pilot.press("enter")
        await pilot.pause()
        await _pick_option(pilot, "Bug")

        assert create_table.row_count == 4  # Bug's create screen supports none of the extra fields
        assert create_screen.values["component"] == ""
        assert create_screen.values["version"] == ""
        assert create_screen.values["priority"] == ""
        assert create_screen.values["labels"] == ""
        assert create_screen.values["assignee"] == ""
        assert create_screen.values["reporter"] == ""
        assert create_screen.values["parent"] == ""

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_requires_type_before_create(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-5"))
        await pilot.press("c")
        await pilot.pause()
        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_screen.values["summary"] = "Something"
        create_screen.values["description"] = "Something else"
        create_screen._rebuild_rows()

        await pilot.press("p")
        await pilot.pause()

        assert isinstance(app.screen, IssueCreateScreen)

        await pilot.press("escape")
        await pilot.pause()

    assert client.create_called is False


@pytest.mark.asyncio
async def test_new_issue_requires_summary_before_create(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-5"))
        await pilot.press("c")
        await pilot.pause()
        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_screen.selected_type = "Task"
        create_screen.values["description"] = "Something else"
        create_screen._rebuild_rows()

        await pilot.press("p")
        await pilot.pause()

        assert isinstance(app.screen, IssueCreateScreen)

        await pilot.press("escape")
        await pilot.pause()

    assert client.create_called is False


@pytest.mark.asyncio
async def test_new_issue_requires_description_before_create(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-5"))
        await pilot.press("c")
        await pilot.pause()
        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        create_screen.selected_type = "Task"
        create_screen.values["summary"] = "Something"
        create_screen._rebuild_rows()

        await pilot.press("p")
        await pilot.pause()

        assert isinstance(app.screen, IssueCreateScreen)

        await pilot.press("escape")
        await pilot.pause()

    assert client.create_called is False


@pytest.mark.asyncio
async def test_new_issue_cancel_does_not_create(tmp_path: Path) -> None:
    _write_epic_test_items(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)

        await pilot.press("c")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)

    assert client.create_called is False


@pytest.mark.asyncio
async def test_new_issue_on_virtual_none_epic_lane_has_no_prefill(tmp_path: Path) -> None:
    from jira_workbench.tui.screens.issue_create import IssueCreateScreen

    _write_epic_test_items(tmp_path)
    write_json(
        tmp_path / "components/helm-chart/SAT-7/issue.json",
        {"key": "SAT-7", "fields": {"summary": "No epic here", "issuetype": {"name": "Task"}, "status": {"name": "To Do"}}},
    )
    build_manifest(tmp_path)
    client = FakeCreateIssueClient()
    app = _issue_create_app(tmp_path, client)

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "epic"
        screen._rebuild_table()
        await pilot.pause()
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("__lane__::(none)"))

        await pilot.press("c")
        await pilot.pause()

        create_screen = app.screen
        assert isinstance(create_screen, IssueCreateScreen)
        assert create_screen.values["component"] == ""
        assert create_screen.values["priority"] == ""
        assert create_screen.values["parent"] == ""

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_issue_without_api_config_notifies_and_does_not_open_any_prompt(tmp_path: Path) -> None:
    _write_epic_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components", project="SAT")  # no jira_url/email/token

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)

        await pilot.press("c")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)

async def test_epic_swimlane_groups_by_epic_key_despite_stale_cached_summary(tmp_path: Path) -> None:
    # Two children of the same epic (SAT-100) with DIFFERENT cached parent.fields.summary
    # text (as happens when the epic gets renamed and only some children are re-synced
    # afterward), interleaved with an unrelated epic in between. Grouping must key off the
    # epic's issue key, not the denormalized summary text, or this either mis-groups or
    # crashes with a DuplicateKey when the same rendered text recurs non-contiguously.
    write_json(
        tmp_path / "components/helm-chart/SAT-100/issue.json",
        {
            "key": "SAT-100",
            "fields": {"summary": "Renamed epic", "issuetype": {"name": "Epic"}, "status": {"name": "In Progress"}},
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Child one",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "parent": {"key": "SAT-100", "fields": {"summary": "Old epic name"}},
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "Child two",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "parent": {"key": "SAT-100", "fields": {"summary": "Renamed epic"}},
            },
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "epic"
        screen._rebuild_table()  # must not raise DuplicateKey
        table = screen.query_one(DataTable)
        # The epic heads its own lane with its own row (no separate divider),
        # so all three issues are real rows and there's exactly one lane.
        lane_rows = table.row_count - len(screen._row_keys)
        assert lane_rows == 0


@pytest.mark.asyncio
async def test_epic_swimlane_lane_label_lives_in_summary_not_key(tmp_path: Path) -> None:
    # Regression: a lane header's label used to be rendered into the Key
    # column, which has no fixed width and never shrinks back down once a
    # DataTable widens it -- a long epic summary would blow out the Key
    # column for every row, real issue keys included. This happens whenever
    # the epic itself was never independently synced (only referenced via
    # a child's own cached parent.fields.summary, real-world-observed at
    # 100+ chars) -- if the epic *is* locally synced, its row is a normal
    # bold item row instead, not this lane-header label path. The label
    # belongs in Summary, which already has to accommodate long text.
    long_summary = "A" * 100
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Child one",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "parent": {"key": "SAT-100", "fields": {"summary": long_summary}},
            },
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "epic"
        screen._rebuild_table()

        table = screen.query_one(DataTable)
        lane_key = "__lane__::SAT-100"
        assert str(table.get_cell(lane_key, "Key")) == ""
        assert long_summary in str(table.get_cell(lane_key, "Summary"))


@pytest.mark.asyncio
async def test_toggle_lane_on_epic_head_hides_children_but_keeps_the_epic_row(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-100/issue.json",
        {"key": "SAT-100", "fields": {"summary": "Epic", "issuetype": {"name": "Epic"}, "status": {"name": "To Do"}}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Child one",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "parent": {"key": "SAT-100", "fields": {"summary": "Epic"}},
            },
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "epic"
        screen._rebuild_table()
        await pilot.pause()
        table = screen.query_one(DataTable)
        assert table.row_count == 2

        table.move_cursor(row=table.get_row_index("SAT-100"))
        await pilot.press("z")
        await pilot.pause()

        # the epic's own row stays -- it's the lane's header, not a child
        assert table.row_count == 1
        assert table.get_row_index("SAT-100") == 0
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert row_key.value == "SAT-100"


@pytest.mark.asyncio
async def test_epic_lane_shows_full_row_even_when_component_filter_excludes_epic(tmp_path: Path) -> None:
    # The epic has no component of its own (common -- epics often aren't
    # scoped to one component), while its child does. A component filter
    # then excludes the epic from the visible list entirely, so it's never
    # seen as its own `item` in the main loop -- the lane header must still
    # fetch and render its real row (icon, state, summary), not fall back
    # to a bare bold label with blank columns.
    write_json(
        tmp_path / "components/_unassigned/SAT-100/issue.json",
        {"key": "SAT-100", "fields": {"summary": "Cross-cutting epic", "issuetype": {"name": "Epic"}, "status": {"name": "To Do"}}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Child one",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
                "parent": {"key": "SAT-100", "fields": {"summary": "Cross-cutting epic"}},
            },
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, IndexScreen)
        screen.current_swimlane = "epic"
        screen.field_filters["component"] = ["helm-chart"]
        screen._rebuild_table()

        assert "SAT-100" in screen._row_keys
        table = screen.query_one(DataTable)
        assert str(table.get_cell("SAT-100", "")) == "◆"
        assert str(table.get_cell("SAT-100", "State")) == "To Do"
        assert str(table.get_cell("SAT-100", "Summary")) == "Cross-cutting epic"
        assert screen._row_keys == ["SAT-100", "SAT-1"]


@pytest.mark.asyncio
async def test_index_table_uses_plain_unicode_icon_by_default(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-1", "")) == "■"  # Task, plain unicode square


@pytest.mark.asyncio
async def test_index_table_uses_nerd_font_icon_when_enabled(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071", nerd_font=True)

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-1", "")) == ""  # fa-square_check


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_index_table_renders_component_and_version_as_pills(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-1", "Component")) == " API Team "
        assert str(table.get_cell("SAT-1", "Version")) == " helm-chart-sa 3.4.4 "


@pytest.mark.asyncio
async def test_index_table_shows_priority_icon_instead_of_text(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/_unassigned/SAT-9/issue.json",
        {
            "key": "SAT-9",
            "fields": {
                "summary": "High priority item",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "priority": {"name": "High"},
            },
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-9", "Priority")) == "↑"


DEV_STATUS_OPEN_PR_RAW = (
    '{pullrequest={dataType=pullrequest, state=OPEN, stateCount=1}, '
    'json={"cachedValue":{"errors":[],"summary":{"pullrequest":{"overall":'
    '{"count":1,"lastUpdated":"2026-04-16T10:16:17.000-0400","stateCount":1,'
    '"state":"OPEN","dataType":"pullrequest","open":true}}}},"isStale":false}}'
)
DEV_STATUS_BRANCH_ONLY_RAW = (
    '{branch={count=1, dataType=branch}, json={"cachedValue":{"errors":[],'
    '"summary":{"branch":{"overall":{"count":1,"lastUpdated":'
    '"2026-05-01T11:02:40.000-0400","dataType":"branch"}}}},"isStale":false}}'
)


@pytest.mark.asyncio
async def test_index_table_dev_column_blank_when_no_dev_status(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-1", "Dev")) == ""


@pytest.mark.asyncio
async def test_index_table_dev_column_shows_pr_state(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_fields(jira_dir, "SAT-1", {"customfield_10000": DEV_STATUS_OPEN_PR_RAW})
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-1", "Dev")) == " Open "


@pytest.mark.asyncio
async def test_index_table_dev_column_shows_branch_when_no_pr(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_fields(jira_dir, "SAT-1", {"customfield_10000": DEV_STATUS_BRANCH_ONLY_RAW})
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test():
        table = app.screen.query_one(DataTable)
        assert str(table.get_cell("SAT-1", "Dev")) == " Branch "


@pytest.mark.asyncio
async def test_index_dev_status_click_opens_the_single_link_directly(tmp_path: Path, monkeypatch) -> None:
    from jira_workbench.tui.screens import index as index_module
    from textual.widgets.data_table import RowKey

    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    _set_issue_fields(jira_dir, "SAT-1", {"customfield_10000": DEV_STATUS_OPEN_PR_RAW})
    single_pr_summary = {
        "summary": {"pullrequest": {"overall": {"count": 1}, "byInstanceType": {APP_TYPE: {"count": 1}}}}
    }
    client = DevStatusClient(summary=single_pr_summary, pr_detail=LINKED_PR_DETAIL, branch_detail={"detail": []})
    app = _app_with_client(jira_dir, client)
    opened: list[str] = []
    monkeypatch.setattr(index_module.webbrowser, "open", lambda url: opened.append(url))

    async with app.run_test():
        assert isinstance(app.screen, IndexScreen)
        await app.screen._open_dev_status_link(RowKey("SAT-1"))

        assert opened == ["https://github.com/stardog-oss/kube-stardog-stack/pull/15"]


@pytest.mark.asyncio
async def test_index_dev_status_click_with_multiple_links_opens_a_picker(tmp_path: Path, monkeypatch) -> None:
    from jira_workbench.tui.screens import index as index_module
    from jira_workbench.tui.widgets.prompts import OptionPickerScreen
    from textual.widgets import OptionList
    from textual.widgets.data_table import RowKey

    jira_dir = synced_jira_dir(tmp_path)
    _set_issue_id(jira_dir, "SAT-1", "78547")
    _set_issue_fields(jira_dir, "SAT-1", {"customfield_10000": DEV_STATUS_OPEN_PR_RAW})
    client = DevStatusClient(summary=LINKED_SUMMARY, pr_detail=LINKED_PR_DETAIL, branch_detail=LINKED_BRANCH_DETAIL)
    app = _app_with_client(jira_dir, client)
    opened: list[str] = []
    monkeypatch.setattr(index_module.webbrowser, "open", lambda url: opened.append(url))

    async with app.run_test() as pilot:
        assert isinstance(app.screen, IndexScreen)
        worker = app.screen.run_worker(app.screen._open_dev_status_link(RowKey("SAT-1")))
        await pilot.pause()
        assert isinstance(app.screen, OptionPickerScreen)

        options = app.screen.query_one(OptionList)
        options.highlighted = 0
        await pilot.press("enter")
        await worker.wait()

        assert opened == ["https://github.com/stardog-oss/kube-stardog-stack/pull/15"]


async def test_push_all_pushes_shadow_and_clears_it(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "summary", "New local summary")
    client = SimplePushClient(updated="2026-07-20T00:00:01.000+0000")
    app = JiraWorkbenchApp(
        jira_dir,
        component_field="customfield_10071",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )
    app.get_api_client = lambda: client  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.press("P")
        await pilot.pause()
        assert isinstance(app.screen, PushReviewScreen)  # review-changes list shown first
        await pilot.press("p")
        await pilot.pause()

        assert isinstance(app.screen, PushAllProgressScreen)
        for _ in range(50):
            if app.screen.__class__ is PushAllProgressScreen and app.screen._done:
                break
            await asyncio.sleep(0.02)
            await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert load_shadow(jira_dir, "SAT-1") is None


@pytest.mark.asyncio
async def test_push_all_review_screen_lists_items_and_shows_full_diff(tmp_path: Path) -> None:
    # Regression test: the curses-era "Push All" confirmation used to show a
    # report of what would actually change before asking to confirm. That
    # report was dropped when the confirm step was rebuilt in Textual as a
    # bare "Push N items?" ConfirmScreen with no detail. This proves the
    # new review screen both lists per-item changes and lets you drill into
    # a full side-by-side diff for a specific item.
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "summary", "New local summary")
    set_field(jira_dir, "SAT-1", "description", "New description")
    app = JiraWorkbenchApp(
        jira_dir,
        component_field="customfield_10071",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )

    async with app.run_test() as pilot:
        await pilot.press("P")
        await pilot.pause()

        assert isinstance(app.screen, PushReviewScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1
        assert table.get_cell("SAT-1", "key") == "SAT-1"
        assert table.get_cell("SAT-1", "component") == "API Team"
        assert table.get_cell("SAT-1", "summary") == "New local summary"
        assert "summary" in str(table.get_cell("SAT-1", "changes"))

        table.move_cursor(row=table.get_row_index("SAT-1"))
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, SideBySideDiffScreen)
        diff_table = app.screen.query_one(DataTable)
        before_cells = [str(diff_table.get_cell_at((row, 0))) for row in range(diff_table.row_count)]
        after_cells = [str(diff_table.get_cell_at((row, 1))) for row in range(diff_table.row_count)]
        assert any("New description" in cell for cell in after_cells)
        assert not any("New description" in cell for cell in before_cells)
        assert any("New local summary" in cell for cell in after_cells)

        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, PushReviewScreen)


@pytest.mark.asyncio
async def test_diff_screen_wraps_long_description_to_fit_without_horizontal_scroll(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    long_description = " ".join(f"word{i}" for i in range(200))
    set_field(jira_dir, "SAT-1", "description", long_description)
    app = JiraWorkbenchApp(
        jira_dir,
        component_field="customfield_10071",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )

    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.press("P")
        await pilot.pause()
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("SAT-1"))
        await pilot.press("enter")
        await pilot.pause()

        diff_screen = app.screen
        assert isinstance(diff_screen, SideBySideDiffScreen)
        column_width = diff_screen._column_width()
        dt = diff_screen.query_one(DataTable)
        for row in range(dt.row_count):
            for col in range(2):
                cell = str(dt.get_cell_at((row, col)))
                # Every wrappable word is short ("wordN"), so nothing here
                # legitimately needs to exceed the computed column width --
                # unlike a real unbreakable long token (e.g. a URL), which
                # is the one accepted exception documented elsewhere.
                assert len(cell) <= column_width, f"row {row} col {col} exceeds column width: {cell!r}"


@pytest.mark.asyncio
async def test_push_all_close_button_guards_until_done_then_closes(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "summary", "New local summary")
    client = SlowSimplePushClient(updated="2026-07-20T00:00:01.000+0000")
    app = JiraWorkbenchApp(
        jira_dir,
        component_field="customfield_10071",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )
    app.get_api_client = lambda: client  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.press("P")
        await pilot.pause()
        assert isinstance(app.screen, PushReviewScreen)  # review-changes list shown first
        await pilot.press("p")
        await pilot.pause()

        assert isinstance(app.screen, PushAllProgressScreen)
        screen = app.screen
        if not screen._done:
            # Clicking Close before the push completes must not dismiss the dialog.
            await pilot.click("#close-button")
            await pilot.pause()
            assert app.screen is screen

        for _ in range(50):
            if screen._done:
                break
            await asyncio.sleep(0.02)
            await pilot.pause()

        await pilot.click("#close-button")
        await pilot.pause()
        assert isinstance(app.screen, IndexScreen)


@pytest.mark.asyncio
async def test_toggle_active_filters_done_items(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-2", "status", "Done")
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        index.action_reload()
        table = index.query_one(DataTable)
        assert table.row_count == 1
        await pilot.press("a")
        table = index.query_one(DataTable)
        assert table.row_count == 2


def _write_sort_test_items(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "a item",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "priority": {"name": "Medium"},
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "b item",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "priority": {"name": "Highest"},
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-3/issue.json",
        {
            "key": "SAT-3",
            "fields": {
                "summary": "c item",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "priority": {"name": "Lowest"},
            },
        },
    )
    build_manifest(tmp_path)


@pytest.mark.asyncio
async def test_click_column_header_sorts_by_that_column(tmp_path: Path) -> None:
    _write_sort_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test(size=(120, 20)) as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        table = index.query_one(DataTable)
        assert [table.get_row_at(i)[1] for i in range(table.row_count)] == ["SAT-1", "SAT-2", "SAT-3"]

        await pilot.click(DataTable, offset=(45, 0))  # "Priority" header
        await pilot.pause()

        assert index.sort_column == "Priority"
        assert index.sort_reverse is False
        assert [table.get_row_at(i)[1] for i in range(table.row_count)] == ["SAT-2", "SAT-1", "SAT-3"]
        header_line = "".join(seg.text for seg in table.render_line(0))
        assert "Pr ▲" in header_line


@pytest.mark.asyncio
async def test_click_same_column_header_twice_reverses_sort(tmp_path: Path) -> None:
    _write_sort_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test(size=(120, 20)) as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        table = index.query_one(DataTable)

        await pilot.click(DataTable, offset=(45, 0))
        await pilot.pause()
        await pilot.click(DataTable, offset=(45, 0))
        await pilot.pause()

        assert index.sort_column == "Priority"
        assert index.sort_reverse is True
        assert [table.get_row_at(i)[1] for i in range(table.row_count)] == ["SAT-3", "SAT-1", "SAT-2"]
        header_line = "".join(seg.text for seg in table.render_line(0))
        assert "Pr ▼" in header_line


def _write_filter_test_items(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Helm item",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
                "assignee": {"displayName": "Marlon Garcia"},
                "fixVersions": [{"name": "3.4.0"}],
            },
        },
    )
    write_json(
        tmp_path / "components/terraform/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "Terraform item",
                "issuetype": {"name": "Story"},
                "status": {"name": "To Do"},
                "components": [{"name": "terraform"}],
                "assignee": {"displayName": "Jane Doe"},
            },
        },
    )
    build_manifest(tmp_path)


@pytest.mark.asyncio
async def test_filters_screen_opens_with_any_defaults(tmp_path: Path) -> None:
    _write_filter_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        await pilot.press("f")
        screen = app.screen
        assert isinstance(screen, FiltersScreen)
        table = screen.query_one(DataTable)
        assert str(table.get_cell("active", "value")) == "Yes"
        assert str(table.get_cell("modified", "value")) == "No"
        assert str(table.get_cell("component", "value")) == "(any)"
        assert str(table.get_cell("fixVersion", "value")) == "(any)"
        assert str(table.get_cell("assignee", "value")) == "(any)"
        assert str(table.get_cell("pattern", "value")) == "(any)"


@pytest.mark.asyncio
async def test_filters_screen_component_filter_narrows_index(tmp_path: Path) -> None:
    _write_filter_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        await pilot.press("f")
        screen = app.screen
        assert isinstance(screen, FiltersScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("component"))
        await pilot.press("enter")
        await pilot.pause()

        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "helm-chart"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, FiltersScreen)
        assert str(table.get_cell("component", "value")) == " helm-chart "
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert index.field_filters == {"component": ["helm-chart"]}
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 1


@pytest.mark.asyncio
async def test_filters_screen_assignee_filter_narrows_index(tmp_path: Path) -> None:
    # Assignee has zero bespoke wiring beyond a FILTER_FIELDS entry -- this
    # proves the generic registry-driven design actually works end-to-end
    # for a field that never had its own keybinding/action/cycle function.
    _write_filter_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        await pilot.press("f")
        screen = app.screen
        assert isinstance(screen, FiltersScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("assignee"))
        await pilot.press("enter")
        await pilot.pause()

        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "Jane Doe"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert index.field_filters == {"assignee": ["Jane Doe"]}
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 1
        assert index_table.get_cell("SAT-2", "Key") == "SAT-2"


@pytest.mark.asyncio
async def test_filters_screen_status_filter_narrows_index(tmp_path: Path) -> None:
    # Status has zero bespoke wiring beyond a FILTER_FIELDS entry, same as
    # project/assignee -- proves the generic registry-driven design covers
    # it too.
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "Two",
                "issuetype": {"name": "Task"},
                "status": {"name": "In Progress"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        assert index.query_one(DataTable).row_count == 2

        await pilot.press("f")
        screen = app.screen
        assert isinstance(screen, FiltersScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("status"))
        await pilot.press("enter")
        await pilot.pause()

        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "In Progress"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, FiltersScreen)
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert index.field_filters == {"status": ["In Progress"]}
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 1
        assert index_table.get_cell("SAT-2", "Key") == "SAT-2"


def _write_board_cache(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {
            "project": "SAT",
            "fetchedAt": "now",
            "boards": [
                {
                    "id": 32,
                    "name": "Helm board",
                    "type": "simple",
                    "predicate": {"op": "eq", "field": "component", "value": "helm-chart"},
                    "backlogKeys": ["SAT-1"],
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_filters_screen_project_filter_narrows_index(tmp_path: Path) -> None:
    _write_filter_test_items(tmp_path)
    write_json(
        tmp_path / "components/_unassigned/PLAT-1/issue.json",
        {
            "key": "PLAT-1",
            "fields": {"summary": "Platform item", "issuetype": {"name": "Task"}, "status": {"name": "To Do"}},
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        assert index.query_one(DataTable).row_count == 3

        await pilot.press("f")
        screen = app.screen
        assert isinstance(screen, FiltersScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("project"))
        await pilot.press("enter")
        await pilot.pause()

        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "PLAT"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, FiltersScreen)
        assert str(table.get_cell("project", "value")) == "PLAT"
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 1
        assert index_table.get_cell("PLAT-1", "Key") == "PLAT-1"


@pytest.mark.asyncio
async def test_filters_screen_board_picker_excludes_inactive_boards(tmp_path: Path) -> None:
    from jira_workbench.metadata import set_board_active
    from jira_workbench.tui.widgets.prompts import OptionPickerScreen

    _write_filter_test_items(tmp_path)
    _write_board_cache(tmp_path)
    set_board_active(tmp_path, "jira", 32, False)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        await pilot.press("f")
        screen = app.screen
        assert isinstance(screen, FiltersScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("board"))
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        assert isinstance(picker, OptionPickerScreen)
        assert picker._options == ["(any)"]


@pytest.mark.asyncio
async def test_filters_screen_board_filter_narrows_index(tmp_path: Path) -> None:
    _write_filter_test_items(tmp_path)
    _write_board_cache(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        await pilot.press("f")
        screen = app.screen
        assert isinstance(screen, FiltersScreen)
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("board"))
        await pilot.press("enter")
        await pilot.pause()

        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "Helm board"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, FiltersScreen)
        assert str(table.get_cell("board", "value")) == "Helm board"
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert index.board == "Helm board"
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 1
        assert index_table.get_cell("SAT-1", "Key") == "SAT-1"


@pytest.mark.asyncio
async def test_filters_screen_board_scope_narrows_to_backlog(tmp_path: Path) -> None:
    _write_filter_test_items(tmp_path)
    _write_board_cache(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        index.field_filters = {}
        index.board = "Helm board"
        index.board_scope = "active"
        index._rebuild_table()
        await pilot.pause()

        # SAT-1 is the only item matching "Helm board", and it's in that
        # board's cached backlog -- so scope=active should show nothing.
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 0

        index.board_scope = "backlog"
        index._rebuild_table()
        await pilot.pause()
        assert index_table.row_count == 1
        assert index_table.get_cell("SAT-1", "Key") == "SAT-1"


@pytest.mark.asyncio
async def test_filters_screen_clear_all_resets_board_filter(tmp_path: Path) -> None:
    _write_filter_test_items(tmp_path)
    _write_board_cache(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        await pilot.press("f")
        screen = app.screen
        assert isinstance(screen, FiltersScreen)
        screen.board = "Helm board"
        screen.board_scope = "backlog"
        screen._rebuild_rows()
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()

        table = screen.query_one(DataTable)
        assert str(table.get_cell("board", "value")) == "(any)"
        assert str(table.get_cell("boardScope", "value")) == "(any)"
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert index.board is None
        assert index.board_scope is None


@pytest.mark.asyncio
async def test_hide_done_after_days_applies_only_to_active_board_scope(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Old done item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Done"},
                "components": [{"name": "helm-chart"}],
                "statuscategorychangedate": old,
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "Active item",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    build_manifest(tmp_path)
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {
            "project": "SAT",
            "fetchedAt": "now",
            "boards": [
                {
                    "id": 32,
                    "name": "Helm board",
                    "type": "simple",
                    "predicate": {"op": "eq", "field": "component", "value": "helm-chart"},
                    "backlogKeys": [],
                }
            ],
        },
    )
    app = JiraWorkbenchApp(tmp_path, component_field="components", hide_done_after_days=7)

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        index.active_only = False
        index.board = "Helm board"
        index.board_scope = "active"
        index._rebuild_table()
        await pilot.pause()

        table = index.query_one(DataTable)
        assert table.row_count == 1
        assert table.get_cell("SAT-2", "Key") == "SAT-2"

        index.board_scope = "any"
        index._rebuild_table()
        await pilot.pause()
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_filters_screen_none_bucket_is_distinct_from_any(tmp_path: Path) -> None:
    # Regression test for a latent bug the new design fixes: picking "(none)"
    # in the old fix-version picker always cleared the filter instead of
    # ever letting you filter down to "items with no fix version". Now
    # "(any)" (clear) and "(none)" (a real empty-value bucket) are distinct.
    _write_filter_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        await pilot.press("f")
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("fixVersion"))
        await pilot.press("enter")
        await pilot.pause()

        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "(none)"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

        assert index.field_filters == {"fixVersion": ["(none)"]}
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 1
        assert index_table.get_cell("SAT-2", "Key") == "SAT-2"

        # Reopen and clear back to "(any)" via the "d" clear action -- a
        # multi-select picker has no "(any)" option of its own to pick.
        await pilot.press("f")
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("fixVersion"))
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

        assert index.field_filters == {}
        assert index.query_one(DataTable).row_count == 2


@pytest.mark.asyncio
async def test_filters_screen_toggle_active_from_inside_matches_footer_toggle(tmp_path: Path) -> None:
    _write_filter_test_items(tmp_path)
    write_json(
        tmp_path / "components/helm-chart/SAT-3/issue.json",
        {"key": "SAT-3", "fields": {"summary": "Done item", "issuetype": {"name": "Task"}, "status": {"name": "Done"}}},
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        assert index.query_one(DataTable).row_count == 2
        await pilot.press("f")
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("active"))
        await pilot.press("enter")
        await pilot.pause()
        assert str(table.get_cell("active", "value")) == "No"
        await pilot.press("q")
        await pilot.pause()

        assert index.active_only is False
        assert index.query_one(DataTable).row_count == 3


@pytest.mark.asyncio
async def test_filters_screen_clear_all_resets_everything(tmp_path: Path) -> None:
    _write_filter_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)
        index.field_filters = {"component": "helm-chart", "assignee": "Marlon Garcia"}
        index.current_filter = "helm"
        index.modified_only = True
        index.active_only = False

        await pilot.press("f")
        assert isinstance(app.screen, FiltersScreen)
        await pilot.press("c")
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert index.field_filters == {}
        assert index.current_filter is None
        assert index.modified_only is False
        assert index.active_only is True


class FakeMetaClient:
    def __init__(self) -> None:
        self.versions: list[dict[str, Any]] = [
            {"id": "10000", "name": "v1", "released": False, "archived": False}
        ]
        self.components: list[dict[str, Any]] = [{"id": "20000", "name": "helm-chart"}]

    def project(self, key: str) -> dict[str, str]:
        return {"id": "1", "key": key}

    def get_project_versions(self, key: str) -> Any:
        return self.versions

    def add_version(
        self,
        key: str,
        project_id: str,
        version: str,
        *,
        is_archived: bool = False,
        is_released: bool = False,
    ) -> Any:
        self.versions.append({"id": "10001", "name": version, "released": is_released, "archived": is_archived})
        return {"name": version}

    def update_version(
        self,
        version: str,
        name: str | None = None,
        description: str | None = None,
        is_archived: bool | None = None,
        is_released: bool | None = None,
        start_date: str | None = None,
        release_date: str | None = None,
    ) -> Any:
        for item in self.versions:
            if item["id"] == version:
                if name is not None:
                    item["name"] = name
                if is_archived is not None:
                    item["archived"] = is_archived
                if is_released is not None:
                    item["released"] = is_released
        return {}

    def delete_version(self, version: str, moved_fixed: str | None = None, move_affected: str | None = None) -> Any:
        self.versions = [item for item in self.versions if item["id"] != version]
        return {}

    def get_project_components(self, key: str) -> Any:
        return self.components

    def create_component(self, component: dict[str, Any]) -> Any:
        self.components.append({"id": "1", "name": component["name"]})
        return component

    def issue_createmeta(self, project: str, expand: str = "projects.issuetypes.fields") -> dict[str, Any]:
        return {"projects": []}

    def add_custom_field_option(self, field_id: str | int, context_id: str | int, options: list[str]) -> Any:
        return {"options": options}

    def get_all_agile_boards(self, project_key: str | None = None) -> Any:
        return {"values": [{"id": 32, "name": "SAT board", "type": "simple"}]}

    def get_agile_board_configuration(self, board_id: object) -> Any:
        return {"filter": {"id": "10089"}}

    def get(self, path: str, params: dict[str, object] | None = None) -> Any:
        if path == "rest/api/2/field":
            return []
        if path == "rest/api/2/filter/10089":
            return {"jql": "project = SAT ORDER BY Rank ASC"}
        if path == "rest/agile/1.0/board/32/backlog":
            start = params["startAt"] if params else 0
            if start == 0:
                return {"total": 1, "issues": [{"key": "SAT-1"}]}
            return {"total": 1, "issues": []}
        raise AssertionError(f"unexpected get() path in test: {path}")


def meta_app(tmp_path: Path, client: FakeMetaClient, monkeypatch: pytest.MonkeyPatch) -> JiraWorkbenchApp:
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: client)
    return JiraWorkbenchApp(
        tmp_path,
        component_field="components",
        project="SAT",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
        initial_screen="meta",
    )


@pytest.mark.asyncio
async def test_meta_screen_opens_versions_and_renames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        assert isinstance(app.screen, MetaScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, VersionsScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1

        await pilot.press("e")
        await pilot.pause()
        input_widget = app.screen.query_one(Input)
        input_widget.value = "v1-renamed"
        await pilot.press("enter")
        await pilot.pause()

        assert client.versions[0]["name"] == "v1-renamed"
        cache = load_versions(tmp_path, "SAT")
        assert cache is not None
        assert cache["versions"][0]["name"] == "v1-renamed"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MetaScreen)


@pytest.mark.asyncio
async def test_versions_screen_blocks_mutations_for_a_read_only_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jira_workbench.config import ProjectSettings
    from jira_workbench.metadata import write_project_registry

    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        assert isinstance(app.screen, MetaScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, VersionsScreen)

        await pilot.press("n")
        await pilot.pause()
        # blocked before any TextPromptScreen was even opened
        assert isinstance(app.screen, VersionsScreen)

        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, VersionsScreen)
        assert client.versions[0]["name"] == "v1"


class VersionRenamePropagationClient(FakeMetaClient):
    """FakeMetaClient plus get_issue -- update_version renames the version
    the same way FakeMetaClient does, and get_issue simulates what a real
    re-fetch would return afterward: Jira resolves fixVersions by id, so the
    *current* (renamed) name comes back regardless of what was cached."""

    def get_issue(self, issue_id_or_key: str, fields: Any = None, **kwargs: Any) -> Any:
        version = self.versions[0]
        return {
            "key": issue_id_or_key,
            "fields": {
                "summary": "Chart values",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "fixVersions": [{"id": version["id"], "name": version["name"]}],
            },
        }


@pytest.mark.asyncio
async def test_version_rename_refreshes_locally_synced_issues_referencing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_json(
        tmp_path / "components/_unassigned/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Chart values",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "fixVersions": [{"id": "10000", "name": "v1"}],
            },
        },
    )
    build_manifest(tmp_path)

    client = VersionRenamePropagationClient()
    app = meta_app(tmp_path, client, monkeypatch)
    app.get_api_client = lambda: client  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        assert isinstance(app.screen, MetaScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, VersionsScreen)

        await pilot.press("e")
        await pilot.pause()
        input_widget = app.screen.query_one(Input)
        input_widget.value = "v1-renamed"
        await pilot.press("enter")
        await pilot.pause()

        assert client.versions[0]["name"] == "v1-renamed"

    # The locally synced issue's own fixVersions is refreshed too, not just
    # the global versions.json cache Meta itself reads.
    issue = read_json(tmp_path / "components/_unassigned/SAT-1/issue.json")
    assert issue["fields"]["fixVersions"][0]["name"] == "v1-renamed"


@pytest.mark.asyncio
async def test_meta_screen_shows_component_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        assert isinstance(app.screen, MetaScreen)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, MetaScreen)
        output_widget = app.screen.query_one("#meta-output")
        assert "helm-chart" in str(output_widget.visual)
        # Regression check: the output must render right below the 2-item action
        # list, not get pushed to the bottom of the screen by an unconstrained
        # ListView defaulting to 1fr height.
        assert output_widget.region.y < 10


@pytest.mark.asyncio
async def test_meta_screen_opens_boards_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        assert isinstance(app.screen, MetaScreen)
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, BoardsScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1
        assert str(table.get_cell("jira:32", "name")) == "SAT board"
        assert str(table.get_cell("jira:32", "active")) == "yes"
        assert str(table.get_cell("jira:32", "type")) == "jira"
        assert str(table.get_cell("jira:32", "status")) == "ok"
        assert str(table.get_cell("jira:32", "backlog")) == "1 issues"

        # "Boards" is read-only -- 'n' (add) should not attempt to create anything.
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MetaScreen)
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, MetaScreen)


@pytest.mark.asyncio
async def test_boards_screen_creates_edits_and_deletes_a_local_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    build_manifest(tmp_path)
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, BoardsScreen)
        boards_screen = app.screen

        await pilot.press("n")
        await pilot.pause()

        from jira_workbench.tui.screens.meta import LocalBoardEditScreen

        edit_screen = app.screen
        assert isinstance(edit_screen, LocalBoardEditScreen)

        # cursor starts on the Name row -- Enter opens a text prompt for it.
        await pilot.press("enter")
        await pilot.pause()
        input_widget = app.screen.query_one(Input)
        input_widget.value = "My Local Board"
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen is edit_screen
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.screen is boards_screen
        table = boards_screen.query_one(DataTable)
        assert str(table.get_cell("local:My Local Board", "type")) == "local"
        assert str(table.get_cell("local:My Local Board", "active")) == "yes"

        # toggle active off
        table.move_cursor(row=table.get_row_index("local:My Local Board"))
        await pilot.press("a")
        await pilot.pause()
        assert str(table.get_cell("local:My Local Board", "active")) == ""

        # deleting a jira board is rejected
        table.move_cursor(row=table.get_row_index("jira:32"))
        await pilot.press("d")
        await pilot.pause()
        assert table.row_count == 2

        # delete the local board
        table.move_cursor(row=table.get_row_index("local:My Local Board"))
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")  # confirm
        await pilot.pause()

        assert table.row_count == 1


@pytest.mark.asyncio
async def test_local_board_edit_screen_cancel_discards_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_manifest(tmp_path)
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        boards_screen = app.screen
        assert isinstance(boards_screen, BoardsScreen)

        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        input_widget = app.screen.query_one(Input)
        input_widget.value = "Never Saved"
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("escape")  # cancel, not save
        await pilot.pause()

        assert app.screen is boards_screen
        from jira_workbench.metadata import load_board_settings

        assert load_board_settings(tmp_path)["localBoards"] == []


@pytest.mark.asyncio
async def test_local_board_edit_screen_save_requires_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_manifest(tmp_path)
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("n")
        await pilot.pause()

        from jira_workbench.tui.screens.meta import LocalBoardEditScreen

        edit_screen = app.screen
        assert isinstance(edit_screen, LocalBoardEditScreen)

        await pilot.press("ctrl+s")  # no name set yet
        await pilot.pause()

        assert app.screen is edit_screen  # blocked, still open

        from jira_workbench.metadata import load_board_settings

        assert load_board_settings(tmp_path)["localBoards"] == []


@pytest.mark.asyncio
async def test_boards_screen_edit_updates_name_and_filters_in_one_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jira_workbench.metadata import add_local_board, load_board_settings

    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
                "assignee": {"displayName": "Alex Epic"},
            },
        },
    )
    build_manifest(tmp_path)
    add_local_board(tmp_path, "Old Name", {}, None)
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        boards_screen = app.screen
        assert isinstance(boards_screen, BoardsScreen)
        table = boards_screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("local:Old Name"))

        await pilot.press("e")
        await pilot.pause()

        from jira_workbench.tui.screens.meta import LocalBoardEditScreen

        edit_screen = app.screen
        assert isinstance(edit_screen, LocalBoardEditScreen)
        edit_table = edit_screen.query_one(DataTable)
        assert str(edit_table.get_cell("name", "value")) == "Old Name"

        edit_table.move_cursor(row=edit_table.get_row_index("name"))
        await pilot.press("enter")
        await pilot.pause()
        input_widget = app.screen.query_one(Input)
        input_widget.value = "New Name"
        await pilot.press("enter")
        await pilot.pause()

        edit_table.move_cursor(row=edit_table.get_row_index("assignee"))
        await pilot.press("enter")
        await pilot.pause()
        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "Alex Epic"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.screen is boards_screen
        boards = load_board_settings(tmp_path)["localBoards"]
        assert boards[0]["name"] == "New Name"
        assert boards[0]["fieldFilters"] == {"assignee": ["Alex Epic"]}


@pytest.mark.asyncio
async def test_boards_screen_reuses_index_items_instead_of_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Opening Meta > Boards > New/Edit from the index used to re-scan every
    # locally synced issue from scratch each time (load_manifest_items),
    # which is what made it feel slow -- it should now reuse IndexScreen's
    # own already-loaded (and incrementally kept fresh) item list instead.
    import jira_workbench.tui.screens.meta as meta_module

    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    build_manifest(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components")

    calls = []
    real_load = meta_module.load_manifest_items
    monkeypatch.setattr(
        meta_module,
        "load_manifest_items",
        lambda *a, **k: calls.append(1) or real_load(*a, **k),
    )

    async with app.run_test() as pilot:
        index = app.screen
        assert isinstance(index, IndexScreen)

        await pilot.press("M")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, BoardsScreen)

        await pilot.press("n")
        await pilot.pause()

        from jira_workbench.tui.screens.meta import LocalBoardEditScreen

        assert isinstance(app.screen, LocalBoardEditScreen)
        await pilot.press("escape")
        await pilot.pause()

    assert calls == []


@pytest.mark.asyncio
async def test_local_board_edit_screen_active_filter_row_opens_nested_sub_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "In Progress"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    build_manifest(tmp_path)
    from jira_workbench.metadata import add_local_board, load_board_settings

    add_local_board(tmp_path, "My Filter", {}, None)
    client = FakeMetaClient()
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        boards_screen = app.screen
        assert isinstance(boards_screen, BoardsScreen)
        table = boards_screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("local:My Filter"))

        await pilot.press("e")
        await pilot.pause()

        from jira_workbench.tui.screens.meta import LocalBoardActiveFilterScreen, LocalBoardEditScreen

        edit_screen = app.screen
        assert isinstance(edit_screen, LocalBoardEditScreen)
        edit_table = edit_screen.query_one(DataTable)
        assert str(edit_table.get_cell("activeFilter", "value")) == "(none)"

        edit_table.move_cursor(row=edit_table.get_row_index("activeFilter"))
        await pilot.press("enter")
        await pilot.pause()

        sub_screen = app.screen
        assert isinstance(sub_screen, LocalBoardActiveFilterScreen)
        sub_table = sub_screen.query_one(DataTable)
        sub_table.move_cursor(row=sub_table.get_row_index("status"))
        await pilot.press("enter")
        await pilot.pause()
        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "In Progress"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is edit_screen
        assert str(edit_table.get_cell("activeFilter", "value")) == "status=In Progress"

        await pilot.press("ctrl+s")
        await pilot.pause()

    boards = load_board_settings(tmp_path)["localBoards"]
    assert boards[0]["activeFilter"] == {"fieldFilters": {"status": ["In Progress"]}, "pattern": None}


class FakeLabelsPushClient:
    def __init__(self, issues: dict[str, Any]) -> None:
        self.issues = issues
        self.update_calls: dict[str, dict[str, Any]] = {}

    def get_issue(self, issue_id_or_key: str, fields: Any = None, **kwargs: Any) -> Any:
        issue = self.issues[issue_id_or_key]
        if fields == "*all":
            return issue
        return {"key": issue_id_or_key, "fields": {"updated": issue["fields"].get("updated")}}

    def issue_get_comments(self, issue_id: str) -> Any:
        return {"comments": []}

    def update_issue_field(self, key: str, fields: dict[str, Any], notify_users: bool = True) -> None:
        self.update_calls[key] = fields
        self.issues[key]["fields"].update(fields)


def _write_labels_test_items(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "One",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "labels": ["infra", "helm"],
                "updated": "2026-01-01T00:00:00.000+0000",
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "Two",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "labels": ["helm"],
                "updated": "2026-01-01T00:00:00.000+0000",
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-3/issue.json",
        {
            "key": "SAT-3",
            "fields": {
                "summary": "Three",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "updated": "2026-01-01T00:00:00.000+0000",
            },
        },
    )
    build_manifest(tmp_path)


def _labels_app(tmp_path: Path, client: Any) -> JiraWorkbenchApp:
    app = JiraWorkbenchApp(
        tmp_path,
        component_field="components",
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
        project="SAT",
    )
    app.get_api_client = lambda: client  # type: ignore[method-assign]
    return app


@pytest.mark.asyncio
async def test_labels_screen_lists_counts(tmp_path: Path) -> None:
    _write_labels_test_items(tmp_path)
    app = _labels_app(tmp_path, FakeLabelsPushClient({}))

    async with app.run_test() as pilot:
        assert isinstance(app.screen, IndexScreen)
        await pilot.press("M")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, LabelsScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2
        assert str(table.get_cell("helm", "count")) == "2"
        assert str(table.get_cell("infra", "count")) == "1"


@pytest.mark.asyncio
async def test_labels_screen_delete_removes_from_all_issues_and_pushes(tmp_path: Path) -> None:
    _write_labels_test_items(tmp_path)
    issues = {
        "SAT-1": {"key": "SAT-1", "fields": {"summary": "One", "labels": ["infra", "helm"], "updated": "2026-01-01T00:00:00.000+0000"}},
        "SAT-2": {"key": "SAT-2", "fields": {"summary": "Two", "labels": ["helm"], "updated": "2026-01-01T00:00:00.000+0000"}},
    }
    client = FakeLabelsPushClient(issues)
    app = _labels_app(tmp_path, client)

    async with app.run_test() as pilot:
        await pilot.press("M")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        labels_screen = app.screen
        assert isinstance(labels_screen, LabelsScreen)
        table = labels_screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("helm"))  # on both SAT-1 and SAT-2

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")  # confirm
        await pilot.pause()

        # Push progress screen opens automatically; close it once done.
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, LabelsScreen)
        assert client.update_calls["SAT-1"]["labels"] == ["infra"]
        assert client.update_calls["SAT-2"]["labels"] == []
        table = labels_screen.query_one(DataTable)
        assert table.row_count == 1  # only "infra" remains
        assert not (tmp_path / "components/helm-chart/SAT-1/shadow.json").exists()  # pushed, shadow cleared

        await pilot.press("escape")  # LabelsScreen -> MetaScreen
        await pilot.pause()
        await pilot.press("escape")  # MetaScreen -> IndexScreen
        await pilot.pause()

        # Returning to the index reflects the label removal without a manual reload.
        assert isinstance(app.screen, IndexScreen)
        items_by_key = {item["key"]: item for item in app.screen.items}
        assert items_by_key["SAT-1"]["labels"] == ["infra"]
        assert items_by_key["SAT-2"]["labels"] == []


@pytest.mark.asyncio
async def test_labels_screen_delete_skips_issues_in_a_read_only_project(tmp_path: Path) -> None:
    from jira_workbench.config import ProjectSettings
    from jira_workbench.metadata import write_project_registry

    _write_labels_test_items(tmp_path)
    write_json(
        tmp_path / "components/helm-chart/OTHERPROJ-1/issue.json",
        {
            "key": "OTHERPROJ-1",
            "fields": {
                "summary": "Other project",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "labels": ["helm"],
                "updated": "2026-01-01T00:00:00.000+0000",
            },
        },
    )
    build_manifest(tmp_path)
    write_project_registry(
        tmp_path,
        (ProjectSettings(key="SAT", default=True), ProjectSettings(key="OTHERPROJ", read_only=True)),
    )
    issues = {
        "SAT-1": {"key": "SAT-1", "fields": {"summary": "One", "labels": ["infra", "helm"], "updated": "2026-01-01T00:00:00.000+0000"}},
        "SAT-2": {"key": "SAT-2", "fields": {"summary": "Two", "labels": ["helm"], "updated": "2026-01-01T00:00:00.000+0000"}},
        "OTHERPROJ-1": {
            "key": "OTHERPROJ-1",
            "fields": {"summary": "Other project", "labels": ["helm"], "updated": "2026-01-01T00:00:00.000+0000"},
        },
    }
    client = FakeLabelsPushClient(issues)
    app = _labels_app(tmp_path, client)

    async with app.run_test() as pilot:
        await pilot.press("M")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        labels_screen = app.screen
        assert isinstance(labels_screen, LabelsScreen)
        table = labels_screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("helm"))  # on SAT-1, SAT-2, and OTHERPROJ-1

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")  # confirm
        await pilot.pause()

        await pilot.press("enter")  # close push progress screen
        await pilot.pause()

        assert "OTHERPROJ-1" not in client.update_calls
        assert client.update_calls["SAT-1"]["labels"] == ["infra"]
        assert client.update_calls["SAT-2"]["labels"] == []
        assert not (tmp_path / "components/helm-chart/OTHERPROJ-1/shadow.json").exists()


@pytest.mark.asyncio
async def test_labels_screen_rename_renames_on_all_issues_and_pushes(tmp_path: Path) -> None:
    _write_labels_test_items(tmp_path)
    issues = {
        "SAT-1": {"key": "SAT-1", "fields": {"summary": "One", "labels": ["infra", "helm"], "updated": "2026-01-01T00:00:00.000+0000"}},
        "SAT-2": {"key": "SAT-2", "fields": {"summary": "Two", "labels": ["helm"], "updated": "2026-01-01T00:00:00.000+0000"}},
    }
    client = FakeLabelsPushClient(issues)
    app = _labels_app(tmp_path, client)

    async with app.run_test() as pilot:
        await pilot.press("M")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        labels_screen = app.screen
        assert isinstance(labels_screen, LabelsScreen)
        table = labels_screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("helm"))

        await pilot.press("e")
        await pilot.pause()
        input_widget = app.screen.query_one(Input)
        input_widget.value = "kubernetes"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")  # confirm
        await pilot.pause()
        await pilot.press("enter")  # close push progress
        await pilot.pause()

        assert client.update_calls["SAT-1"]["labels"] == ["infra", "kubernetes"]
        assert client.update_calls["SAT-2"]["labels"] == ["kubernetes"]
        table = labels_screen.query_one(DataTable)
        assert table.row_count == 2  # "infra" and "kubernetes" -- "helm" is gone
        assert str(table.get_cell("kubernetes", "count")) == "2"
        assert str(table.get_cell("infra", "count")) == "1"


@pytest.mark.asyncio
async def test_labels_screen_delete_with_no_labels_notifies_and_does_nothing(tmp_path: Path) -> None:
    write_json(tmp_path / "manifest.json", {"workItems": []})
    app = _labels_app(tmp_path, FakeLabelsPushClient({}))

    async with app.run_test() as pilot:
        await pilot.press("M")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, LabelsScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 0

        await pilot.press("d")
        await pilot.pause()

        assert isinstance(app.screen, LabelsScreen)


@pytest.mark.asyncio
async def test_labels_screen_without_api_config_blocks_rename_and_delete(tmp_path: Path) -> None:
    _write_labels_test_items(tmp_path)
    app = JiraWorkbenchApp(tmp_path, component_field="components", project="SAT")  # no jira_url/email/token

    async with app.run_test() as pilot:
        await pilot.press("M")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        labels_screen = app.screen
        assert isinstance(labels_screen, LabelsScreen)
        table = labels_screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("helm"))

        await pilot.press("d")
        await pilot.pause()

        # Still on the same screen -- no prompt was opened, nothing was touched.
        assert isinstance(app.screen, LabelsScreen)
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_labels_screen_new_gives_guidance_without_faking_a_create(tmp_path: Path) -> None:
    _write_labels_test_items(tmp_path)
    app = _labels_app(tmp_path, FakeLabelsPushClient({}))

    async with app.run_test() as pilot:
        await pilot.press("M")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, LabelsScreen)
        table = app.screen.query_one(DataTable)
        rows_before = table.row_count

        await pilot.press("n")
        await pilot.pause()
        input_widget = app.screen.query_one(Input)
        input_widget.value = "not-yet-used"
        await pilot.press("enter")
        await pilot.pause()

        # Nothing was created -- Jira has no standalone label registry to add to.
        assert isinstance(app.screen, LabelsScreen)
        assert table.row_count == rows_before


@pytest.mark.asyncio
async def test_index_screen_opens_meta_via_keybinding_and_returns(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        assert isinstance(app.screen, IndexScreen)
        await pilot.press("M")
        await pilot.pause()
        assert isinstance(app.screen, MetaScreen)
        assert app.screen.standalone is False

        await pilot.press("escape")
        await pilot.pause()
        # If MetaScreen had called app.exit() instead of dismiss(), the app would
        # be shutting down here rather than showing the index again.
        assert isinstance(app.screen, IndexScreen)


@pytest.mark.asyncio
async def test_detail_screen_status_change_recognizes_custom_done_status_via_category(tmp_path: Path) -> None:
    # Regression: a custom workflow status ("Solved") that Jira classifies
    # as Done must still trigger the resolution prompt, even though its
    # name isn't one of the hardcoded English words the old code guessed
    # from -- as long as it's been observed (with a real statusCategory)
    # on some locally synced issue anywhere in this jira_dir.
    from textual.widgets import OptionList

    from jira_workbench.tui.widgets.prompts import StatusChangeScreen

    jira_dir = synced_jira_dir(tmp_path)
    write_json(
        jira_dir / "components/_unassigned/SAT-99/issue.json",
        {
            "key": "SAT-99",
            "fields": {"summary": "Other", "status": {"name": "Solved", "statusCategory": {"key": "done"}}},
        },
    )
    app = JiraWorkbenchApp(jira_dir, component_field="customfield_10071")

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("status"))
        await pilot.press("enter")
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, StatusChangeScreen)
        status_options = screen.query_one("#status-options", OptionList)
        solved_index = next(
            index
            for index in range(status_options.option_count)
            if str(status_options.get_option_at_index(index).prompt) == "Solved"
        )
        status_options.highlighted = solved_index
        await pilot.press("enter")
        await pilot.pause()

        resolution_options = screen.query_one("#resolution-options", OptionList)
        assert resolution_options.display is True


@pytest.mark.asyncio
async def test_status_change_screen_combines_status_and_resolution_in_one_modal() -> None:
    from textual import work
    from textual.app import App
    from textual.widgets import OptionList

    from jira_workbench.tui.widgets.prompts import StatusChangeScreen

    class HostApp(App):
        result: tuple[str, str | None] | None = None

        @work
        async def on_mount(self) -> None:
            self.result = await self.push_screen_wait(
                StatusChangeScreen(
                    ["Open", "Done"],
                    ["Fixed", "Won't Do"],
                    current_status="Open",
                    is_done_status=lambda status: status.strip().lower() in {"done", "closed", "resolved", "close"},
                )
            )

    app = HostApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, StatusChangeScreen)
        resolution_options = screen.query_one("#resolution-options", OptionList)
        assert resolution_options.display is False

        status_options = screen.query_one("#status-options", OptionList)
        status_options.highlighted = 1  # "Done"
        await pilot.press("enter")
        await pilot.pause()

        # Selecting a Done-like status reveals the resolution list IN THE SAME
        # screen instance -- no second modal is pushed for it.
        assert isinstance(app.screen, StatusChangeScreen)
        assert app.screen is screen
        assert resolution_options.display is True

        await pilot.press("down")  # "(none)" -> "Fixed"
        await pilot.press("enter")
        await pilot.pause()

        assert app.result == ("Done", "Fixed")


@pytest.mark.asyncio
async def test_versions_screen_delete_combines_confirm_and_move_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeMetaClient()
    client.versions.append({"id": "10002", "name": "v2", "released": False, "archived": False})
    app = meta_app(tmp_path, client, monkeypatch)

    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, VersionsScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2

        await pilot.press("d")
        await pilot.pause()
        from jira_workbench.tui.widgets.prompts import ConfirmWithInputScreen

        assert isinstance(app.screen, ConfirmWithInputScreen)

        input_widget = app.screen.query_one(Input)
        input_widget.value = "v2"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, VersionsScreen)
        assert all(version["id"] != "10000" for version in client.versions)

