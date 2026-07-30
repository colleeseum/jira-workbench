from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi.testclient import TestClient

from jira_workbench.config import ProjectSettings, WorkbenchConfig
from jira_workbench.server import ISSUE_KEY_PATTERN, create_app
from jira_workbench.shadow import set_field
from jira_workbench.sync import SyncConfig, build_manifest, sync_project, write_json
from test_sync import FakeJiraClient


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
            "workItems": [{"key": "SAT-1", "component": "helm-chart", "status": "To Do"}],
        },
    )
    client = TestClient(create_app(tmp_path, WorkbenchConfig()))

    response = client.get("/meta/components", params={"project": "SAT"})

    assert response.status_code == 200
    assert "helm-chart" in response.text


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

    # SAT-1/SAT-2 are both type "Task" and priority "Medium" -- shown as
    # SVG icon spans (title carries the real name), not as plain table text.
    assert 'title="Task">' in response.text
    assert 'title="Medium">' in response.text
    assert "<svg" in response.text
    assert response.text.count("<td>Task</td>") == 0
    assert response.text.count("<td>Medium</td>") == 0


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

    assert 'title="No priority set">—</span>' in response.text


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


def test_items_page_has_one_form_with_filter_save_and_reset_actions(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert response.text.count("<form") == 1
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

    assert 'style="color: #EF4444" title="Highest"' in response.text  # priority chevron
    assert 'style="color: #EF4444" title="Bug"' in response.text  # type icon


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
    form_tag = '<form method="get" class="layout">'
    assert checkbox_tag in body_html
    assert form_tag in body_html
    assert body_html.index(checkbox_tag) < body_html.index(form_tag)
    assert '<label for="sidebar-collapsed" class="sidebar-toggle"' in response.text
    assert "#sidebar-collapsed:checked ~ .layout .sidebar" in response.text


def test_items_page_sidebar_is_css_resizable(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    assert "resize: horizontal" in response.text


def test_items_page_sidebar_is_inside_the_single_filter_form(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    response = client.get("/")

    form_start = response.text.index("<form")
    aside_start = response.text.index("<aside")
    form_end = response.text.index("</form>")
    assert form_start < aside_start < form_end


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

    assert '<select name="board" id="sidebar-board" onchange="this.form.submit()">' in response.text
    assert '<select name="board_scope" id="sidebar-board-scope" onchange="this.form.submit()">' in response.text


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
    start = response_text.index("Fix version")
    return response_text[start : response_text.index("</details>", start)]


def _field_checklist(response_text: str, field_label: str) -> str:
    start = response_text.index(f">{field_label}<")
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
    start = response_text.rindex("<details", 0, response_text.index("Fix version"))
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


def test_items_page_fix_version_panel_stays_open_when_a_version_is_selected(tmp_path: Path) -> None:
    client = _synced_client(tmp_path)

    closed_tag = _fix_version_details_tag(client.get("/").text)
    open_tag = _fix_version_details_tag(client.get("/", params={"fixVersion": "helm-chart-sa 3.4.4"}).text)

    assert " open" not in closed_tag
    assert " open" in open_tag


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
