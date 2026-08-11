from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import jira_workbench.service
from jira_workbench.config import ProjectSettings, WorkbenchConfig
from jira_workbench.metadata import write_project_registry
from jira_workbench.server import ISSUE_KEY_PATTERN, ITEMS_FILTER_FORM_ID, _diff_display_value, all_items, create_app
from jira_workbench.shadow import add_comment, load_shadow, set_field, set_status_change
from jira_workbench.sync import SyncConfig, build_manifest, read_json, sync_project, write_json
from test_metadata import ApiClient
from test_shadow import PushJiraClient
from test_sync import FakeJiraClient


def _icon_select_template_content(response_text: str, widget_html: str) -> str:
    """The Items list's icon-select widgets (type/priority/assignee/
    fixVersions) no longer carry their own option list inline -- it's
    deduped into a shared <template> (see ensure_icon_template/
    _icon_select_options_template_html in server.py) referenced by the
    widget's own data-options-key. This resolves that reference so a test
    can still assert on what the dropdown's options actually contain."""
    match = re.search(r'data-options-key="([^"]*)"', widget_html)
    assert match, f"widget has no data-options-key: {widget_html!r}"
    key = html.unescape(match.group(1))
    template_start = response_text.index(f'<template id="tpl-{key}"')
    content_start = response_text.index(">", template_start) + 1
    content_end = response_text.index("</template>", content_start)
    return response_text[content_start:content_end]


def _icon_select_current_values(widget_html: str) -> list[str]:
    """The current value(s) an Items-list icon-select trigger carries, from
    its own data-current attribute (a JSON array either way, single- or
    multi-select) -- the shared template itself never bakes in a selected/
    checked state (see jiraWbPopulateMenu's own docstring), so a test
    checking "is this row's value marked as selected" has to read it from
    here instead of a "selected"/"checked" attribute in the HTML."""
    import json as _json

    match = re.search(r'data-current="([^"]*)"', widget_html)
    assert match, f"widget has no data-current: {widget_html!r}"
    return _json.loads(html.unescape(match.group(1)))


def _synced_client(tmp_path: Path, config: WorkbenchConfig | None = None) -> TestClient:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    default_config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),)
    )
    return TestClient(create_app(tmp_path, config or default_config))


def test_all_items_reflects_a_shadow_write_on_the_very_next_call(tmp_path: Path) -> None:
    # all_items() reads straight through to load_manifest_items on every
    # call (no cache to go stale) -- a field edit must be visible
    # immediately.
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),))

    items = all_items(tmp_path, config)
    assert next(item for item in items if item["key"] == "SAT-1")["priority"] == "Medium"

    set_field(tmp_path, "SAT-1", "priority", "High")

    items_after = all_items(tmp_path, config)
    assert next(item for item in items_after if item["key"] == "SAT-1")["priority"] == "High"


def test_all_items_reflects_a_resync_on_the_very_next_call(tmp_path: Path) -> None:
    # build_manifest (a full jira-wb sync, or the post-push refresh) must
    # be visible immediately too -- a newly synced issue should show up on
    # the very next all_items() call.
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),))
    all_items(tmp_path, config)

    write_json(
        tmp_path / "components/other/SAT-3/issue.json",
        {"key": "SAT-3", "fields": {"summary": "A new issue", "issuetype": {"name": "Task"}, "status": {"name": "Open"}}},
    )
    build_manifest(tmp_path)

    items_after = all_items(tmp_path, config)
    assert any(item["key"] == "SAT-3" for item in items_after)


def test_all_items_reflects_a_fix_version_rename_on_the_very_next_call(tmp_path: Path) -> None:
    from jira_workbench.metadata import cache_versions

    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),))
    cache_versions(tmp_path, "SAT", [{"id": "10000", "name": "v1"}])
    (issue_path,) = tmp_path.glob("components/*/SAT-1/issue.json")
    issue = read_json(issue_path)
    issue["fields"]["fixVersions"] = [{"id": "10000", "name": "v1"}]
    write_json(issue_path, issue)
    build_manifest(tmp_path, "customfield_10071")

    items = all_items(tmp_path, config)
    assert next(item for item in items if item["key"] == "SAT-1")["fixVersion"] == "v1"

    cache_versions(tmp_path, "SAT", [{"id": "10000", "name": "v1-renamed"}])

    items_after = all_items(tmp_path, config)
    assert next(item for item in items_after if item["key"] == "SAT-1")["fixVersion"] == "v1-renamed"


def test_list_items_returns_every_synced_item(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/api/items")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert {item["key"] for item in payload["items"]} == {"SAT-1", "SAT-2"}


def test_list_items_filters_by_component(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/api/items", params={"component": "API Team"})

    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["key"] == "SAT-1"


def test_list_items_filters_by_search_pattern(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/api/items", params={"pattern": "SAT-2"})

    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["key"] == "SAT-2"


def test_list_items_missing_manifest_returns_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/api/items")

    assert response.status_code == 404


def test_get_item_returns_issue_and_empty_comments_and_attachments(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/api/items/SAT-2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["issue"]["key"] == "SAT-2"
    assert payload["issue"]["fields"]["summary"] == "Summary for SAT-2"
    assert payload["comments"] == []
    assert payload["attachments"] == []


def test_get_item_reflects_local_shadow_edits(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited locally")

    response = client.get("/api/items/SAT-1")

    assert response.json()["issue"]["fields"]["summary"] == "Edited locally"


def test_get_item_not_found_returns_404(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/api/items/SAT-999")

    assert response.status_code == 404


def test_get_item_rejects_path_traversal_key(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    (jira_dir / "components" / "compA" / "SAT-1").mkdir(parents=True)
    (jira_dir / "components" / "compA" / "SAT-1" / "issue.json").write_text('{"key": "SAT-1"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "issue.json").write_text('{"secret": "leaked"}')
    client = TestClient(create_app(jira_dir, WorkbenchConfig()))

    response = client.get("/api/items/..%2F..%2Foutside")

    assert response.status_code == 404
    assert "leaked" not in response.text


def test_issue_key_pattern_accepts_real_keys_and_rejects_traversal_attempts() -> None:
    # find_existing_issue's glob-based lookup turns out to already be safe
    # against a "../../x" key on its own (glob doesn't resolve ".." the way
    # a direct path join would) -- this is what actually guards the
    # ISSUE_KEY_PATTERN check itself, independent of that incidental safety,
    # since a future refactor (e.g. passing a component_hint, which does do
    # a direct path join) could reintroduce a real traversal otherwise.
    assert ISSUE_KEY_PATTERN.match("SAT-741")
    assert not ISSUE_KEY_PATTERN.match("../../outside")
    assert not ISSUE_KEY_PATTERN.match("SAT-741/../../outside")
    assert not ISSUE_KEY_PATTERN.match("")


def test_meta_versions_endpoint_returns_cached_versions(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {"project": "SAT", "versions": [{"id": "1", "name": "helm-chart-sa 3.4.0", "released": False}]},
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/api/meta/versions", params={"project": "SAT"})

    assert response.status_code == 200
    versions = response.json()["versions"]
    assert [version["name"] for version in versions] == ["helm-chart-sa 3.4.0"]


def test_meta_versions_endpoint_missing_returns_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/api/meta/versions", params={"project": "SAT"})

    assert response.status_code == 404


def test_meta_components_endpoint_uses_manifest_usage_summary(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {
            "components": [{"component": "helm-chart", "count": 2}],
            "workItems": [
                {"key": "SAT-1", "component": "helm-chart", "status": "To Do"},
                {"key": "SAT-2", "component": "helm-chart", "status": "Done"},
            ],
        },
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/api/meta/components")

    assert response.status_code == 200
    components = response.json()["components"]
    assert [component["component"] for component in components] == ["helm-chart"]


def test_meta_components_endpoint_uses_configured_custom_field(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/customfield_10071-options.json",
        {"project": "SAT", "field": "customfield_10071", "options": [{"id": "10114", "value": "helm-chart"}]},
    )
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),)
    )
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/api/meta/components", params={"project": "SAT"})

    assert response.status_code == 200
    components = response.json()["components"]
    assert [component["value"] for component in components] == ["helm-chart"]


def test_meta_boards_endpoint_returns_cached_boards(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {"project": "SAT", "boards": [{"id": "1", "name": "SAT board", "supported": True}]},
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/api/meta/boards", params={"project": "SAT"})

    assert response.status_code == 200
    boards = response.json()["boards"]
    assert [board["name"] for board in boards] == ["SAT board"]


def test_meta_boards_endpoint_missing_returns_404(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/api/meta/boards", params={"project": "SAT"})

    assert response.status_code == 404


def test_items_page_renders_synced_items(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<a href="/items/SAT-1">SAT-1</a>' in response.text
    assert '<a href="/items/SAT-2">SAT-2</a>' in response.text


def test_items_page_filters_by_component(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/", params={"component": "API Team"})

    assert "SAT-1" in response.text
    assert "SAT-2" not in response.text


def test_items_page_missing_manifest_shows_friendly_message_not_an_error(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/")

    assert response.status_code == 200
    assert "jira-wb sync" in response.text


def test_items_page_self_heals_a_bootstrapped_but_never_reindexed_index(tmp_path: Path) -> None:
    # Regression: index.db can exist (bootstrapped -- sources/users rows
    # created just by opening a connection) without ever having been
    # populated by an actual reindex, e.g. a jira_dir where the server was
    # started but `jira-wb sync`/`db reindex` was never run since. The
    # SQL-only fast path (filtered_manifest_items/field_counts/count_items)
    # has no per-item file fallback the way load_manifest_items does, so
    # left unchecked this rendered "0 of 0 synced items match" instead of
    # the real, already-synced data manifest.json says exists.
    from jira_workbench import db

    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    # Simulate "index.db was bootstrapped but never reindexed" by clearing
    # the items table a real sync just populated, without touching
    # manifest.json or the synced issue.json files themselves.
    conn = db.connect(tmp_path)
    try:
        conn.execute("DELETE FROM items")
        conn.commit()
    finally:
        conn.close()
    assert db.count_items(tmp_path) == 0

    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),))
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/")

    assert response.status_code == 200
    assert "SAT-1" in response.text
    assert "of 0 synced items" not in response.text
    assert db.count_items(tmp_path) > 0  # self-healed for the next request too


def test_item_detail_page_renders_fields_and_comments(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    assert response.status_code == 200
    assert "Summary for SAT-1" in response.text
    assert "API Team" in response.text
    assert "comment" in response.text  # SAT-1's fixture comment body


def test_item_detail_page_without_comments_says_so(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-2")

    assert "No comments." in response.text


def test_item_detail_page_not_found_returns_404(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-999")

    assert response.status_code == 404


def test_item_detail_page_rejects_path_traversal_key(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    (jira_dir / "components" / "compA" / "SAT-1").mkdir(parents=True)
    (jira_dir / "components" / "compA" / "SAT-1" / "issue.json").write_text('{"key": "SAT-1"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "issue.json").write_text('{"secret": "leaked"}')
    client = TestClient(create_app(jira_dir, WorkbenchConfig()))

    response = client.get("/items/..%2F..%2Foutside")

    assert response.status_code == 404
    assert "leaked" not in response.text


def _pushable_config() -> WorkbenchConfig:
    return WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),),
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )


def test_item_detail_page_has_multiple_forms_unlike_items_and_meta_pages(tmp_path: Path) -> None:
    # Detail needs many independent small POST forms (one per editable
    # field, one per comment, revert/push) -- page()'s form_wrapped=False
    # deliberately drops the single shared <form> every other page uses,
    # since HTML forbids nesting a <form> inside another <form>. The outer
    # layout wrapper must actually be a <div>, not a <form> -- a plain
    # count of "<form" occurrences can't tell a legal sibling form apart
    # from an illegal nested one, so this checks the wrapper tag directly.
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    assert response.text.count("<form") > 1
    assert '<div class="layout">' in response.text
    assert '<form method="get" class="layout">' not in response.text


def test_items_page_has_the_filter_form_plus_one_per_editable_row(tmp_path: Path) -> None:
    # The single-shared-form design ended once per-row field-edit widgets
    # needed their own independent POST forms (HTML forbids nesting a
    # <form> in another) -- one filter <form>, one small form per editable
    # field per row (Type/Status/Component/Summary/Priority/Assignee/Fix
    # versions = 7 widgets x 2 items here), plus the sidebar's own Goto
    # quick-jump form (present on every page).
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert response.text.count("<form") == 2 + 7 * 2
    assert '<div class="layout">' in response.text


def test_items_page_row_widget_posts_to_that_rows_own_fields_route(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert 'action="/items/SAT-1/fields"' in response.text
    assert 'action="/items/SAT-2/fields"' in response.text


def test_items_page_edit_from_the_list_actually_persists(tmp_path: Path) -> None:
    # The row widget posts to the exact same route the Detail page uses --
    # no new backend endpoint, just new markup pointing at it.
    client = _synced_client(tmp_path)

    response = client.post("/items/SAT-1/fields", data={"field": "priority", "value": "High"}, follow_redirects=False)

    assert response.status_code == 303
    assert load_shadow(tmp_path, "SAT-1")["fields"]["priority"] == {"name": "High"}


def test_items_page_read_only_project_rows_keep_the_plain_icon_not_a_widget(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))

    response = client.get("/")

    assert 'action="/items/SAT-1/fields"' not in response.text
    assert 'class="pill-select-cell"' in response.text  # cells still there, just not editable
    assert 'class="pill-select"' not in response.text


def test_items_page_options_are_scoped_per_row_project(tmp_path: Path) -> None:
    # Two projects with different observed types on screen at once -- each
    # row's widget must only offer its own project's types, not the union.
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    write_json(
        tmp_path / "components/other-project/PLAT-1/issue.json",
        {"key": "PLAT-1", "fields": {"summary": "A PLAT epic", "issuetype": {"name": "Epic"}, "status": {"name": "Open"}}},
    )
    build_manifest(tmp_path)
    config = WorkbenchConfig(
        projects=(
            ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),
            ProjectSettings(key="PLAT"),
        )
    )
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/")

    sat_form_start = response.text.index('action="/items/SAT-1/fields"')
    sat_type_start = response.text.index('name="field" value="type"', sat_form_start)
    sat_type_widget = response.text[sat_type_start : sat_type_start + 600]
    assert "Epic" not in sat_type_widget

    plat_form_start = response.text.index('action="/items/PLAT-1/fields"')
    plat_type_start = response.text.index('name="field" value="type"', plat_form_start)
    plat_type_widget = response.text[plat_type_start : plat_type_start + 600]
    assert "Epic" in plat_type_widget
    assert "Task" not in plat_type_widget


def test_items_page_status_component_assignee_summary_fixversion_are_editable(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    # Status posts to its own /items/{key}/status route (matching the
    # Detail page); the rest share the generic /items/{key}/fields route.
    assert 'action="/items/SAT-1/status"' in response.text
    for field in ("customfield_10071", "assignee", "fixVersions", "summary"):
        assert f'name="field" value="{field}"' in response.text, field


def test_items_page_status_widget_reveals_a_resolution_dropdown_on_done_transition(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    status_start = response.text.index('action="/items/SAT-1/status"')
    status_widget = response.text[status_start : status_start + 1800]
    assert 'class="pill-select"' in status_widget
    assert 'name="status"' in status_widget
    assert 'class="resolution-picker"' in status_widget
    assert 'name="resolution"' in status_widget
    assert "prompt(" not in status_widget


def test_items_page_resolution_dropdown_lists_observed_resolutions(tmp_path: Path) -> None:
    # No hardcoded resolution list -- these come from whatever resolutions
    # this Jira instance's own already-closed issues actually carry, the
    # same local-observation pattern every other dropdown on this page
    # (status/priority/component/assignee options) already uses.
    client = _synced_client(tmp_path)
    write_json(
        tmp_path / "components/other/SAT-3/issue.json",
        {
            "key": "SAT-3",
            "fields": {
                "summary": "An old closed bug",
                "issuetype": {"name": "Bug"},
                "status": {"name": "Done", "statusCategory": {"key": "done"}},
                "resolution": {"name": "Won't Do"},
            },
        },
    )
    build_manifest(tmp_path)

    response = client.get("/")

    status_start = response.text.index('action="/items/SAT-1/status"')
    status_widget = response.text[status_start : status_start + 1800]
    resolution_widget = status_widget[status_widget.index('class="resolution-picker"') :]
    options_html = _icon_select_template_content(response.text, resolution_widget)
    assert 'data-value="Won&#x27;t Do"' in options_html
    assert '<div class="pill-select-option" role="option" data-value=""' in options_html  # explicit "clear" option


def test_items_page_editing_a_row_field_persists(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post("/items/SAT-1/fields", data={"field": "assignee", "value": "Alex"}, follow_redirects=False)

    assert response.status_code == 303
    assert load_shadow(tmp_path, "SAT-1")["fields"]["assignee"] == "Alex"


def test_items_page_component_field_option_never_shows_unassigned_sentinel(tmp_path: Path) -> None:
    # SAT-2 has no component (its manifest entry falls back to the real
    # on-disk "_unassigned" sync sentinel) -- it must never leak as a
    # visible option or as the current value in the Component editor.
    client = _synced_client(tmp_path)

    response = client.get("/")

    component_start = response.text.index('name="field" value="customfield_10071"', response.text.index("SAT-2"))
    component_widget = response.text[component_start : component_start + 500]
    assert "_unassigned" not in component_widget


def test_items_page_swimlane_selector_present_in_sidebar(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert f'<select name="swimlane" id="sidebar-swimlane" form="{ITEMS_FILTER_FORM_ID}"' in aside
    for mode in ("None", "Epic", "Version", "Component", "Board"):
        assert f">{mode}</option>" in aside


def test_items_page_swimlane_groups_rows_with_a_header(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    # SAT-1 has a component ("API Team"), SAT-2 doesn't.
    response = client.get("/", params={"swimlane": "component"})

    assert response.text.count('class="swimlane-header"') == 2
    api_team_header = response.text.index(">API Team</td>")
    unassigned_header = response.text.index(">(none)</td>")
    sat1_row = response.text.index("SAT-1")
    sat2_row = response.text.index("SAT-2")
    # Each item's row must appear after its own lane's header.
    assert api_team_header < sat1_row
    assert unassigned_header < sat2_row


def test_items_page_swimlane_none_has_no_group_headers(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert 'class="swimlane-header"' not in response.text


def test_items_page_defaults_to_latest_key_first_when_nothing_is_sorted(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    # SAT-2 has a higher issue number than SAT-1 -- with no column
    # explicitly clicked, the list should still default to showing the
    # most recently created issue first, not arbitrary manifest order.
    assert response.text.index("SAT-2") < response.text.index("SAT-1")


def test_items_page_first_click_on_a_column_sorts_descending(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assignee_header = re.search(r'<th><a href="([^"]*)">Assignee</a></th>', response.text)
    assert assignee_header is not None
    href = html.unescape(assignee_header.group(1))
    assert "sort=assignee" in href
    assert "dir=desc" in href


def test_items_page_second_click_on_the_sorted_column_flips_to_ascending(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/", params={"sort": "assignee", "dir": "desc"})

    assignee_header = re.search(r'<th><a href="([^"]*)">Assignee[^<]*</a></th>', response.text)
    assert assignee_header is not None
    href = html.unescape(assignee_header.group(1))
    assert "sort=assignee" in href
    assert "dir=asc" in href


def test_items_page_row_widgets_carry_return_to_the_current_list_url(tmp_path: Path) -> None:
    # Every row widget posts to the same /items/{key}/fields or /status
    # route the Detail page uses, whose default redirect target is the
    # Detail page itself -- a list-row edit needs to instead carry the
    # list's own current URL (filters and all) along as a hidden field so
    # the route can send the user back to the list, not into "view mode".
    client = _synced_client(tmp_path)

    response = client.get("/", params={"swimlane": "component"})

    assert 'name="return_to" value="/?' in response.text
    return_to_start = response.text.index('name="return_to" value="') + len('name="return_to" value="')
    return_to_end = response.text.index('"', return_to_start)
    return_to_value = html.unescape(response.text[return_to_start:return_to_end])
    assert "swimlane=component" in return_to_value


def test_items_page_row_field_edit_redirects_back_to_the_list(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post(
        "/items/SAT-1/fields",
        data={"field": "priority", "value": "High", "return_to": "/?project=SAT&swimlane=component"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/?project=SAT&swimlane=component&")
    assert "flash=" in location


def test_items_page_status_change_redirects_back_to_the_list(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post(
        "/items/SAT-1/status",
        data={"status": "In Progress", "return_to": "/?project=SAT"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/?project=SAT&")


def test_items_page_field_edit_without_return_to_still_goes_to_detail(tmp_path: Path) -> None:
    # The Detail page's own forms never send return_to -- must keep
    # redirecting there, unchanged, when the field is absent.
    client = _synced_client(tmp_path)

    response = client.post("/items/SAT-1/fields", data={"field": "priority", "value": "High"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/items/SAT-1?")


def test_items_page_swimlane_header_is_clickable_to_collapse(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/", params={"swimlane": "component"})

    assert 'class="swimlane-header" onclick="jiraWbToggleSwimlane(this)"' in response.text
    assert "function jiraWbToggleSwimlane" in response.text


def test_items_page_swimlane_script_absent_when_ungrouped(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert "jiraWbToggleSwimlane" not in response.text


def test_items_page_new_issue_is_a_plus_icon_below_the_table(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert 'class="new-issue-fab" title="Create a new Jira issue">+</a>' in response.text
    assert 'href="/items/new?return_to=' in response.text
    assert ">New issue<" not in response.text
    assert response.text.index('class="new-issue-fab"') > response.text.index("</table>")


def test_items_page_save_filter_persists_swimlane(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    client = TestClient(create_app(tmp_path, WorkbenchConfig(), config_path))

    response = client.post("/save-filter", data={"swimlane": "epic"}, follow_redirects=False)

    assert response.status_code == 303
    assert "swimlane=epic" in response.headers["location"]
    from jira_workbench.config import load_config

    assert load_config(config_path).view_swimlane == "epic"


def test_items_page_bare_reload_remembers_last_view_via_cookie(tmp_path: Path) -> None:
    # Reloading (a bare "/" nav) previously always snapped back to
    # config.toml's saved defaults, even right after picking a different
    # swimlane -- LAST_VIEW_COOKIE is what a plain reload should now read
    # back instead.
    client = _synced_client(tmp_path)

    client.get("/", params={"swimlane": "component"})
    response = client.get("/")

    assert '<option value="component" selected>Component</option>' in response.text


def test_items_page_bare_reload_uses_config_defaults_when_no_cookie_yet(tmp_path: Path) -> None:
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),),
        view_swimlane="epic",
    )
    client = _synced_client(tmp_path, config)

    response = client.get("/")

    assert '<option value="epic" selected>Epic</option>' in response.text


def test_items_page_remembers_swimlane_separately_per_board(tmp_path: Path) -> None:
    # Picking a swimlane while on one board, then switching to a different
    # board, must not carry that swimlane over -- switching back to the
    # first board should restore its own remembered swimlane, not whatever
    # the second board last had.
    client = _synced_client(tmp_path)

    client.get("/", params={"board": "", "swimlane": "component"})
    client.get("/", params={"board": "Modified Items"})
    response = client.get("/", params={"board": ""})

    assert '<option value="component" selected>Component</option>' in response.text


def test_items_page_switching_boards_does_not_carry_over_the_previous_boards_filters(tmp_path: Path) -> None:
    import http.cookies
    import json as json_module

    client = _synced_client(tmp_path)

    # First view: no board, swimlane=component.
    client.get("/", params={"board": "", "swimlane": "component"})
    # The sidebar's Board <select> resubmits the whole form -- carrying the
    # stale swimlane=component along -- when switching to a different,
    # never-visited board.
    client.get("/", params={"board": "Modified Items", "swimlane": "component"})

    raw_cookie = http.cookies.SimpleCookie()
    raw_cookie.load(f"jira_wb_last_view={client.cookies['jira_wb_last_view']}")
    cookie = json_module.loads(raw_cookie["jira_wb_last_view"].value)
    # The stale swimlane=component from the previous board must not have
    # been saved under the new board's own remembered state.
    assert "swimlane=component" not in cookie["per_board"]["Modified Items"]


def test_items_page_bare_reload_restores_the_currently_active_boards_filters(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    client.get("/", params={"board": "", "swimlane": "component"})
    client.get("/", params={"board": "Modified Items"})
    response = client.get("/")

    # The *last active* board (Modified Items) wins on a bare reload, not
    # the first board visited in this session.
    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert 'id="sidebar-board-scope"' not in aside


def test_items_page_old_format_cookie_does_not_crash(tmp_path: Path) -> None:
    # A cookie set before this per-board JSON scheme existed was a bare
    # urlencoded query string, not JSON -- must be treated as "no cookie
    # yet", not crash the request.
    client = _synced_client(tmp_path)
    client.cookies.set("jira_wb_last_view", "project=SAT&swimlane=component")

    response = client.get("/")

    assert response.status_code == 200


def test_item_detail_page_shows_edit_forms_for_editable_fields(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    assert 'action="/items/SAT-1/fields"' in response.text
    assert 'action="/items/SAT-1/status"' in response.text
    assert 'action="/items/SAT-1/comments"' in response.text


def test_item_detail_page_hides_revert_and_commit_when_there_is_no_shadow(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    assert 'action="/items/SAT-1/revert"' not in response.text
    assert 'action="/items/SAT-1/push"' not in response.text
    assert 'class="local-diff"' not in response.text


def test_item_detail_page_shows_revert_and_commit_once_a_shadow_exists(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.get("/items/SAT-1")

    revert_start = response.text.index('action="/items/SAT-1/revert"')
    revert_form = response.text[revert_start : revert_start + 300]
    assert 'title="Discard all local unpushed changes for SAT-1"' in revert_form
    assert ">Revert<" in revert_form
    push_start = response.text.index('action="/items/SAT-1/push"')
    push_form = response.text[push_start : push_start + 300]
    assert 'title="Push local changes for SAT-1 to Jira"' in push_form
    assert ">Commit<" in push_form
    assert "Local diff" in response.text


def test_item_detail_page_push_button_shows_a_pending_state_on_submit(tmp_path: Path) -> None:
    # The only feedback during a push used to be the browser tab's own
    # loading spinner -- easy to mistake for the click not registering at
    # all. jiraWbPending swaps the button for an inline spinner as soon as
    # the form submits.
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.get("/items/SAT-1")

    push_start = response.text.index('action="/items/SAT-1/push"')
    push_form = response.text[push_start : push_start + 300]
    assert "jiraWbPending(this)" in push_form
    assert "function jiraWbPending(" in response.text


def test_item_detail_page_highlights_a_locally_modified_field(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.get("/items/SAT-1")

    summary_start = response.text.index('class="detail-summary')
    assert "modified" in response.text[summary_start : summary_start + 40]
    # Priority wasn't touched -- its grid cell must not get the class too.
    priority_start = response.text.index('name="field" value="priority"')
    priority_div_start = response.text.rindex('<div class="detail-field', 0, priority_start)
    assert "modified" not in response.text[priority_div_start : priority_div_start + 40]


def test_item_detail_page_shows_a_badge_beside_a_modified_fields_label(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "priority", {"name": "High"})

    response = client.get("/items/SAT-1")

    priority_start = response.text.index('name="field" value="priority"')
    priority_div_start = response.text.rindex('<div class="detail-field', 0, priority_start)
    priority_div = response.text[priority_div_start:priority_start]
    assert 'class="modified-badge"' in priority_div
    # Assignee wasn't touched -- no badge should show up in its own label.
    assignee_start = response.text.index('name="field" value="assignee"')
    assignee_div_start = response.text.rindex('<div class="detail-field', 0, assignee_start)
    assignee_div = response.text[assignee_div_start:assignee_start]
    assert 'class="modified-badge"' not in assignee_div


def test_item_detail_page_status_select_reveals_a_resolution_dropdown(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    status_form_start = response.text.index('action="/items/SAT-1/status"')
    status_form = response.text[status_form_start : status_form_start + 800]
    assert "Change status</button>" not in status_form
    assert "<select" in status_form
    assert "onchange=" in status_form
    assert "prompt(" not in status_form
    assert 'class="resolution-picker"' in status_form
    assert 'name="resolution"' in status_form


def test_item_detail_page_status_select_marks_done_category_statuses(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    issue_path = tmp_path / "components/api-team/SAT-1/issue.json"
    issue = read_json(issue_path)
    issue["fields"]["status"] = {"name": "Done", "statusCategory": {"key": "done"}}
    write_json(issue_path, issue)
    build_manifest(tmp_path)

    response = client.get("/items/SAT-1")

    status_form_start = response.text.index('action="/items/SAT-1/status"')
    status_form = response.text[status_form_start : status_form_start + 800]
    data_done_attr = html.unescape(status_form.split('data-done="')[1].split('"')[0])
    assert "Done" in data_done_attr


def test_item_detail_page_text_fields_auto_save_on_blur_with_no_save_button(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    assert "Save</button>" not in response.text
    assert "Save edit</button>" not in response.text
    # Isolate the Summary field's own <form> so this specifically proves
    # _text_editor_html's onblur guard, not some other row's markup that
    # happens to contain the same literal substring elsewhere on the page.
    summary_form_start = response.text.index('name="field" value="summary"')
    summary_form = response.text[summary_form_start : summary_form_start + 300]
    assert 'onblur="if(this.value!==this.defaultValue)this.form.submit()"' in summary_form


def test_item_detail_page_labels_field_is_a_tag_input_not_checkboxes(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    labels_section_start = response.text.index('<div class="detail-field-full">')
    labels_section = response.text[labels_section_start : labels_section_start + 500]
    assert 'type="checkbox"' not in labels_section
    assert 'name="value"' in labels_section
    assert "<datalist" in labels_section


def test_item_detail_page_type_field_is_an_icon_dropdown(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    type_form_start = response.text.index('name="field" value="type"')
    type_form = response.text[type_form_start : type_form_start + 1200]
    # A native <select> can't render the real per-type SVG icon inside its
    # own <option> list, so Type is a custom icon-select widget instead --
    # a hidden input still carries the value posted to /items/{key}/fields.
    assert 'class="icon-select"' in type_form
    assert 'role="listbox"' in type_form
    assert "<svg" in type_form
    assert 'input type="hidden" name="value" value="Task"' in type_form
    assert 'data-value="Task"' in type_form


def test_item_detail_page_icon_dropdown_has_an_icon_per_option(tmp_path: Path) -> None:
    # SAT-1/SAT-2 (the fixture) are both type "Task" -- add a second synced
    # type so this can prove every *option*, not just the trigger, gets its
    # own icon while the dropdown is open.
    client = _synced_client(tmp_path)
    write_json(
        tmp_path / "components/other/SAT-3/issue.json",
        {"key": "SAT-3", "fields": {"summary": "An epic", "issuetype": {"name": "Epic"}, "status": {"name": "Open"}}},
    )
    build_manifest(tmp_path)

    response = client.get("/items/SAT-1")

    type_form_start = response.text.index('name="field" value="type"')
    type_form = response.text[type_form_start : type_form_start + 1200]
    assert "Epic" in type_form
    assert type_form.count("<svg") > 1


def test_item_detail_page_ships_icon_select_script_only_when_needed(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    editable_response = client.get("/items/SAT-1")
    assert "<script>" in editable_response.text
    assert "function jiraWbIconToggle" in editable_response.text
    assert "function jiraWbIconSelect" in editable_response.text

    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))
    read_only_response = client.get("/items/SAT-1")
    assert "<script>" not in read_only_response.text
    assert "function jiraWbIconToggle" not in read_only_response.text


def test_item_detail_page_priority_dropdown_uses_the_same_svg_icons_as_the_items_list(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    priority_form_start = response.text.index('name="field" value="priority"')
    priority_form = response.text[priority_form_start : priority_form_start + 1200]
    # Real colored SVG icons (PRIORITY_ICONS_SVG, same ones the Items list
    # table renders) for every option, not an emoji substitute.
    assert 'class="icon-select"' in priority_form
    assert "<svg" in priority_form
    assert priority_form.count("<svg") > 1
    assert "⚪" not in priority_form
    assert 'input type="hidden" name="value" value="Medium"' in priority_form


def test_item_detail_page_assignee_shows_an_avatar_circle(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    assignee_start = response.text.index('name="field" value="assignee"')
    assignee_widget = response.text[assignee_start : assignee_start + 1200]
    assert 'class="avatar-circle"' in assignee_widget
    assert ">SC<" in assignee_widget
    trigger_start = assignee_widget.index('class="icon-select-trigger"')
    trigger_end = assignee_widget.index("</button>", trigger_start)
    # The closed trigger is avatar-only -- no "Serge Colle" name text --
    # but the option row underneath still carries the name so picking a
    # different assignee from the list is never ambiguous.
    assert ">Serge Colle<" not in assignee_widget[trigger_start:trigger_end]
    assert ">Serge Colle<" in assignee_widget[trigger_end:]


def test_item_detail_page_assignee_shows_a_real_photo_when_jira_has_one(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    issue_path = next((tmp_path / "components").glob("*/SAT-1/issue.json"))
    issue = read_json(issue_path)
    issue["fields"]["assignee"] = {
        "displayName": "Serge Colle",
        "avatarUrls": {"32x32": "https://example.com/serge-32.png"},
    }
    write_json(issue_path, issue)
    build_manifest(tmp_path)

    response = client.get("/items/SAT-1")

    assignee_start = response.text.index('name="field" value="assignee"')
    assignee_widget = response.text[assignee_start : assignee_start + 1200]
    assert '<img class="avatar-circle" src="https://example.com/serge-32.png"' in assignee_widget


def test_item_detail_page_parent_dropdown_shows_emoji_not_type_word(tmp_path: Path) -> None:
    # SAT-1's parent (SAT-100) is embedded on SAT-1's own synced issue but
    # was never independently fetchable (see FakeJiraClient), so it can't
    # appear as a real candidate in parent_options() -- this also exercises
    # the "current value not among the candidates" fallback.
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    parent_form_start = response.text.index('name="field" value="parent"')
    parent_form = response.text[parent_form_start : parent_form_start + 400]
    assert "<select" in parent_form
    assert 'value="SAT-100" selected' in parent_form
    assert "Epic:" not in parent_form


def test_item_detail_page_uses_a_compact_fields_grid(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    assert '<div class="detail-fields">' in response.text
    assert '<div class="detail-field">' in response.text
    assert '<div class="detail-field-full">' in response.text


def test_item_detail_page_scopes_options_to_the_items_own_project(tmp_path: Path) -> None:
    # Labels/Type/Priority/Component option lists must only reflect this
    # project's own synced issues, not every locally synced project's --
    # otherwise SAT's editors would offer PLAT's labels/types/components too.
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    write_json(
        tmp_path / "components/other-project/PLAT-1/issue.json",
        {
            "key": "PLAT-1",
            "fields": {
                "summary": "A PLAT issue",
                "issuetype": {"name": "Epic"},
                "status": {"name": "Open"},
                "labels": ["plat-only-label"],
                "priority": {"name": "Medium"},
            },
        },
    )
    build_manifest(tmp_path)
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),)
    )
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/items/SAT-1")

    assert "plat-only-label" not in response.text
    type_form_start = response.text.index('name="field" value="type"')
    type_form = response.text[type_form_start : type_form_start + 1200]
    # Positive assertion alongside the negative one -- SAT-1's own type
    # ("Task") must still appear in the icon-select widget, so a scoping
    # bug that empties the options list entirely (falling back to a plain
    # text input, which would also trivially lack "Epic") fails this too.
    assert 'class="icon-select"' in type_form
    assert "Task" in type_form
    assert "Epic" not in type_form


def test_post_item_field_saves_a_text_field(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post(
        "/items/SAT-1/fields", data={"field": "summary", "value": "Edited via web"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "flash=Saved" in response.headers["location"]
    shadow = load_shadow(tmp_path, "SAT-1")
    assert shadow["fields"]["summary"] == "Edited via web"


def test_post_item_field_saves_a_multi_value_labels_field(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post(
        "/items/SAT-1/fields",
        data={"field": "labels", "value": ["alpha", "beta"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    shadow = load_shadow(tmp_path, "SAT-1")
    assert shadow["fields"]["labels"] == ["alpha", "beta"]


def _mark_status_observed_as_done(tmp_path: Path, key: str, status_name: str) -> None:
    # observed_status_category_map (used to decide whether a resolution
    # applies) reads every locally synced issue's own status dict for a
    # real statusCategory -- not the manifest -- so the fixture issue.json
    # itself needs one of the statuses it's testing to actually carry
    # statusCategory.key == "done".
    path = tmp_path / "components/api-team/SAT-1/issue.json"
    issue = read_json(path)
    issue["fields"]["status"] = {"name": status_name, "statusCategory": {"key": "done"}}
    write_json(path, issue)
    build_manifest(tmp_path)


def test_post_item_status_change_with_resolution_on_a_done_status(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    _mark_status_observed_as_done(tmp_path, "SAT-1", "Done")

    response = client.post(
        "/items/SAT-1/status", data={"status": "Done", "resolution": "Fixed"}, follow_redirects=False
    )

    assert response.status_code == 303
    shadow = load_shadow(tmp_path, "SAT-1")
    assert shadow["fields"]["status"] == "Done"
    assert shadow["statusChange"] == {"resolution": "Fixed"}


def test_post_item_status_change_to_non_done_status_clears_stale_resolution(tmp_path: Path) -> None:
    # Deliberate fix over the TUI's own _edit_status, which only clears a
    # resolution when a new one is given -- moving off a done status must
    # actively clear whatever resolution is already recorded, not leave it
    # lingering in the shadow.
    client = _synced_client(tmp_path)
    _mark_status_observed_as_done(tmp_path, "SAT-1", "Done")
    set_field(tmp_path, "SAT-1", "status", "Done")
    set_status_change(tmp_path, "SAT-1", resolution="Fixed")

    response = client.post("/items/SAT-1/status", data={"status": "Open"}, follow_redirects=False)

    assert response.status_code == 303
    shadow = load_shadow(tmp_path, "SAT-1")
    assert "statusChange" not in shadow


def test_post_item_comment_adds_a_new_local_comment(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post("/items/SAT-1/comments", data={"body": "A new comment"}, follow_redirects=False)

    assert response.status_code == 303
    shadow = load_shadow(tmp_path, "SAT-1")
    assert shadow["comments"][0]["body"] == "A new comment"


def test_post_item_comment_rejects_empty_body(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post("/items/SAT-1/comments", data={"body": "   "}, follow_redirects=False)

    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert load_shadow(tmp_path, "SAT-1") is None


def test_post_item_comment_delete_discards_a_local_new_comment(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    client.post("/items/SAT-1/comments", data={"body": "temp"}, follow_redirects=False)
    shadow = load_shadow(tmp_path, "SAT-1")
    comment_id = shadow["comments"][0]["id"]

    response = client.post(f"/items/SAT-1/comments/{comment_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "discarded" in response.headers["location"].lower()
    shadow = load_shadow(tmp_path, "SAT-1")
    assert shadow["comments"] == []


def test_post_item_comment_delete_then_undelete_a_synced_comment(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    # Written after sync -- sync_project's own comment fetch would
    # otherwise overwrite a pre-sync fixture file with whatever
    # FakeJiraClient returns for comments.
    write_json(
        tmp_path / "components/api-team/SAT-1/comments.json",
        {"comments": [{"id": "555", "body": "synced comment", "author": {"displayName": "Alex"}, "created": "2026-01-01"}]},
    )

    delete_response = client.post("/items/SAT-1/comments/555/delete", follow_redirects=False)
    assert delete_response.status_code == 303
    assert load_shadow(tmp_path, "SAT-1")["commentDeletes"] == ["555"]

    undelete_response = client.post("/items/SAT-1/comments/555/delete", follow_redirects=False)
    assert undelete_response.status_code == 303
    assert "555" not in load_shadow(tmp_path, "SAT-1").get("commentDeletes", [])


def test_post_item_comment_edit_updates_body(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    write_json(
        tmp_path / "components/api-team/SAT-1/comments.json",
        {"comments": [{"id": "555", "body": "original", "author": {"displayName": "Alex"}, "created": "2026-01-01"}]},
    )

    response = client.post("/items/SAT-1/comments/555/edit", data={"body": "updated"}, follow_redirects=False)

    assert response.status_code == 303
    assert load_shadow(tmp_path, "SAT-1")["commentEdits"]["555"] == "updated"


def test_post_item_revert_deletes_the_shadow(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.post("/items/SAT-1/revert", follow_redirects=False)

    assert response.status_code == 303
    assert load_shadow(tmp_path, "SAT-1") is None


def test_post_item_push_succeeds_and_clears_the_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    set_field(tmp_path, "SAT-1", "summary", "Edited via web")
    push_client = PushJiraClient(updated="2026-07-20T00:00:01.000+0000")
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: push_client)

    response = client.post("/items/SAT-1/push", follow_redirects=False)

    assert response.status_code == 303
    assert "pushed" in response.headers["location"].lower()
    assert load_shadow(tmp_path, "SAT-1") is None
    assert push_client.update_calls


def test_post_item_push_without_jira_credentials_flashes_an_error(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.post("/items/SAT-1/push", follow_redirects=False)

    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert load_shadow(tmp_path, "SAT-1") is not None


def test_diff_display_value_normalizes_every_shape_a_field_value_takes() -> None:
    # Both Jira's own raw JSON shapes ({"name": ...}, {"value": ...},
    # {"key": ...} for parent, lists of dicts for fixVersions/components)
    # and a shadow's already-encoded edit (view.py's encode_edit_value:
    # plain strings, or lists of plain strings for labels) must render as
    # the same kind of plain display string.
    assert _diff_display_value(None) == "(none)"
    assert _diff_display_value("") == "(none)"
    assert _diff_display_value("Open") == "Open"
    assert _diff_display_value({"name": "High"}) == "High"
    assert _diff_display_value({"value": "helm-chart"}) == "helm-chart"
    assert _diff_display_value({"key": "SAT-100"}) == "SAT-100"
    assert _diff_display_value({}) == "(none)"
    assert _diff_display_value([]) == "(none)"
    assert _diff_display_value(["urgent", "bug"]) == "urgent, bug"
    assert _diff_display_value([{"name": "3.4.0"}, {"name": "3.5.0"}]) == "3.4.0, 3.5.0"


def test_push_review_page_shows_a_diff_per_modified_issue(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited summary")

    response = client.get("/items/push-review")

    assert response.status_code == 200
    assert "1 locally modified issue" in response.text
    # The key alone doesn't say what the issue is about -- the header
    # shows key + summary (here, the shadow-merged/new summary, since
    # summary is itself the field being changed).
    assert "<summary>SAT-1 — Edited summary</summary>" in response.text
    assert 'action="/items/push-all"' in response.text


def test_push_review_page_push_button_shows_a_pending_state_on_submit(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited summary")

    response = client.get("/items/push-review")

    push_start = response.text.index('action="/items/push-all"')
    push_form = response.text[push_start : push_start + 400]
    assert "jiraWbPending(this" in push_form
    assert "function jiraWbPending(" in response.text


def test_push_review_page_diff_is_human_friendly_not_a_unified_diff(tmp_path: Path) -> None:
    # The old rendering (shadow.py's render_diff, still used by the Detail
    # page's own "Local diff" and the CLI) is a unified diff of pretty-
    # printed JSON -- not what a browser reader wants to parse for a
    # one-word field change.
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "New summary text")

    response = client.get("/items/push-review")

    assert "---" not in response.text
    assert "+++" not in response.text
    assert "@@" not in response.text
    assert 'class="diff-table"' in response.text
    assert "<th>Summary</th>" in response.text
    assert 'class="diff-before"' in response.text
    assert 'class="diff-after">New summary text</td>' in response.text


def test_push_review_page_diff_columns_align_the_same_across_every_issue(tmp_path: Path) -> None:
    # Each modified issue gets its own separate diff table -- without a
    # fixed layout + explicit column widths, the browser sizes each one's
    # columns from only its own content, so "Before"/"After" would land at
    # a different x position per issue depending on how long that one
    # issue's own values happen to be.
    client = _synced_client(tmp_path)

    response = client.get("/items/push-review")

    assert "table-layout: fixed" in response.text
    assert "diff-table th:first-child" in response.text


def test_push_review_page_shows_the_original_value_as_before(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "priority", "Low")

    response = client.get("/items/push-review")

    diff_start = response.text.index('<summary>SAT-1 — Summary for SAT-1</summary>')
    diff_section = response.text[diff_start : diff_start + 800]
    assert "<th>Priority</th>" in diff_section
    assert 'class="diff-before">Medium</td>' in diff_section
    assert 'class="diff-after">Low</td>' in diff_section


def test_push_review_page_labels_a_custom_component_field_as_component(tmp_path: Path) -> None:
    # "customfield_10071" (SAT's configured component field, see
    # _synced_client) has no display name of its own without a live
    # createmeta/editmeta fetch -- it should still read as "Component",
    # not the raw field id.
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "customfield_10071", "helm-chart")

    response = client.get("/items/push-review")

    diff_start = response.text.index("<summary>SAT-1 — Summary for SAT-1</summary>")
    diff_section = response.text[diff_start : diff_start + 500]
    assert "<th>Component</th>" in diff_section
    assert "customfield_10071" not in diff_section


def test_push_review_page_shows_comments_and_resolution_as_plain_text(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    add_comment(tmp_path, "SAT-1", "A staged comment")
    _mark_status_observed_as_done(tmp_path, "SAT-1", "Done")
    set_field(tmp_path, "SAT-1", "status", "Done")
    set_status_change(tmp_path, "SAT-1", resolution="Fixed")

    response = client.get("/items/push-review")

    diff_start = response.text.index('<summary>SAT-1 — Summary for SAT-1</summary>')
    diff_section = response.text[diff_start : diff_start + 1500]
    assert "<strong>Comments:</strong>" in diff_section
    assert "New comment: A staged comment" in diff_section
    assert "<strong>Resolution:</strong> Fixed" in diff_section


def test_push_review_page_with_nothing_modified(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/push-review")

    assert response.status_code == 200
    assert "Nothing to push" in response.text
    assert 'action="/items/push-all"' not in response.text


def test_push_review_page_back_link_uses_return_to(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.get("/items/push-review", params={"return_to": "/?project=SAT"})

    assert 'href="/?project=SAT"' in response.text
    assert 'name="return_to" value="/?project=SAT"' in response.text


def test_post_push_all_modified_pushes_every_shadow_and_clears_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    set_field(tmp_path, "SAT-1", "summary", "Edited one")
    set_field(tmp_path, "SAT-2", "summary", "Edited two")
    push_client = PushJiraClient(
        updated={"SAT-1": "2026-07-20T00:00:01.000+0000", "SAT-2": "2026-07-20T00:00:02.000+0000"}
    )
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: push_client)

    response = client.post("/items/push-all", data={"return_to": "/"}, follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/?")
    assert "2+pushed" in location
    assert load_shadow(tmp_path, "SAT-1") is None
    assert load_shadow(tmp_path, "SAT-2") is None


def test_post_push_all_modified_with_nothing_staged(tmp_path: Path) -> None:
    client = _synced_client(tmp_path, _pushable_config())

    response = client.post("/items/push-all", data={"return_to": "/"}, follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert "Nothing+to+push" in location
    assert "flash_kind=success" in location


def test_post_push_all_modified_without_return_to_goes_to_the_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    set_field(tmp_path, "SAT-1", "summary", "Edited")
    push_client = PushJiraClient(updated="2026-07-20T00:00:01.000+0000")
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: push_client)

    response = client.post("/items/push-all", follow_redirects=False)

    assert response.headers["location"].startswith("/?")


def test_read_only_project_hides_field_edit_forms_but_keeps_comments_and_push(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))
    # Comments are always allowed even in a read-only project, and having
    # one queued is what gives Push something to do -- otherwise it's
    # correctly hidden (no shadow at all).
    add_comment(tmp_path, "SAT-1", "still allowed")

    response = client.get("/items/SAT-1")

    assert "read-only" in response.text.lower()
    assert 'action="/items/SAT-1/fields"' not in response.text
    assert 'action="/items/SAT-1/comments"' in response.text
    assert 'action="/items/SAT-1/push"' in response.text


def test_read_only_project_blocks_field_edit_via_post_but_allows_comments(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))

    field_response = client.post(
        "/items/SAT-1/fields", data={"field": "summary", "value": "nope"}, follow_redirects=False
    )
    comment_response = client.post("/items/SAT-1/comments", data={"body": "still allowed"}, follow_redirects=False)

    assert "flash_kind=error" in field_response.headers["location"]
    assert "read-only" in field_response.headers["location"].lower()
    assert "flash_kind=error" not in comment_response.headers["location"]


def test_write_route_rejects_mismatched_origin(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post(
        "/items/SAT-1/revert",
        headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_write_route_allows_same_origin_referer(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post(
        "/items/SAT-1/revert",
        headers={"Referer": "http://testserver/items/SAT-1"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_post_item_field_not_synced_returns_404(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.post("/items/SAT-999/fields", data={"field": "summary", "value": "x"})

    assert response.status_code == 404


class CreateIssueJiraClient(PushJiraClient):
    def __init__(self, create_fields_by_type: dict[str, dict[str, object]], new_key: str = "SAT-999") -> None:
        super().__init__(updated="irrelevant")
        self.create_fields_by_type = create_fields_by_type
        self.new_key = new_key
        self.created_fields: dict[str, object] | None = None
        self.createmeta_calls = 0

    def issue_createmeta(self, project: str) -> dict[str, object]:
        self.createmeta_calls += 1
        return {
            "projects": [
                {
                    "issuetypes": [
                        {"name": name, "fields": fields} for name, fields in self.create_fields_by_type.items()
                    ]
                }
            ]
        }

    def issue_create(self, fields: dict[str, object]) -> dict[str, object]:
        self.created_fields = fields
        return {"key": self.new_key}

    def myself(self) -> dict[str, object]:
        return {"accountId": "acc-1", "displayName": "Serge Colle"}


class AgileCreateJiraClient(CreateIssueJiraClient):
    """Adds the Agile board/move surface refresh_boards_api and
    move_issue_to_backlog/board need, on top of CreateIssueJiraClient's
    existing plain-create support -- for testing the scoped "create
    directly into a section" flow end to end."""

    def __init__(self, *args: object, fail_move: bool = False, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fail_move = fail_move
        self.move_calls: list[tuple[object, ...]] = []

    def get_all_agile_boards(self, project_key: str | None = None) -> dict[str, object]:
        return {"values": [{"id": 32, "name": "SAT board", "type": "simple"}]}

    def get_agile_board_configuration(self, board_id: object) -> dict[str, object]:
        return {"filter": {"id": "10089"}}

    def get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
        if path == "rest/api/2/filter/10089":
            return {"jql": "project = SAT"}
        if path in ("rest/agile/1.0/board/32/backlog", "rest/agile/1.0/board/32/issue"):
            return {"total": 0, "issues": []}
        raise AssertionError(f"unexpected get() path in test: {path}")

    def move_issues_to_backlog(self, issue_keys: list) -> None:
        self.move_calls.append(("backlog", tuple(issue_keys)))
        if self.fail_move:
            raise RuntimeError("simulated move failure")

    def get_agile_resource_url(self, resource: str) -> str:
        return f"rest/agile/1.0/{resource}"

    def post(self, path: str, json: dict | None = None) -> None:
        self.move_calls.append(("board", path, json))
        if self.fail_move:
            raise RuntimeError("simulated move failure")


def _write_scoped_board_cache(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {"project": "SAT", "boards": [{"id": 32, "name": "SAT board", "backlogKeys": [], "boardKeys": []}]},
    )


def test_post_new_issue_moves_a_scoped_create_into_the_backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    _write_scoped_board_cache(tmp_path)
    create_client = AgileCreateJiraClient({"Task": {"summary": {}, "description": {}}}, new_key="SAT-999")
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={
            "project": "SAT",
            "type": "Task",
            "summary": "A new task",
            "description": "Some details",
            "board": "SAT board",
            "target_scope": "backlog",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert create_client.move_calls == [("backlog", ("SAT-999",))]
    assert "flash_kind=error" not in response.headers["location"]


def test_post_new_issue_moves_a_scoped_create_onto_the_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    _write_scoped_board_cache(tmp_path)
    create_client = AgileCreateJiraClient({"Task": {"summary": {}, "description": {}}}, new_key="SAT-999")
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={
            "project": "SAT",
            "type": "Task",
            "summary": "A new task",
            "description": "Some details",
            "board": "SAT board",
            "target_scope": "active",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert create_client.move_calls == [("board", "rest/agile/1.0/board/32/issue", {"issues": ["SAT-999"]})]


def test_post_new_issue_surfaces_a_warning_when_the_scoped_move_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    _write_scoped_board_cache(tmp_path)
    create_client = AgileCreateJiraClient(
        {"Task": {"summary": {}, "description": {}}}, new_key="SAT-999", fail_move=True
    )
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={
            "project": "SAT",
            "type": "Task",
            "summary": "A new task",
            "description": "Some details",
            "board": "SAT board",
            "target_scope": "backlog",
        },
        follow_redirects=False,
    )

    # The issue was still created -- a failed scope move is a warning, not
    # a failed create, and lands on the issue's own Detail page (no
    # return_to was given in this request) with a clickable warning.
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/items/SAT-999")
    assert "flash_kind=warning" in location


def test_new_issue_page_shows_scoped_create_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get(
        "/items/new", params={"project": "SAT", "board": "SAT board", "target_scope": "backlog"}
    )

    assert response.status_code == 200
    assert "Will be created in: Backlog of SAT board" in response.text
    assert 'name="board" value="SAT board"' in response.text
    assert 'name="target_scope" value="backlog"' in response.text


class BoardPositionJiraClient(PushJiraClient):
    """Fake for POST /items/{key}/board-position -- the move/rank surface
    plus get_all_fields (rank field id discovery)."""

    def __init__(self, *, rank_field: bool = True) -> None:
        super().__init__(updated="irrelevant")
        self.move_calls: list[tuple[object, ...]] = []
        self.rank_calls: list[tuple[object, ...]] = []
        self.rank_field = rank_field

    def move_issues_to_backlog(self, issue_keys: list) -> None:
        self.move_calls.append(("backlog", tuple(issue_keys)))

    def get_agile_resource_url(self, resource: str) -> str:
        return f"rest/agile/1.0/{resource}"

    def post(self, path: str, json: dict | None = None) -> None:
        self.move_calls.append(("board", path, json))

    def update_rank(self, issues_to_rank: list, rank_before: str, customfield_number: str) -> None:
        self.rank_calls.append((tuple(issues_to_rank), rank_before, customfield_number))

    def get_all_fields(self) -> list[dict[str, str]]:
        return [{"id": "customfield_10019", "name": "Rank"}] if self.rank_field else []


def _write_position_board_cache(tmp_path: Path, *, backlog_keys=None, board_keys=None) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {
            "project": "SAT",
            "boards": [
                {
                    "id": 32,
                    "name": "SAT board",
                    "backlogKeys": backlog_keys or [],
                    "boardKeys": board_keys or [],
                }
            ],
        },
    )


def test_post_item_board_position_moves_scope_and_ranks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    _write_position_board_cache(tmp_path, backlog_keys=["SAT-1"], board_keys=["SAT-2"])
    fake = BoardPositionJiraClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/items/SAT-1/board-position",
        data={"project": "SAT", "board": "SAT board", "target_scope": "active", "before_key": "SAT-2"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert fake.move_calls == [("board", "rest/agile/1.0/board/32/issue", {"issues": ["SAT-1"]})]
    assert fake.rank_calls == [(("SAT-1",), "SAT-2", "customfield_10019")]

    cache = read_json(tmp_path / "meta/SAT/boards.json")
    board = cache["boards"][0]
    assert board["backlogKeys"] == []
    assert board["boardKeys"] == ["SAT-1", "SAT-2"]


def test_post_item_board_position_rank_only_when_scope_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    _write_position_board_cache(tmp_path, board_keys=["SAT-2", "SAT-1"])
    fake = BoardPositionJiraClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/items/SAT-1/board-position",
        data={"project": "SAT", "board": "SAT board", "target_scope": "active", "before_key": "SAT-2"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    # Already "active" (present in boardKeys, absent from backlogKeys) --
    # no move call, only a rank call.
    assert fake.move_calls == []
    assert fake.rank_calls == [(("SAT-1",), "SAT-2", "customfield_10019")]


def test_post_item_board_position_returns_error_json_when_board_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    fake = BoardPositionJiraClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/items/SAT-1/board-position",
        data={"project": "SAT", "board": "No Such Board", "target_scope": "active"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "No Such Board" in body["error"]
    assert fake.move_calls == []


def test_post_item_board_position_skips_ranking_when_no_rank_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    _write_position_board_cache(tmp_path, backlog_keys=["SAT-1", "SAT-2"])
    fake = BoardPositionJiraClient(rank_field=False)
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/items/SAT-1/board-position",
        data={"project": "SAT", "board": "SAT board", "target_scope": "backlog", "before_key": "SAT-2"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert fake.rank_calls == []


def test_new_issue_page_without_jira_credentials_shows_unavailable_message(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/items/new", params={"project": "SAT"})

    assert response.status_code == 200
    assert "Cannot create an issue here" in response.text


def test_new_issue_page_shows_type_select_before_a_type_is_chosen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT"})

    assert response.status_code == 200
    # Type is the same custom icon-select widget as the Detail/Items-list
    # pages, not a plain <option> list -- a hidden input still carries the
    # real value, with one data-value="Task" option row in the listbox.
    assert 'class="icon-select"' in response.text
    assert 'name="type"' in response.text
    assert 'data-value="Task"' in response.text
    assert 'name="priority"' not in response.text


def test_new_issue_page_shows_type_specific_fields_once_a_type_is_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient(
        {"Task": {"summary": {}, "description": {}, "priority": {}, "labels": {}}}
    )
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "type": "Task"})

    assert response.status_code == 200
    assert 'name="priority"' in response.text
    assert 'name="labels"' in response.text
    assert 'name="assignee"' not in response.text


def test_post_new_issue_creates_and_redirects_to_the_new_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}, "priority": {}}}, new_key="SAT-999")
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={
            "project": "SAT",
            "type": "Task",
            "summary": "A new task",
            "description": "Some details",
            "priority": "High",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/items/SAT-999")
    assert create_client.created_fields["summary"] == "A new task"
    assert create_client.created_fields["priority"] == {"name": "High"}


def test_post_new_issue_returns_to_the_list_when_return_to_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}}, new_key="SAT-999")
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={
            "project": "SAT",
            "type": "Task",
            "summary": "A new task",
            "description": "Some details",
            "return_to": "/?project=SAT",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/?project=SAT&")
    assert "flash=SAT-999+created" in location
    assert "flash_kind=success" in location


def test_post_new_issue_warns_when_the_created_item_does_not_match_the_list_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A filter that doesn't happen to include the project/status/component
    # you just picked is a completely ordinary thing to have active -- the
    # new issue would otherwise just silently vanish from the list you land
    # back on, which reads as "creation failed" even though it didn't.
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}}, new_key="SAT-999")
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={
            "project": "SAT",
            "type": "Task",
            "summary": "A new task",
            "description": "Some details",
            "return_to": "/?project=PLAT",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/?project=PLAT&")
    assert "flash_kind=warning" in location
    assert "flash_href=%2Fitems%2FSAT-999" in location

    list_response = client.get(location, follow_redirects=False)
    assert 'class="flash flash-warning"' in list_response.text
    assert '<a href="/items/SAT-999">' in list_response.text


def test_new_issue_page_return_to_round_trips_through_a_type_change_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get(
        "/items/new", params={"project": "SAT", "type": "Task", "return_to": "/?project=SAT&swimlane=component"}
    )

    assert response.status_code == 200
    assert 'name="return_to" value="/?project=SAT&amp;swimlane=component"' in response.text


def test_post_new_issue_requires_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={"project": "SAT", "type": "Task", "summary": "", "description": "desc"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/items/new" in response.headers["location"]
    assert "flash_kind=error" in response.headers["location"]
    assert create_client.created_fields is None


def test_post_new_issue_blocked_for_read_only_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={"project": "SAT", "type": "Task", "summary": "x", "description": "y"},
        follow_redirects=False,
    )

    assert "flash_kind=error" in response.headers["location"]
    assert create_client.created_fields is None


def test_new_issue_page_scopes_component_priority_and_labels_to_the_current_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    write_json(
        tmp_path / "components/other-project/PLAT-1/issue.json",
        {
            "key": "PLAT-1",
            "fields": {
                "summary": "A PLAT issue",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "priority": {"name": "Blocker"},
                "labels": ["plat-only-label"],
                "customfield_10071": {"value": "plat-only-component"},
            },
        },
    )
    build_manifest(tmp_path)
    config = WorkbenchConfig(
        projects=(
            ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),
            ProjectSettings(key="PLAT", component_field="customfield_10071"),
        ),
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )
    client = TestClient(create_app(tmp_path, config))
    create_client = CreateIssueJiraClient(
        {"Task": {"summary": {}, "description": {}, "priority": {}, "labels": {}, "customfield_10071": {}}}
    )
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "type": "Task"})

    assert "plat-only-label" not in response.text
    assert "plat-only-component" not in response.text
    assert "Blocker" not in response.text
    assert "Medium" in response.text  # SAT-1's own priority


def test_new_issue_page_scopes_assignee_options_to_the_current_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    write_json(
        tmp_path / "components/other-project/PLAT-1/issue.json",
        {
            "key": "PLAT-1",
            "fields": {
                "summary": "A PLAT issue",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "assignee": {"displayName": "Cross Project User"},
            },
        },
    )
    build_manifest(tmp_path)
    config = WorkbenchConfig(
        projects=(
            ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),
            ProjectSettings(key="PLAT"),
        ),
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )
    client = TestClient(create_app(tmp_path, config))
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}, "assignee": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "type": "Task"})

    assignee_start = response.text.index('name="assignee"')
    assignee_widget = response.text[assignee_start : assignee_start + 800]
    assert "Serge Colle" in assignee_widget
    assert "Cross Project User" not in assignee_widget


def test_new_issue_page_priority_is_an_icon_dropdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}, "priority": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "type": "Task"})

    priority_start = response.text.index('name="priority"')
    priority_widget = response.text[priority_start : priority_start + 800]
    assert 'class="icon-select"' in response.text[max(0, priority_start - 400) : priority_start + 800]
    assert "<svg" in priority_widget or "<svg" in response.text[max(0, priority_start - 400) : priority_start]


def test_new_issue_page_labels_field_is_a_tag_input_not_checkboxes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}, "labels": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "type": "Task"})

    labels_start = response.text.index('<div class="detail-field-full">')
    labels_section = response.text[labels_start : labels_start + 500]
    assert 'type="checkbox"' not in labels_section
    assert 'name="labels"' in labels_section
    assert "<datalist" in labels_section


def test_new_issue_page_uses_the_compact_fields_grid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}, "priority": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "type": "Task"})

    assert '<div class="detail-fields">' in response.text
    assert '<div class="detail-field">' in response.text
    assert "<table>" not in response.text


def test_new_issue_page_caches_createmeta_across_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A live issue_createmeta call is what made picking a Type feel slow --
    # the Type field auto-submits a reload on every change, so without a
    # per-process cache (matching the TUI's own app.issue_type_fields_cache)
    # every single click re-fetched the full create-metadata from Jira.
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    client.get("/items/new", params={"project": "SAT"})
    client.get("/items/new", params={"project": "SAT", "type": "Task"})
    client.post(
        "/items/new",
        data={"project": "SAT", "type": "Task", "summary": "x", "description": "y"},
        follow_redirects=False,
    )

    assert create_client.createmeta_calls == 1


def test_post_new_issue_blocks_a_required_custom_field_left_blank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient(
        {"Task": {"summary": {}, "description": {}, "priority": {"required": True}}}
    )
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={"project": "SAT", "type": "Task", "summary": "x", "description": "y", "priority": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert create_client.created_fields is None


def test_post_new_issue_allows_a_required_custom_field_when_filled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient(
        {"Task": {"summary": {}, "description": {}, "priority": {"required": True}}}
    )
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.post(
        "/items/new",
        data={"project": "SAT", "type": "Task", "summary": "x", "description": "y", "priority": "High"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/items/SAT-999")
    assert create_client.created_fields["priority"] == {"name": "High"}


def test_new_issue_page_marks_a_required_field_for_client_side_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient(
        {"Task": {"summary": {}, "description": {}, "priority": {"required": True}}}
    )
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "type": "Task"})

    priority_start = response.text.index('name="priority"')
    priority_widget = response.text[priority_start : priority_start + 60]
    assert "required" in priority_widget
    assert "jiraWbValidateRequired" in response.text


def test_new_issue_page_prefills_defaults_from_epic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "epic": "SAT-1"})

    assert response.status_code == 200
    assert 'value="SAT-1"' in response.text  # hidden epic field round-trips


def test_items_page_has_new_issue_link(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert 'href="/items/new?return_to=' in response.text


def test_goto_redirects_to_the_matching_item(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/goto", params={"goto_key": "SAT-1"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/items/SAT-1"


def test_goto_normalizes_case_and_whitespace(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/goto", params={"goto_key": "  sat-1  "}, follow_redirects=False)

    assert response.headers["location"] == "/items/SAT-1"


def test_goto_rejects_a_malformed_key(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/goto", params={"goto_key": "not a key"}, follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/?")
    assert "flash_kind=error" in location
    assert "doesn" in location  # "doesn't look like a Jira issue key"


def test_goto_reports_a_key_that_is_not_synced_locally(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/goto", params={"goto_key": "SAT-999"}, follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/?")
    assert "flash_kind=error" in location
    assert "not+synced+locally" in location


def test_goto_with_no_key_just_goes_to_the_list(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/goto", params={"goto_key": ""}, follow_redirects=False)

    assert response.headers["location"] == "/"


def test_items_page_has_a_goto_box_in_the_sidebar(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert 'action="/goto"' in aside
    assert 'name="goto_key"' in aside


def test_item_detail_page_has_a_goto_box_in_the_sidebar_too(tmp_path: Path) -> None:
    # The Goto box lives in page()'s shared sidebar template, not any one
    # page's own sidebar_extra -- it should show up everywhere, not just
    # the Items list.
    client = _synced_client(tmp_path)

    response = client.get("/items/SAT-1")

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert 'action="/goto"' in aside
    assert 'name="goto_key"' in aside


def test_new_issue_page_goto_box_rides_the_shared_outer_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # New Issue's own page still wraps its whole body in one <form
    # method="get"> (form_wrapped defaults to True there) -- the Goto box
    # can't have its own nested <form> in that case, so it instead rides
    # that outer form via a formaction-overridden submit button, the same
    # override trick already used for the page's own "Create issue" button.
    client = _synced_client(tmp_path, _pushable_config())
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT"})

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert "<form" not in aside
    assert 'formaction="/goto"' in aside
    assert 'name="goto_key"' in aside


def test_post_meta_version_add_creates_and_redirects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app(tmp_path, _pushable_config()))
    fake = ApiClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/meta/versions/add", data={"project": "SAT", "name": "kube 2.0.0"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "flash=created" in response.headers["location"]
    assert any(call[0] == "add_version" for call in fake.calls)


def test_post_meta_version_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app(tmp_path, _pushable_config()))
    fake = ApiClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/meta/versions/rename",
        data={"project": "SAT", "identifier": "10000", "new_name": "helm-chart-sa 3.5.0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "flash=renamed" in response.headers["location"]
    assert any(call[0] == "update_version" for call in fake.calls)


def test_post_meta_version_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app(tmp_path, _pushable_config()))
    fake = ApiClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/meta/versions/release", data={"project": "SAT", "identifier": "10000"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]
    assert any(call[0] == "update_version" and call[1][4] is True for call in fake.calls)


def test_post_meta_version_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app(tmp_path, _pushable_config()))
    fake = ApiClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/meta/versions/archive", data={"project": "SAT", "identifier": "10000"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert any(call[0] == "update_version" and call[1][3] is True for call in fake.calls)


def test_post_meta_version_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app(tmp_path, _pushable_config()))
    fake = ApiClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/meta/versions/delete", data={"project": "SAT", "identifier": "10000"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert any(call[0] == "delete_version" for call in fake.calls)


def test_post_meta_version_add_without_credentials_flashes_error(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.post("/meta/versions/add", data={"project": "SAT", "name": "x"}, follow_redirects=False)

    assert "flash_kind=error" in response.headers["location"]


def test_post_meta_component_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app(tmp_path, _pushable_config()))
    fake = ApiClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/meta/components/add", data={"project": "SAT", "name": "new-component"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert any(call[0] == "create_component" for call in fake.calls)


def test_post_meta_component_field_option_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),),
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )
    client = TestClient(create_app(tmp_path, config))
    fake = ApiClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: fake)

    response = client.post(
        "/meta/components/add-option",
        data={"project": "SAT", "name": "puppy", "context_id": "999"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert any(call[0] == "add_custom_field_option" for call in fake.calls)


def test_post_meta_board_add_creates_local_board(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.post(
        "/meta/boards/add",
        data={"project": "SAT", "name": "My Filter", "board_project": "SAT", "status": "To Do, In Progress"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "flash=created" in response.headers["location"]
    from jira_workbench.metadata import load_board_settings

    settings = load_board_settings(tmp_path)
    assert settings["localBoards"][0]["name"] == "My Filter"
    assert settings["localBoards"][0]["fieldFilters"] == {"project": ["SAT"], "status": ["To Do", "In Progress"]}


def test_post_meta_board_delete(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))
    client.post("/meta/boards/add", data={"project": "SAT", "name": "My Filter"}, follow_redirects=False)

    response = client.post("/meta/boards/delete", data={"project": "SAT", "name": "My Filter"}, follow_redirects=False)

    assert response.status_code == 303
    from jira_workbench.metadata import load_board_settings

    assert load_board_settings(tmp_path)["localBoards"] == []


def test_post_meta_board_toggle_active_for_a_jira_board(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.post(
        "/meta/boards/toggle-active",
        data={"project": "SAT", "kind": "jira", "identifier": "32", "active": "0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    from jira_workbench.metadata import load_board_settings

    assert "32" in load_board_settings(tmp_path)["disabledBoardIds"]


def test_post_meta_label_rename_bulk_edits_and_pushes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    # load_manifest_items() re-derives "labels" from each issue's own real
    # synced issue.json (via with_local_index_fields), not from whatever's
    # written into manifest.json directly -- so the fixture issue itself
    # needs the label, then the manifest rebuilt from it.
    issue_path = tmp_path / "components/api-team/SAT-1/issue.json"
    issue = read_json(issue_path)
    issue["fields"]["labels"] = ["urgent"]
    write_json(issue_path, issue)
    build_manifest(tmp_path)
    push_client = PushJiraClient(updated="2026-07-20T00:00:01.000+0000")
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: push_client)

    response = client.post(
        "/meta/labels/rename",
        data={"project": "SAT", "label": "urgent", "new_name": "critical"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]
    shadow = load_shadow(tmp_path, "SAT-1")
    assert shadow is None  # pushed and cleared


def test_post_meta_label_delete_skips_issues_in_read_only_projects(tmp_path: Path) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    issue_path = tmp_path / "components/api-team/SAT-1/issue.json"
    issue = read_json(issue_path)
    issue["fields"]["labels"] = ["urgent"]
    write_json(issue_path, issue)
    build_manifest(tmp_path)
    write_project_registry(tmp_path, (ProjectSettings(key="SAT", read_only=True),))

    response = client.post(
        "/meta/labels/delete", data={"project": "SAT", "label": "urgent"}, follow_redirects=False
    )

    assert "flash_kind=error" in response.headers["location"]
    assert "read-only" in response.headers["location"].lower()
    # never touched -- the project's own read-only registry blocked it before any edit
    assert load_shadow(tmp_path, "SAT-1") is None


def test_post_meta_label_delete_with_no_matching_issues_flashes_error(tmp_path: Path) -> None:
    client = _synced_client(tmp_path, _pushable_config())
    write_json(tmp_path / "manifest.json", {"workItems": []})

    response = client.post(
        "/meta/labels/delete", data={"project": "SAT", "label": "nope"}, follow_redirects=False
    )

    assert "flash_kind=error" in response.headers["location"]


def test_meta_versions_page_shows_add_form_and_row_actions(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {"project": "SAT", "versions": [{"id": "1", "name": "helm-chart-sa 3.4.0", "released": False}]},
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/versions", params={"project": "SAT"})

    assert 'action="/meta/versions/add"' in response.text
    assert 'action="/meta/versions/rename"' in response.text
    assert 'action="/meta/versions/release"' in response.text
    assert 'action="/meta/versions/archive"' in response.text
    assert 'action="/meta/versions/delete"' in response.text


def test_meta_versions_page_hides_archived_versions_by_default(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {
            "project": "SAT",
            "versions": [
                {"id": "1", "name": "sat-current", "released": False, "archived": False},
                {"id": "2", "name": "sat-old", "released": True, "archived": True},
            ],
        },
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/versions", params={"project": "SAT"})

    assert "sat-current" in response.text
    assert "sat-old" not in response.text
    checkbox_start = response.text.index('name="show_archived"')
    assert "checked" not in response.text[checkbox_start : checkbox_start + 40]


def test_meta_versions_page_shows_archived_versions_when_checkbox_is_on(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {
            "project": "SAT",
            "versions": [
                {"id": "1", "name": "sat-current", "released": False, "archived": False},
                {"id": "2", "name": "sat-old", "released": True, "archived": True},
            ],
        },
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/versions", params={"project": "SAT", "show_archived": "1"})

    assert "sat-current" in response.text
    assert "sat-old" in response.text
    checkbox_start = response.text.index('name="show_archived"')
    assert "checked" in response.text[checkbox_start : checkbox_start + 40]


def test_meta_versions_page_archived_checkbox_auto_submits(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/versions", params={"project": "SAT"})

    checkbox_start = response.text.index('name="show_archived"')
    checkbox = response.text[checkbox_start : checkbox_start + 100]
    assert 'onchange="this.form.submit()"' in checkbox


def test_meta_boards_page_shows_add_local_board_form(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/boards", params={"project": "SAT"})

    assert 'action="/meta/boards/add"' in response.text


def test_meta_labels_page_lists_labels_with_actions(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {"workItems": [{"key": "SAT-1", "project": "SAT", "labels": ["urgent", "bug"]}]},
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/labels", params={"project": "SAT"})

    assert response.status_code == 200
    assert "urgent" in response.text
    assert 'action="/meta/labels/rename"' in response.text
    assert 'action="/meta/labels/delete"' in response.text


def test_meta_section_pages_allow_multiple_forms(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/versions", params={"project": "SAT"})

    assert '<div class="layout">' in response.text
    assert '<form method="get" class="layout">' not in response.text


def test_meta_write_route_rejects_mismatched_origin(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.post(
        "/meta/boards/add",
        data={"project": "SAT", "name": "x"},
        headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_meta_page_with_no_projects_shows_placeholder(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta")

    assert response.status_code == 200
    assert "No projects configured" in response.text


def test_meta_page_lists_configured_projects(tmp_path: Path) -> None:
    config = WorkbenchConfig(
        projects=(
            ProjectSettings(key="SAT", default=True),
            ProjectSettings(key="PLAT"),
        )
    )
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/meta")

    assert response.status_code == 200
    assert '<a href="/meta/versions?project=SAT">SAT</a>' in response.text
    assert '<a href="/meta/versions?project=PLAT">PLAT</a>' in response.text


def test_meta_versions_page_renders_versions_for_the_selected_project(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {"project": "SAT", "versions": [{"id": "1", "name": "helm-chart-sa 3.4.0", "released": False}]},
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/versions", params={"project": "SAT"})

    assert response.status_code == 200
    assert "helm-chart-sa 3.4.0" in response.text


def test_meta_components_page_renders_components_for_the_selected_project(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {
            "components": [{"component": "helm-chart", "count": 1}],
            "workItems": [{"key": "SAT-1", "project": "SAT", "component": "helm-chart", "status": "To Do"}],
        },
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/components", params={"project": "SAT"})

    assert response.status_code == 200
    assert "helm-chart" in response.text


def test_meta_components_page_does_not_leak_other_projects_components(tmp_path: Path) -> None:
    # Regression: the manifest's own top-level "components" list is built
    # from the on-disk components/<slug>/<KEY>/ directory structure, which
    # is shared across every synced project -- rendering it unscoped mixed
    # every other project's component values (and their own _unassigned
    # bucket's project-wide counts) into what should have been just SAT's
    # short, real list.
    write_json(
        tmp_path / "manifest.json",
        {
            "workItems": [
                {"key": "SAT-1", "project": "SAT", "component": "helm-chart", "status": "To Do"},
                {"key": "PLAT-1", "project": "PLAT", "component": "plat-only-component", "status": "To Do"},
                {"key": "PLAT-2", "project": "PLAT", "component": "_unassigned", "status": "To Do"},
            ],
        },
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/components", params={"project": "SAT"})

    assert "helm-chart" in response.text
    assert "plat-only-component" not in response.text


def test_meta_boards_page_renders_boards_for_the_selected_project(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {"project": "SAT", "boards": [{"id": "1", "name": "SAT board", "supported": True}]},
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/boards", params={"project": "SAT"})

    assert response.status_code == 200
    assert "SAT board" in response.text


def test_meta_section_pages_without_a_project_show_the_project_picker(tmp_path: Path) -> None:
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", default=True),))
    client = TestClient(create_app(tmp_path, config))

    for section in ("versions", "components", "boards"):
        response = client.get(f"/meta/{section}")
        assert response.status_code == 200
        assert f'<a href="/meta/{section}?project=SAT">SAT</a>' in response.text


def test_meta_boards_page_shows_supported_yes_no_and_jql(tmp_path: Path) -> None:
    # Regression: boards have no "supported" key at all (metadata.py stores
    # `unsupportedReason` instead, None when supported) -- the old generic
    # column renderer just printed item.get("supported", "") for every
    # board, which is always blank.
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {
            "project": "SAT",
            "boards": [
                {
                    "id": 1,
                    "name": "SAT board",
                    "type": "simple",
                    "jql": "project = SAT ORDER BY Rank ASC",
                    "unsupportedReason": None,
                },
                {
                    "id": 2,
                    "name": "Weird board",
                    "type": "simple",
                    "jql": "project = SAT AND sprint in openSprints()",
                    "unsupportedReason": "unsupported JQL construct: IN",
                },
            ],
        },
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/boards", params={"project": "SAT"})

    assert "<td>Yes</td><td><code>project = SAT ORDER BY Rank ASC</code></td>" in response.text
    assert (
        "<td>No</td><td><code>unsupported JQL construct: IN "
        "(jql: project = SAT AND sprint in openSprints())</code></td>" in response.text
    )


def test_meta_versions_page_filters_by_name(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {
            "project": "SAT",
            "versions": [
                {"id": "1", "name": "helm-chart-sa 3.4.0", "released": False},
                {"id": "2", "name": "kube 1.2.0", "released": False},
            ],
        },
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/versions", params={"project": "SAT", "q": "kube"})

    assert "kube 1.2.0" in response.text
    assert "helm-chart-sa 3.4.0" not in response.text


def test_meta_versions_page_filter_with_no_matches_shows_a_message(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {"project": "SAT", "versions": [{"id": "1", "name": "helm-chart-sa 3.4.0", "released": False}]},
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/versions", params={"project": "SAT", "q": "nope"})

    assert "No versions match that filter." in response.text


def test_meta_sidebar_shows_project_and_section_nav(tmp_path: Path) -> None:
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", default=True), ProjectSettings(key="PLAT")))
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/meta/boards", params={"project": "SAT"})

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert '<a href="/meta/boards?project=SAT" class="active">SAT</a>' in aside
    assert '<a href="/meta/boards?project=PLAT">PLAT</a>' in aside
    assert '<a href="/meta/versions?project=SAT">Versions</a>' in aside
    assert '<a href="/meta/components?project=SAT">Components</a>' in aside
    assert '<a href="/meta/boards?project=SAT" class="active">Boards</a>' in aside


def test_items_page_shows_type_and_priority_as_icons_not_text(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    # SAT-1/SAT-2 are both type "Task" and priority "Medium", in a writable
    # project -- shown as pill-select widgets (real SVG icon + visible
    # text on the trigger), not as plain table text.
    assert 'class="pill-select-cell"' in response.text
    assert "<svg" in response.text
    assert "Task" in response.text
    assert "Medium" in response.text
    assert response.text.count("<td>Task</td>") == 0
    assert response.text.count("<td>Medium</td>") == 0


def test_items_page_priority_trigger_is_icon_only_with_a_text_tooltip(tmp_path: Path) -> None:
    # Priority's row trigger is icon-only -- no visible "Medium" label,
    # which would either clip in this narrow column or spill into the
    # next one -- but the value is still discoverable on hover via a
    # native title tooltip (see _pill_html's compact=True) rather than
    # being lost entirely.
    client = _synced_client(tmp_path)

    response = client.get("/")

    priority_start = response.text.index('name="field" value="priority"')
    priority_widget = response.text[priority_start : priority_start + 1200]
    trigger_start = priority_widget.index('class="pill-select-trigger"')
    trigger_end = priority_widget.index("</div>", trigger_start)
    trigger_html = priority_widget[trigger_start:trigger_end]
    assert "<svg" in trigger_html
    assert 'title="Medium"' in trigger_html
    assert "<span>Medium</span>" not in trigger_html
    options_html = _icon_select_template_content(response.text, priority_widget[: trigger_end + len("</div>")])
    assert "Medium" in options_html


def test_items_page_type_trigger_is_icon_only_with_a_tooltip(tmp_path: Path) -> None:
    # Type's row trigger is icon-only too, same as Priority -- no visible
    # "Task" label, discoverable via a title tooltip instead.
    client = _synced_client(tmp_path)

    response = client.get("/")

    type_start = response.text.index('name="field" value="type"')
    type_widget = response.text[type_start : type_start + 1200]
    trigger_start = type_widget.index('class="pill-select-trigger"')
    trigger_end = type_widget.index("</div>", trigger_start)
    trigger_html = type_widget[trigger_start:trigger_end]
    assert "<svg" in trigger_html
    assert 'title="Task"' in trigger_html
    assert "<span>Task</span>" not in trigger_html


def test_items_page_columns_have_explicit_widths_so_summary_gets_the_rest(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert 'class="items-table"' in response.text
    assert "<colgroup>" in response.text
    assert response.text.count("<col") >= 8


def test_items_page_assignee_column_is_narrow_like_priority_not_a_dropdown_width(tmp_path: Path) -> None:
    # Assignee shows an avatar-only trigger now, same as Priority's icon-
    # only trigger -- it shouldn't still reserve the wide column a
    # name-and-icon dropdown used to need.
    client = _synced_client(tmp_path)

    response = client.get("/")

    assignee_start = response.text.index('name="field" value="assignee"')
    assignee_td_start = response.text.rindex("<td", 0, assignee_start)
    assert 'class="pill-select-cell"' in response.text[assignee_td_start : assignee_td_start + 40]


def test_items_page_open_dropdown_is_not_clipped_by_the_row(tmp_path: Path) -> None:
    # .items-table td { overflow: hidden } (added to keep long text from
    # spilling across the fixed-width grid) was clipping the custom icon-
    # select widget's own open dropdown menu too, since that menu is an
    # absolutely-positioned child of the same td that's meant to escape
    # its bounds -- it looked like the dropdown vanished behind the next
    # row instead of opening.
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert "items-table td:not(.pill-select-cell):not(:has(.pill-select-menu)) { overflow: hidden; }" in response.text


def test_items_page_type_trigger_does_not_spill_into_the_key_column(tmp_path: Path) -> None:
    # Excluding icon-select-cell from the td-level overflow:hidden (see
    # the test above) reopened a second problem: Type's own trigger button
    # is width:auto (see .icon-select-cell .icon-select-trigger) and its
    # label ("Task", "Story", ...) can be wider than the narrow Type
    # column -- with nothing left to clip it, that label visibly spilled
    # into the Key column next to it. The trigger itself (not its td) now
    # clips its own overflow, which doesn't touch the sibling dropdown
    # menu at all.
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert ".icon-select-cell .icon-select-trigger {" in response.text
    trigger_rule_start = response.text.index(".icon-select-cell .icon-select-trigger {")
    trigger_rule = response.text[trigger_rule_start : trigger_rule_start + 700]
    assert "overflow: hidden;" in trigger_rule


def test_items_page_sticky_header_has_a_z_index_above_scrolled_rows(tmp_path: Path) -> None:
    # position: sticky alone doesn't guarantee the header paints above
    # tbody rows once they scroll to the same screen position -- without
    # an explicit z-index, later-in-DOM rows win, and the header looks
    # like the list scrolls right over it instead of staying on top.
    client = _synced_client(tmp_path)

    response = client.get("/")

    th_rule_start = response.text.index("th {")
    th_rule = response.text[th_rule_start : th_rule_start + 200]
    assert "position: sticky" in th_rule
    assert "z-index:" in th_rule


def test_items_page_sticky_header_z_index_does_not_outrank_open_dropdowns(tmp_path: Path) -> None:
    # Regression: the sticky header's own z-index was set to the same
    # value (10) as the sidebar filter's open .checklist popup -- on a
    # tie, later-in-DOM wins, and the header (which comes after the
    # filter form in the page) painted right over an open filter dropdown
    # that happened to scroll to the same screen position, effectively
    # blocking it. The header only needs to beat plain scrolled rows
    # (stacking level 0); it must still lose to any real popup.
    client = _synced_client(tmp_path)

    response = client.get("/")

    th_rule = response.text[response.text.index("th {") :]
    th_z_index = int(re.search(r"z-index:\s*(\d+)", th_rule).group(1))
    checklist_rule = response.text[response.text.index(".checklist {") :]
    checklist_z_index = int(re.search(r"z-index:\s*(\d+)", checklist_rule).group(1))
    icon_menu_rule = response.text[response.text.index(".icon-select-menu {") :]
    icon_menu_z_index = int(re.search(r"z-index:\s*(\d+)", icon_menu_rule).group(1))
    assert th_z_index < checklist_z_index
    assert th_z_index < icon_menu_z_index


def test_items_page_fix_version_column_is_wide_enough_to_read(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    col_widths = re.findall(r'<col style="width: (\d+)px;">', response.text)
    assert col_widths, "expected explicit column widths"
    assert int(col_widths[-1]) >= 150


def test_items_page_assignee_shows_an_avatar_circle(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assignee_start = response.text.index('name="field" value="assignee"')
    assignee_widget = response.text[assignee_start : assignee_start + 1200]
    assert 'class="pill-select"' in assignee_widget
    # SAT-1 is assigned to "Serge Colle" -- initials "SC" on a colored
    # circle, not plain name text with no icon.
    assert 'class="avatar-circle"' in assignee_widget
    assert ">SC<" in assignee_widget
    trigger_start = assignee_widget.index('class="pill-select-trigger"')
    trigger_end = assignee_widget.index("</div>", trigger_start)
    trigger_html = assignee_widget[trigger_start:trigger_end]
    # The trigger is avatar-only -- no visible "Serge Colle" name text,
    # which would spill this narrow column into the next one -- but it's
    # still on the pill's own title tooltip (see _pill_html compact=True).
    assert 'title="Serge Colle"' in trigger_html
    assert "<span>Serge Colle</span>" not in trigger_html
    options_html = _icon_select_template_content(response.text, assignee_widget[: trigger_end + len("</div>")])
    assert ">Serge Colle<" in options_html


def test_items_page_assignee_shows_a_real_photo_when_jira_has_one(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    issue_path = next((tmp_path / "components").glob("*/SAT-1/issue.json"))
    issue = read_json(issue_path)
    issue["fields"]["assignee"] = {
        "displayName": "Serge Colle",
        "avatarUrls": {"32x32": "https://example.com/serge-32.png"},
    }
    write_json(issue_path, issue)
    build_manifest(tmp_path)

    response = client.get("/")

    # SAT-2 shares the same assignee name but has no avatarUrls of its own
    # -- scope the search to SAT-1's own row so a match there can't be
    # mistaken for SAT-2's initials-fallback widget.
    row_start = response.text.index(">SAT-1<")
    row_end = response.text.index("</tr>", row_start)
    assignee_start = response.text.index('name="field" value="assignee"', row_start, row_end)
    assignee_widget = response.text[assignee_start : assignee_start + 1200]
    trigger_start = assignee_widget.index('class="pill-select-trigger"')
    trigger_end = assignee_widget.index("</div>", trigger_start)
    # The photo is only known for the CURRENT assignee (from the manifest) --
    # shown on the trigger -- while the option row for that same name (and
    # any other candidate) still falls back to plain initials.
    assert '<img class="avatar-circle" src="https://example.com/serge-32.png"' in assignee_widget[trigger_start:trigger_end]


def test_items_page_unassigned_shows_a_grey_person_circle(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    write_json(
        tmp_path / "components/other/SAT-3/issue.json",
        {"key": "SAT-3", "fields": {"summary": "Unassigned one", "issuetype": {"name": "Task"}, "status": {"name": "Open"}}},
    )
    build_manifest(tmp_path)

    response = client.get("/")

    row_start = response.text.index(">SAT-3<")
    section = response.text[row_start : response.text.index("</tr>", row_start)]
    assert 'class="avatar-circle avatar-unassigned"' in section


def test_items_page_blanks_out_unassigned_component_sentinel(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    # SAT-2 has no component -- the raw "_unassigned" on-disk sentinel is a
    # legitimate filter *value* (still present as a checkbox's value=...
    # attribute, matching the TUI's own Filters screen), but must never
    # leak as visible text: not in the table body, and shown as
    # "(unassigned)" rather than raw in the filter panel's checkbox label.
    tbody = response.text[response.text.index("<tbody>") : response.text.index("</tbody>")]
    assert "_unassigned" not in tbody
    assert '<input type="checkbox" name="component" value="_unassigned">' in response.text
    assert "> (unassigned)<span" in response.text


def test_items_page_with_no_query_at_all_uses_config_view_defaults(tmp_path: Path) -> None:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),),
        view_component=("API Team",),
    )
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/")

    assert "SAT-1" in response.text
    assert "SAT-2" not in response.text


def test_items_page_explicit_query_overrides_config_view_defaults(tmp_path: Path) -> None:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),),
        view_component=("API Team",),
    )
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/", params={"component": ""})

    # An explicit (even empty) query param means "the form was submitted" --
    # config's saved default must not silently reassert itself.
    assert "SAT-1" in response.text
    assert "SAT-2" in response.text


def test_save_filter_persists_to_config_and_redirects(tmp_path: Path) -> None:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )
    config_path = tmp_path / "config.toml"
    client = TestClient(create_app(tmp_path, WorkbenchConfig(), config_path))

    response = client.post(
        "/save-filter",
        data={"component": "API Team", "pattern": "", "active": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "component=API+Team" in response.headers["location"] or "component=API%20Team" in response.headers["location"]
    from jira_workbench.config import load_config

    saved = load_config(config_path)
    assert saved.view_component == ("API Team",)
    assert saved.view_active is True


def test_save_filter_without_config_path_returns_400(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig(), None))

    response = client.post("/save-filter", data={"component": "API Team"})

    assert response.status_code == 400


def test_items_page_priority_column_has_pr_header(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert "<th><a" in response.text
    assert ">Pr<" in response.text or ">Pr ▲<" in response.text


def test_items_page_shows_unset_priority_as_a_distinct_dash_not_blank(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {"summary": "No priority here", "issuetype": {"name": "Task"}, "status": {"name": "Open"}},
        },
    )
    build_manifest(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/")

    # SAT isn't registered read-only, so this renders as the pill-select
    # widget -- an unset value shows a plain "(none)" placeholder pill
    # (see _pill_select_div_html), not the old bespoke muted-dash icon
    # each field previously had to render for itself.
    priority_start = response.text.index('name="field" value="priority"')
    priority_widget = response.text[priority_start : priority_start + 900]
    assert '<span class="pill-select-empty">(none)</span>' in priority_widget


def test_items_page_sorts_by_priority_when_column_header_clicked(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Low one",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "priority": {"name": "Low"},
            },
        },
    )
    write_json(
        tmp_path / "components/compA/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "Highest one",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "priority": {"name": "Highest"},
            },
        },
    )
    build_manifest(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params={"sort": "priority", "dir": "asc"})

    assert response.text.index("SAT-2") < response.text.index("SAT-1")  # Highest sorts before Low


def _write_board_cache(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {
            "project": "SAT",
            "boards": [
                {
                    "id": 1,
                    "name": "SAT board",
                    "predicate": {"op": "eq", "field": "component", "value": "helm-chart"},
                }
            ],
        },
    )
    from jira_workbench.db import recompute_board_membership

    recompute_board_membership(tmp_path)


def test_items_page_modified_board_appears_first_in_the_board_dropdown(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    board_select_start = aside.index('id="sidebar-board"')
    board_select = aside[board_select_start : aside.index("</select>", board_select_start)]
    assert '>Modified Items</option>' in board_select


def test_items_page_modified_board_shows_only_locally_modified_items(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.get("/", params={"board": "Modified Items"})

    assert "SAT-1" in response.text
    assert "SAT-2" not in response.text


def test_items_page_modified_board_is_always_a_flat_list(tmp_path: Path) -> None:
    # No Active/Backlog split and no swimlane grouping for this board, even
    # if a swimlane/board_scope happens to still be set from before.
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.get(
        "/", params={"board": "Modified Items", "swimlane": "component", "board_scope": "active"}
    )

    assert 'class="swimlane-header"' not in response.text
    assert 'class="swimlane-header section-header"' not in response.text


def test_items_page_modified_board_hides_scope_and_swimlane_selects(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/", params={"board": "Modified Items"})

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert 'id="sidebar-board-scope"' not in aside
    assert 'id="sidebar-swimlane"' not in aside


def test_items_page_shows_a_badge_only_on_modified_rows(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)
    set_field(tmp_path, "SAT-1", "summary", "Edited")

    response = client.get("/")

    sat1_start = response.text.index(">SAT-1<")
    sat2_start = response.text.index(">SAT-2<")
    assert "modified-badge" in response.text[sat1_start : sat1_start + 200]
    assert "modified-badge" not in response.text[sat2_start : sat2_start + 200]


def test_items_page_push_button_shown_only_when_something_is_modified(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    clean_response = client.get("/")
    assert 'href="/items/push-review' not in clean_response.text

    set_field(tmp_path, "SAT-1", "summary", "Edited")
    modified_response = client.get("/")
    assert 'href="/items/push-review?' in modified_response.text
    assert "Review &amp; push 1 modified" in modified_response.text


def test_items_page_filters_by_board(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "On board",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    write_json(
        tmp_path / "components/other/SAT-2/issue.json",
        {"key": "SAT-2", "fields": {"summary": "Off board", "issuetype": {"name": "Task"}, "status": {"name": "Open"}}},
    )
    build_manifest(tmp_path)
    _write_board_cache(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params={"board": "SAT board"})

    assert "SAT-1" in response.text
    assert "SAT-2" not in response.text


def _write_sections_fixture(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Backlog item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "components": [{"name": "helm-chart"}],
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
                "status": {"name": "Open"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    build_manifest(tmp_path)
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {
            "project": "SAT",
            "boards": [
                {
                    "id": 1,
                    "name": "SAT board",
                    "predicate": {"op": "eq", "field": "component", "value": "helm-chart"},
                    "backlogKeys": ["SAT-1"],
                    "boardKeys": ["SAT-2"],
                }
            ],
        },
    )
    from jira_workbench.db import recompute_board_membership

    recompute_board_membership(tmp_path)


def test_items_page_sections_mode_splits_active_and_backlog(tmp_path: Path) -> None:
    _write_sections_fixture(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params={"board": "SAT board"})

    assert response.text.count('class="swimlane-header section-header"') == 2
    assert 'data-scope="active"' in response.text
    assert 'data-scope="backlog"' in response.text
    # Active section is listed first, matching Jira's own backlog view
    # layout (board/sprint section above, backlog below).
    active_header = response.text.index(">Active ")
    backlog_header = response.text.index(">Backlog ")
    sat1 = response.text.index("SAT-1")
    sat2 = response.text.index("SAT-2")
    assert active_header < backlog_header
    assert active_header < sat2 < backlog_header
    assert backlog_header < sat1


def test_items_page_sections_mode_rows_are_draggable(tmp_path: Path) -> None:
    _write_sections_fixture(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params={"board": "SAT board"})

    assert 'draggable="true"' in response.text
    assert 'data-key="SAT-1"' in response.text
    assert 'data-key="SAT-2"' in response.text
    assert "function jiraWbDrop" in response.text
    assert "function jiraWbToggleSection" in response.text


def test_items_page_sections_mode_suppressed_by_explicit_scope(tmp_path: Path) -> None:
    _write_sections_fixture(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params={"board": "SAT board", "board_scope": "active"})

    assert 'class="swimlane-header section-header"' not in response.text
    assert "SAT-2" in response.text
    assert "SAT-1" not in response.text


def test_items_page_sections_mode_suppressed_by_swimlane(tmp_path: Path) -> None:
    _write_sections_fixture(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params={"board": "SAT board", "swimlane": "component"})

    assert 'class="swimlane-header section-header"' not in response.text
    assert "jiraWbDrop" not in response.text


def test_items_page_sections_have_a_scoped_plus_each(tmp_path: Path) -> None:
    _write_sections_fixture(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params={"board": "SAT board"})

    assert "target_scope=active" in response.text
    assert "target_scope=backlog" in response.text
    assert response.text.count('class="new-issue-fab-small"') == 2
    # The generic bottom "+" FAB is suppressed in sections_mode -- the two
    # per-section "+" buttons already cover creation unambiguously.
    assert 'class="new-issue-fab"' not in response.text


def test_items_page_bottom_fab_shown_outside_sections_mode(tmp_path: Path) -> None:
    _write_sections_fixture(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/")

    assert 'class="new-issue-fab"' in response.text


def test_items_page_board_dropdown_lists_active_boards(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/boards.json",
        {"localBoards": [], "disabledBoardIds": []},
    )
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {"project": "SAT", "boards": [{"id": "1", "name": "SAT board", "supported": True}]},
    )
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert '<option value="SAT board">SAT board</option>' in response.text


def test_save_filter_persists_board_and_board_scope(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    client = TestClient(create_app(tmp_path, WorkbenchConfig(), config_path))

    response = client.post(
        "/save-filter",
        data={"board": "SAT board", "board_scope": "active"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    from jira_workbench.config import load_config

    saved = load_config(config_path)
    assert saved.view_board == "SAT board"
    assert saved.view_board_scope == "active"


def test_items_page_with_no_query_at_all_uses_config_board_default(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "On board",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "components": [{"name": "helm-chart"}],
            },
        },
    )
    write_json(
        tmp_path / "components/other/SAT-2/issue.json",
        {"key": "SAT-2", "fields": {"summary": "Off board", "issuetype": {"name": "Task"}, "status": {"name": "Open"}}},
    )
    build_manifest(tmp_path)
    _write_board_cache(tmp_path)
    config = WorkbenchConfig(view_board="SAT board")
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/")

    assert "SAT-1" in response.text
    assert "SAT-2" not in response.text


def test_items_page_filter_form_does_not_mention_tui(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert "TUI" not in response.text


def test_nav_links_live_in_the_sidebar_not_a_separate_top_bar(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert "<nav" not in response.text.split("<aside", 1)[0]  # no standalone top nav before the sidebar
    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert '<nav class="sidebar-nav">' in aside
    assert '<a href="/" class="active">Items</a>' in aside
    assert '<a href="/meta">Meta</a>' in aside


def test_meta_page_nav_highlights_meta_as_active(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta")

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert '<a href="/meta" class="active">Meta</a>' in aside
    assert '<a href="/" >Items</a>' in aside or '<a href="/">Items</a>' in aside


def test_meta_page_project_form_is_not_a_nested_form(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta")

    assert response.text.count("<form") == 1


def test_items_page_matches_any_of_several_checked_statuses(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {"key": "SAT-1", "fields": {"summary": "One", "issuetype": {"name": "Task"}, "status": {"name": "Open"}}},
    )
    write_json(
        tmp_path / "components/compA/SAT-2/issue.json",
        {"key": "SAT-2", "fields": {"summary": "Two", "issuetype": {"name": "Task"}, "status": {"name": "Closed"}}},
    )
    write_json(
        tmp_path / "components/compA/SAT-3/issue.json",
        {"key": "SAT-3", "fields": {"summary": "Three", "issuetype": {"name": "Task"}, "status": {"name": "Blocked"}}},
    )
    build_manifest(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params=[("status", "Open"), ("status", "Closed"), ("active", "0")])

    assert "SAT-1" in response.text
    assert "SAT-2" in response.text
    assert "SAT-3" not in response.text


def test_items_page_explicit_empty_field_does_not_zero_out_results(tmp_path: Path) -> None:
    # Regression: an explicit but empty query value (e.g. a form submitted
    # with a field left blank) used to arrive as a one-element list
    # containing "", which then matched nothing (no item's component is
    # literally "" -- unset ones are "_unassigned") instead of meaning "no
    # filter on this field".
    client = _synced_client(tmp_path)

    response = client.get("/", params={"component": ""})

    assert "SAT-1" in response.text
    assert "SAT-2" in response.text


def test_items_page_filter_form_has_save_and_reset_actions(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert f'<form method="get" id="{ITEMS_FILTER_FORM_ID}"' in response.text
    assert 'title="Reload the list using the project/status/component/etc. selections above">Apply filters</button>' in (
        response.text
    )
    assert '<button type="submit" formmethod="post" formaction="/save-filter"' in response.text
    assert '<a href="/" class="button-link"' in response.text
    assert ">Reset to saved default filter</a>" in response.text


def test_items_page_priority_icon_uses_jira_style_red_chevrons_for_highest(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Urgent",
                "issuetype": {"name": "Bug"},
                "status": {"name": "Open"},
                "priority": {"name": "Highest"},
            },
        },
    )
    build_manifest(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/")

    # SAT isn't registered read-only, so Type/Priority render as the
    # icon-select widget (no title attribute -- the trigger already shows
    # the name as visible text) rather than the read-only icon-cell.
    assert response.text.count('style="color: #EF4444"') >= 2  # Bug (type) and Highest (priority) share this color
    priority_start = response.text.index('name="field" value="priority"')
    assert "Highest" in response.text[priority_start : priority_start + 800]
    type_start = response.text.index('name="field" value="type"')
    assert "Bug" in response.text[type_start : type_start + 800]


def test_items_page_board_and_scope_controls_live_in_the_sidebar(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    sidebar = response.text[response.text.index('<aside class="sidebar">') : response.text.index("</aside>")]
    assert 'name="board"' in sidebar
    assert 'name="board_scope"' in sidebar
    # ...and nowhere else -- moved out of the filter chip row entirely.
    rest = response.text.replace(sidebar, "")
    assert 'name="board"' not in rest
    assert 'name="board_scope"' not in rest


def test_items_page_sidebar_collapses_to_a_thin_strip_via_css_checkbox_hack(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    # The whole sidebar collapses (not just the Board/Scope sub-section) via
    # a CSS-only checkbox+label toggle -- no JavaScript. The checkbox must
    # be a sibling of .layout, not nested inside it, for the ~ selector to
    # reach .sidebar -- checked here against the real <body> markup only
    # (not the <style> block, which mentions "sidebar-collapsed" earlier in
    # the document via its own CSS selector and would otherwise make an
    # index()-based ordering check pass regardless of the actual DOM order).
    body_html = response.text[response.text.index("<body>") :]
    checkbox_tag = '<input type="checkbox" id="sidebar-collapsed">'
    layout_tag = '<div class="layout">'
    assert checkbox_tag in body_html
    assert layout_tag in body_html
    assert body_html.index(checkbox_tag) < body_html.index(layout_tag)
    assert '<label for="sidebar-collapsed" class="sidebar-toggle"' in response.text
    assert "#sidebar-collapsed:checked ~ .layout .sidebar" in response.text


def test_items_page_sidebar_is_css_resizable(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert "resize: horizontal" in response.text


def test_items_page_sidebar_selects_submit_the_filter_form_via_form_attribute(tmp_path: Path) -> None:
    # The sidebar (<aside>) is no longer physically nested inside the
    # filter <form> -- per-row icon-select widgets in the table needed
    # their own independent POST forms, which forced the page off the old
    # single-shared-form design (see page()'s form_wrapped). The sidebar's
    # Board/Scope selects instead use HTML5's form="..." attribute to
    # still submit as part of the filter form despite living outside it.
    client = _synced_client(tmp_path)

    response = client.get("/")

    aside = response.text[response.text.index("<aside") : response.text.index("</aside>")]
    assert f'form="{ITEMS_FILTER_FORM_ID}"' in aside
    assert f'<form method="get" id="{ITEMS_FILTER_FORM_ID}"' in response.text
    # And the sidebar itself is genuinely outside that form's own tags.
    form_start = response.text.index(f'<form method="get" id="{ITEMS_FILTER_FORM_ID}"')
    form_end = response.text.index("</form>", form_start)
    aside_start = response.text.index("<aside")
    assert not (form_start < aside_start < form_end)


def test_items_page_count_shows_filtered_out_of_the_true_unfiltered_total(tmp_path: Path) -> None:
    # Regression: "total" used to be handed the *filtered* count a second
    # time, so the summary line always read "N of N" no matter how
    # narrowing the filter was -- it should read filtered-count of the
    # real, unfiltered total synced locally.
    client = _synced_client(tmp_path)

    response = client.get("/", params={"component": "API Team"})

    assert "1 of 2 synced items match the current filter." in response.text


def test_items_page_active_only_checkbox_has_an_explanatory_tooltip(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert "title=\"Hide issues whose status is Done/Closed (anything Jira categorizes as a 'done' status)\"" in (
        response.text
    )


def test_items_page_board_and_scope_selects_submit_on_change(tmp_path: Path) -> None:
    # Unlike the checkbox filter fields (where you usually tick several
    # values before applying), a single-value dropdown should take effect
    # immediately -- otherwise changing it silently does nothing until you
    # separately click "Apply filters", which reads as broken.
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert (
        f'<select name="board" id="sidebar-board" form="{ITEMS_FILTER_FORM_ID}" onchange="this.form.submit()">'
        in response.text
    )
    assert (
        f'<select name="board_scope" id="sidebar-board-scope" form="{ITEMS_FILTER_FORM_ID}" onchange="this.form.submit()">'
        in response.text
    )


def test_items_page_field_clear_link_only_shows_when_that_field_has_a_selection(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    unfiltered = client.get("/")
    filtered = client.get("/", params={"component": "API Team"})

    assert "Clear component" not in unfiltered.text
    assert "Clear component" in filtered.text
    # Every other (unselected) field's clear link is still absent.
    assert "Clear project" not in filtered.text
    assert "Clear status" not in filtered.text


def test_items_page_clear_field_link_preserves_other_selected_filters(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/", params={"component": "API Team", "pattern": "SAT", "sort": "priority", "dir": "desc"})

    clear_component_href = re.search(r'<a href="([^"]*)" class="clear-field-link">Clear component</a>', response.text)
    assert clear_component_href is not None
    href = html.unescape(clear_component_href.group(1))
    assert "component=" not in href
    assert "pattern=SAT" in href
    assert "sort=priority" in href
    assert "dir=desc" in href


def test_items_page_clear_all_filters_drops_everything_but_keeps_sort(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get(
        "/", params={"component": "API Team", "pattern": "SAT", "active": "1", "sort": "priority", "dir": "desc"}
    )

    clear_all_href = re.search(r'<a href="([^"]*)" class="button-link" title="Remove every filter[^"]*">', response.text)
    assert clear_all_href is not None
    href = html.unescape(clear_all_href.group(1))
    assert "component=" not in href
    assert "pattern=" not in href
    assert "active=" not in href
    assert "sort=priority" in href
    assert "dir=desc" in href


def _fix_version_checklist(response_text: str) -> str:
    # Anchored on the checklist's own <summary> tag specifically -- a bare
    # substring search for "Fix version" can also match the sidebar's
    # Swimlane <select> (one of its options is literally "Version") or a
    # sort-header link, both of which appear earlier in the document.
    start = response_text.index("<summary>Fix version")
    return response_text[start : response_text.index("</details>", start)]


def _field_checklist(response_text: str, field_label: str) -> str:
    # Anchored on <summary>, not a bare ">{label}<" search -- the sidebar's
    # Swimlane <select> has options literally named "Component"/"Board",
    # which appear earlier in the document than the real filter checklist.
    start = response_text.index(f"<summary>{field_label}")
    return response_text[start : response_text.index("</details>", start)]


def _write_versions_cache(tmp_path: Path, project: str, versions: list[dict]) -> None:
    write_json(tmp_path / f"meta/{project}/versions.json", {"project": project, "versions": versions})


def test_items_page_fix_version_options_scoped_to_selected_project(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "SAT one",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "sat-1.0"}],
            },
        },
    )
    write_json(
        tmp_path / "components/compB/PLAT-1/issue.json",
        {
            "key": "PLAT-1",
            "fields": {
                "summary": "PLAT one",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "plat-2.0"}],
            },
        },
    )
    build_manifest(tmp_path)
    _write_versions_cache(tmp_path, "SAT", [{"id": "1", "name": "sat-1.0", "released": False, "archived": False}])
    _write_versions_cache(tmp_path, "PLAT", [{"id": "2", "name": "plat-2.0", "released": False, "archived": False}])
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    unscoped = _fix_version_checklist(client.get("/").text)
    assert "sat-1.0" in unscoped
    assert "plat-2.0" in unscoped

    scoped = _fix_version_checklist(client.get("/", params={"project": "SAT"}).text)
    assert "sat-1.0" in scoped
    assert "plat-2.0" not in scoped


def test_items_page_fix_version_edit_widget_is_a_clickable_dropdown(tmp_path: Path) -> None:
    # Regression: the edit widget used to be a free-text input with an
    # autocomplete <datalist> -- its transparent border made it look like
    # plain static text rather than something interactive, and a value
    # only ever surfaced if you already knew to type it. A real dropdown
    # lists every known version (including ones with zero issues so far --
    # fix_version_groups_for already sourced those correctly) as a
    # browsable, clickable option -- no checkbox (misleading: it suggested
    # you had to hit that exact target rather than the whole row), just a
    # checkmark on whichever option is currently selected.
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "SAT one",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "kube 1.3.0"}],
            },
        },
    )
    build_manifest(tmp_path)
    _write_versions_cache(
        tmp_path,
        "SAT",
        [
            {"id": "1", "name": "kube 1.3.0", "released": False, "archived": False},
            {"id": "2", "name": "kube 1.4.0", "released": False, "archived": False},
        ],
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/")

    widget_start = response.text.index('name="field" value="fixVersions"')
    widget = response.text[widget_start : widget_start + 1000]
    # The option rows themselves are deduped into a shared <template>
    # (see ensure_pill_template) -- never "selected" there, since the same
    # template is cloned into every row regardless of that row's own
    # values; jiraWbPillPopulate applies the .selected class (a checkmark,
    # see CSS) at open time from the widget's own data-current attribute
    # instead (see _icon_select_current_values).
    options_html = _icon_select_template_content(response.text, widget)
    assert 'data-value="kube 1.3.0"' in options_html
    assert 'data-value="kube 1.4.0"' in options_html
    assert 'onclick="jiraWbPillMultiToggle(this)"' in options_html
    assert "checkbox" not in options_html
    assert "selected" not in options_html
    assert "datalist" not in widget
    assert _icon_select_current_values(widget) == ["kube 1.3.0"]
    # version_options() prepends "(none)" as a real, selectable value for
    # the single-select widgets it was originally built for -- here,
    # leaving every real version unselected already means "no fix version",
    # so a literal "(none)" option would be redundant (or contradictory if
    # selected alongside a real one).
    assert 'data-value="(none)"' not in options_html


def test_icon_select_trigger_shows_a_dropdown_caret_like_a_native_select(tmp_path: Path) -> None:
    # Regression: the custom icon-select widget (Type/Priority/Assignee/
    # Fix Version) had no visual indicator that it opens a dropdown at
    # all, unlike Status/Component's native <select>, which always shows
    # its own arrow. The compact Items-list variant (Type/Priority/
    # Assignee) deliberately keeps it off -- there's no room, and it
    # would read as clutter on an icon-only trigger.
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert ".icon-select-trigger::after" in response.text
    assert ".icon-select-cell .icon-select-trigger::after { display: none; }" in response.text


def test_items_page_fix_version_dropdown_is_not_clipped_by_the_row(tmp_path: Path) -> None:
    # Regression: Fix Version's checkbox dropdown lives in a
    # .pill-select-row-cell (it needs the wider, roomier trigger those get,
    # not the compact .pill-select-cell one Type/Priority/Assignee use) --
    # but the blanket "clip a td's overflow" rule has a `:has(.pill-select-menu)`
    # escape hatch precisely so any open dropdown, in either cell kind,
    # isn't clipped at the row's edge, looking like it vanished behind the
    # row below.
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert ":not(:has(.pill-select-menu)) { overflow: hidden; }" in response.text


def test_item_detail_page_fix_version_edit_widget_keeps_every_current_value_checked(tmp_path: Path) -> None:
    # Regression: the Detail page's own fixVersions widget used to be a
    # single <select> -- editable_field_value comma-joins ALL of an
    # issue's fix versions into one string, which never matches any single
    # <option>, so a genuinely multi-valued issue showed nothing selected
    # and the next save would have silently collapsed it down to one.
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "SAT one",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "kube 1.3.0"}, {"name": "kube 1.4.0"}],
            },
        },
    )
    build_manifest(tmp_path)
    _write_versions_cache(
        tmp_path,
        "SAT",
        [
            {"id": "1", "name": "kube 1.3.0", "released": False, "archived": False},
            {"id": "2", "name": "kube 1.4.0", "released": False, "archived": False},
        ],
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/items/SAT-1")

    widget_start = response.text.index('name="field" value="fixVersions"')
    widget = response.text[widget_start : widget_start + 1000]
    assert 'value="kube 1.3.0" checked' in widget
    assert 'value="kube 1.4.0" checked' in widget


def test_items_page_fix_version_hides_released_and_archived_by_default(tmp_path: Path) -> None:
    # Each version under test needs its own item -- the checklist only
    # ever lists versions actually assigned to a currently-synced item, so
    # a version with no item behind it is vacuously "hidden" either way.
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "unreleased item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "sat-unreleased"}],
            },
        },
    )
    write_json(
        tmp_path / "components/compA/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "released item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "sat-released"}],
            },
        },
    )
    write_json(
        tmp_path / "components/compA/SAT-3/issue.json",
        {
            "key": "SAT-3",
            "fields": {
                "summary": "archived item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "sat-archived"}],
            },
        },
    )
    build_manifest(tmp_path)
    _write_versions_cache(
        tmp_path,
        "SAT",
        [
            {"id": "1", "name": "sat-unreleased", "released": False, "archived": False},
            {"id": "2", "name": "sat-released", "released": True, "archived": False},
            {"id": "3", "name": "sat-archived", "released": False, "archived": True},
        ],
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    checklist = _fix_version_checklist(client.get("/", params={"project": "SAT"}).text)

    assert "sat-unreleased" in checklist
    assert "sat-released" not in checklist
    assert "sat-archived" not in checklist


def test_items_page_show_released_toggle_reveals_released_but_never_archived(tmp_path: Path) -> None:
    # The checklist only ever lists versions actually assigned to a
    # currently-synced item (it's a filter over what's here, not a full
    # catalog picker) -- so each version under test needs its own item.
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "unreleased item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "sat-unreleased"}],
            },
        },
    )
    write_json(
        tmp_path / "components/compA/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {
                "summary": "released item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "sat-released"}],
            },
        },
    )
    write_json(
        tmp_path / "components/compA/SAT-3/issue.json",
        {
            "key": "SAT-3",
            "fields": {
                "summary": "archived item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "sat-archived"}],
            },
        },
    )
    build_manifest(tmp_path)
    _write_versions_cache(
        tmp_path,
        "SAT",
        [
            {"id": "1", "name": "sat-unreleased", "released": False, "archived": False},
            {"id": "2", "name": "sat-released", "released": True, "archived": False},
            {"id": "3", "name": "sat-archived", "released": False, "archived": True},
        ],
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    checklist = _fix_version_checklist(client.get("/", params={"project": "SAT", "show_released": "1"}).text)

    assert "sat-unreleased" in checklist
    assert "sat-released" in checklist
    assert "sat-archived" not in checklist


def test_items_page_show_released_toggle_submits_on_change(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert 'name="show_released" value="1"  onchange="this.form.submit()"' in response.text


def test_items_page_currently_selected_version_stays_visible_even_if_hidden_by_default(tmp_path: Path) -> None:
    # Toggling "show released" off must never silently un-submit a
    # released version you'd already picked -- it should stay checked and
    # visible until you explicitly uncheck/clear it.
    write_json(
        tmp_path / "components/compA/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "released item",
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "fixVersions": [{"name": "sat-released"}],
            },
        },
    )
    build_manifest(tmp_path)
    _write_versions_cache(tmp_path, "SAT", [{"id": "1", "name": "sat-released", "released": True, "archived": False}])
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    checklist = _fix_version_checklist(
        client.get("/", params={"project": "SAT", "fixVersion": "sat-released"}).text
    )

    assert 'value="sat-released" checked' in checklist


def _fix_version_details_tag(response_text: str) -> str:
    """The Fix version field's own opening <details ...> tag, e.g.
    '<details class="filter-field">' or '...filter-field" open>'."""
    start = response_text.rindex("<details", 0, response_text.index("<summary>Fix version"))
    return response_text[start : response_text.index(">", start) + 1]


def test_items_page_fix_version_panel_stays_open_when_show_released_is_on(tmp_path: Path) -> None:
    # Regression: clicking "show released" resubmits the whole form (a real
    # page reload, no JS state survives that) -- without force_open, the
    # freshly-rendered <details> has no open attribute and snaps shut on
    # the very reload the click just triggered, even though the checkbox
    # itself did toggle correctly.
    client = _synced_client(tmp_path)

    closed_tag = _fix_version_details_tag(client.get("/", params={"show_released": "0"}).text)
    open_tag = _fix_version_details_tag(client.get("/", params={"show_released": "1"}).text)

    assert " open" not in closed_tag
    assert " open" in open_tag


def test_items_page_filter_panels_stay_closed_even_with_a_selection(tmp_path: Path) -> None:
    # Having a value already selected used to auto-open every matching
    # filter's panel on every page load -- annoying once several filters
    # are set, since they'd all pop open at once. Only the badge count
    # should show; the panel itself stays collapsed until clicked, except
    # for the narrow force_open cases (see the show_released test above).
    client = _synced_client(tmp_path)

    response = client.get("/", params={"fixVersion": "helm-chart-sa 3.4.4"})

    assert " open" not in _fix_version_details_tag(response.text)
    assert '<span class="badge">1</span>' in response.text


def test_items_page_status_component_assignee_options_scoped_to_selected_project(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "SAT one",
                "issuetype": {"name": "Task"},
                "status": {"name": "SAT-only-status"},
                "components": [{"name": "SAT-only-component"}],
                "assignee": {"displayName": "SAT Only Assignee"},
            },
        },
    )
    write_json(
        tmp_path / "components/other/PLAT-1/issue.json",
        {
            "key": "PLAT-1",
            "fields": {
                "summary": "PLAT one",
                "issuetype": {"name": "Task"},
                "status": {"name": "PLAT-only-status"},
                "components": [{"name": "PLAT-only-component"}],
                "assignee": {"displayName": "PLAT Only Assignee"},
            },
        },
    )
    build_manifest(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    unscoped = client.get("/").text
    for field, sat_value, plat_value in (
        ("Status", "SAT-only-status", "PLAT-only-status"),
        ("Component", "SAT-only-component", "PLAT-only-component"),
        ("Assignee", "SAT Only Assignee", "PLAT Only Assignee"),
    ):
        checklist = _field_checklist(unscoped, field)
        assert sat_value in checklist
        assert plat_value in checklist

    scoped = client.get("/", params={"project": "SAT"}).text
    for field, sat_value, plat_value in (
        ("Status", "SAT-only-status", "PLAT-only-status"),
        ("Component", "SAT-only-component", "PLAT-only-component"),
        ("Assignee", "SAT Only Assignee", "PLAT Only Assignee"),
    ):
        checklist = _field_checklist(scoped, field)
        assert sat_value in checklist
        assert plat_value not in checklist


def test_items_page_status_component_assignee_options_scoped_to_selected_board(tmp_path: Path) -> None:
    # A selected board narrows these dropdowns the same way an explicit
    # Project checkbox does -- even with no Project checkbox ticked at
    # all, which is the normal state once you've picked a board (see
    # items_page's per-board "clean slate": switching to a never-before-
    # seen board resets the Project checkboxes to empty, and that empty
    # selection then gets remembered forever in the per-board cookie).
    # Regression: this used to fall back to every synced project's values
    # whenever Project itself was unset, regardless of which board was
    # selected.
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "SAT one",
                "issuetype": {"name": "Task"},
                "status": {"name": "SAT-only-status"},
                "components": [{"name": "SAT-only-component"}],
                "assignee": {"displayName": "SAT Only Assignee"},
            },
        },
    )
    write_json(
        tmp_path / "components/other/PLAT-1/issue.json",
        {
            "key": "PLAT-1",
            "fields": {
                "summary": "PLAT one",
                "issuetype": {"name": "Task"},
                "status": {"name": "PLAT-only-status"},
                "components": [{"name": "PLAT-only-component"}],
                "assignee": {"displayName": "PLAT Only Assignee"},
            },
        },
    )
    build_manifest(tmp_path)
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {
            "project": "SAT",
            "boards": [{"id": 1, "name": "SAT board", "predicate": {"op": "eq", "field": "project", "value": "SAT"}}],
        },
    )
    from jira_workbench.db import recompute_board_membership

    recompute_board_membership(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/", params={"board": "SAT board"})

    for field, sat_value, plat_value in (
        ("Status", "SAT-only-status", "PLAT-only-status"),
        ("Component", "SAT-only-component", "PLAT-only-component"),
        ("Assignee", "SAT Only Assignee", "PLAT Only Assignee"),
    ):
        checklist = _field_checklist(response.text, field)
        assert sat_value in checklist
        assert plat_value not in checklist


def _write_two_assignee_issues(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {"summary": "One", "issuetype": {"name": "Task"}, "assignee": {"displayName": "Al Baker"}},
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-2/issue.json",
        {
            "key": "SAT-2",
            "fields": {"summary": "Two", "issuetype": {"name": "Task"}, "assignee": {"displayName": "Serge Colle"}},
        },
    )
    build_manifest(tmp_path)


def test_items_page_assignee_filter_shows_ex_members_too(tmp_path: Path) -> None:
    # The FILTER checklist (narrowing the list to whoever an issue is
    # currently assigned to) must still be able to find issues assigned to
    # someone who's since left -- unlike the ASSIGN dropdown below, it
    # keeps using unrestricted local observation.
    _write_two_assignee_issues(tmp_path)
    write_json(tmp_path / "meta/SAT/assignees.json", {"project": "SAT", "assignees": [{"displayName": "Serge Colle"}]})
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/")

    checklist = _field_checklist(response.text, "Assignee")
    assert "Al Baker" in checklist
    assert "Serge Colle" in checklist


def test_items_page_assignee_edit_dropdown_only_offers_active_members(tmp_path: Path) -> None:
    # Al Baker is only in this project's synced ISSUE HISTORY, not in the
    # refreshed assignable-users cache (jira-wb meta refresh --assignees)
    # -- he shouldn't be offered as someone NEW work can be assigned to,
    # even though SAT-1 (still assigned to him) must keep showing him as
    # its own current value.
    _write_two_assignee_issues(tmp_path)
    write_json(tmp_path / "meta/SAT/assignees.json", {"project": "SAT", "assignees": [{"displayName": "Serge Colle"}]})
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/")

    row_start = response.text.index(">SAT-2<")
    row_end = response.text.index("</tr>", row_start)
    sat2_widget_start = response.text.index('name="field" value="assignee"', row_start, row_end)
    sat2_widget = response.text[sat2_widget_start : sat2_widget_start + 1500]
    assert "Serge Colle" in sat2_widget
    assert "Al Baker" not in sat2_widget

    sat1_row_start = response.text.index(">SAT-1<")
    sat1_widget_start = response.text.index('name="field" value="assignee"', sat1_row_start)
    sat1_widget = response.text[sat1_widget_start : sat1_widget_start + 1500]
    assert "Al Baker" in sat1_widget  # still shown as SAT-1's own current value


def test_items_page_assignee_edit_dropdown_falls_back_to_local_observation_without_a_refreshed_cache(
    tmp_path: Path,
) -> None:
    # jira-wb meta refresh --assignees has never been run for this project
    # -- no meta/SAT/assignees.json exists yet -- so the edit dropdown
    # falls back to the old local-observation behavior rather than
    # offering nobody at all.
    _write_two_assignee_issues(tmp_path)
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/")

    row_start = response.text.index(">SAT-2<")
    widget_start = response.text.index('name="field" value="assignee"', row_start)
    widget = response.text[widget_start : widget_start + 1500]
    options_html = _icon_select_template_content(response.text, widget)
    assert "Al Baker" in options_html


def test_item_detail_page_assignee_dropdown_only_offers_active_members(tmp_path: Path) -> None:
    _write_two_assignee_issues(tmp_path)
    write_json(tmp_path / "meta/SAT/assignees.json", {"project": "SAT", "assignees": [{"displayName": "Serge Colle"}]})
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/items/SAT-2")

    widget_start = response.text.index('name="field" value="assignee"')
    widget = response.text[widget_start : widget_start + 1500]
    assert "Serge Colle" in widget
    assert "Al Baker" not in widget


def _write_sat_issue_with_component(tmp_path: Path, key: str, component_value: str) -> None:
    write_json(
        tmp_path / f"components/{component_value}/{key}/issue.json",
        {
            "key": key,
            "fields": {
                "summary": key,
                "issuetype": {"name": "Task"},
                "customfield_10071": {"value": component_value},
            },
        },
    )


def _custom_field_options_config() -> WorkbenchConfig:
    return WorkbenchConfig(projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),))


def test_items_page_component_edit_dropdown_includes_options_never_used_by_any_issue(tmp_path: Path) -> None:
    # Regression: the component EDIT dropdown used to be built purely from
    # local observation (_project_scoped_options), which can only ever
    # list values already used on a synced issue -- a value that's real in
    # Jira (confirmed via jira-wb meta refresh --components, or added
    # straight from Jira's own UI) but not yet assigned to anything was
    # invisible in the dropdown even though it correctly showed up on the
    # Meta > Components page, which reads the same refreshed cache
    # directly.
    _write_sat_issue_with_component(tmp_path, "SAT-1", "helm-chart")
    build_manifest(tmp_path)
    write_json(
        tmp_path / "meta/SAT/customfield_10071-options.json",
        {
            "project": "SAT",
            "field": "customfield_10071",
            "options": [{"id": "1", "value": "helm-chart"}, {"id": "2", "value": "training"}],
        },
    )
    client = TestClient(create_app(tmp_path, _custom_field_options_config()))

    response = client.get("/")

    widget_start = response.text.index('name="field" value="customfield_10071"')
    widget = response.text[widget_start : widget_start + 1000]
    assert "helm-chart" in widget
    # "training" isn't the current value, so it only lives in the shared
    # <template> the widget's own menu is lazily populated from (see
    # _icon_select_template_content), not inline in the trigger's own pill.
    options_html = _icon_select_template_content(response.text, widget)
    assert "training" in options_html


def test_items_page_component_edit_dropdown_falls_back_to_local_observation_without_a_refreshed_cache(
    tmp_path: Path,
) -> None:
    _write_sat_issue_with_component(tmp_path, "SAT-1", "helm-chart")
    build_manifest(tmp_path)
    client = TestClient(create_app(tmp_path, _custom_field_options_config()))

    response = client.get("/")

    widget_start = response.text.index('name="field" value="customfield_10071"')
    widget = response.text[widget_start : widget_start + 1000]
    assert "helm-chart" in widget


def test_item_detail_page_component_edit_dropdown_includes_options_never_used_by_any_issue(tmp_path: Path) -> None:
    _write_sat_issue_with_component(tmp_path, "SAT-1", "helm-chart")
    build_manifest(tmp_path)
    write_json(
        tmp_path / "meta/SAT/customfield_10071-options.json",
        {
            "project": "SAT",
            "field": "customfield_10071",
            "options": [{"id": "1", "value": "helm-chart"}, {"id": "2", "value": "training"}],
        },
    )
    client = TestClient(create_app(tmp_path, _custom_field_options_config()))

    response = client.get("/items/SAT-1")

    widget_start = response.text.index('name="field" value="customfield_10071"')
    widget = response.text[widget_start : widget_start + 1000]
    assert "training" in widget


def test_new_issue_page_component_dropdown_includes_options_never_used_by_any_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_sat_issue_with_component(tmp_path, "SAT-1", "helm-chart")
    build_manifest(tmp_path)
    write_json(
        tmp_path / "meta/SAT/customfield_10071-options.json",
        {
            "project": "SAT",
            "field": "customfield_10071",
            "options": [{"id": "1", "value": "helm-chart"}, {"id": "2", "value": "training"}],
        },
    )
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),),
        jira_url="https://example.atlassian.net",
        jira_email="user@example.com",
        jira_api_token="token",
    )
    client = TestClient(create_app(tmp_path, config))
    create_client = CreateIssueJiraClient({"Task": {"summary": {}, "description": {}, "customfield_10071": {}}})
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: create_client)

    response = client.get("/items/new", params={"project": "SAT", "type": "Task"})

    assert "training" in response.text


def test_items_page_assignee_edit_dropdown_respects_manual_exclude_list_with_a_cache(tmp_path: Path) -> None:
    # Confirmed in practice against a real Jira instance: someone can keep
    # holding the project's Administrator/Member role (and so keep
    # appearing in the refreshed cache) long after they've actually
    # stopped working on the project -- config.toml's exclude_assignees is
    # the manual override for exactly that gap.
    _write_two_assignee_issues(tmp_path)
    write_json(
        tmp_path / "meta/SAT/assignees.json",
        {"project": "SAT", "assignees": [{"displayName": "Serge Colle"}, {"displayName": "Matt Petrillo"}]},
    )
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", exclude_assignees=("Matt Petrillo",)),))
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/")

    row_start = response.text.index(">SAT-2<")
    widget_start = response.text.index('name="field" value="assignee"', row_start)
    widget = response.text[widget_start : widget_start + 1500]
    assert "Serge Colle" in widget
    assert "Matt Petrillo" not in widget


def test_items_page_assignee_edit_dropdown_respects_manual_exclude_list_without_a_cache(tmp_path: Path) -> None:
    # The exclude list must also apply to the local-observation fallback,
    # not just the refreshed-cache path.
    _write_two_assignee_issues(tmp_path)
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", exclude_assignees=("Al Baker",)),))
    client = TestClient(create_app(tmp_path, config))

    response = client.get("/")

    row_start = response.text.index(">SAT-2<")
    widget_start = response.text.index('name="field" value="assignee"', row_start)
    widget = response.text[widget_start : widget_start + 1500]
    assert "Serge Colle" in widget
    assert "Al Baker" not in widget
