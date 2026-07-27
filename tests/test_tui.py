from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import DataTable, Input, TextArea

import jira_workbench.cli
from jira_workbench.metadata import load_versions
from jira_workbench.shadow import add_comment, load_shadow, set_field
from jira_workbench.sync import SyncConfig, build_manifest, sync_project, write_json
from jira_workbench.tui.app import JiraWorkbenchApp
from jira_workbench.tui.screens.detail import DetailScreen
from jira_workbench.tui.screens.diff_view import SideBySideDiffScreen
from jira_workbench.tui.screens.filters import FiltersScreen
from jira_workbench.tui.screens.help import HelpScreen
from jira_workbench.tui.screens.index import IndexScreen
from jira_workbench.tui.screens.meta import MetaScreen, VersionsScreen
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


@pytest.mark.asyncio
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
        screen.field_filters["component"] = "helm-chart"
        screen._rebuild_table()

        assert "SAT-100" in screen._row_keys
        table = screen.query_one(DataTable)
        assert str(table.get_cell("SAT-100", "")) == "◆"
        assert str(table.get_cell("SAT-100", "State")) == "To Do"
        assert str(table.get_cell("SAT-100", "Summary")) == "Cross-cutting epic"
        assert screen._row_keys == ["SAT-100", "SAT-1"]


@pytest.mark.asyncio
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
        assert "Priority ▲" in header_line


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
        assert "Priority ▼" in header_line


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

        assert isinstance(app.screen, FiltersScreen)
        assert str(table.get_cell("component", "value")) == "helm-chart"
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert index.field_filters == {"component": "helm-chart"}
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
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, IndexScreen)
        assert index.field_filters == {"assignee": "Jane Doe"}
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 1
        assert index_table.get_cell("SAT-2", "Key") == "SAT-2"


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
        await pilot.press("q")
        await pilot.pause()

        assert index.field_filters == {"fixVersion": "(none)"}
        index_table = index.query_one(DataTable)
        assert index_table.row_count == 1
        assert index_table.get_cell("SAT-2", "Key") == "SAT-2"

        # Reopen and clear back to "(any)" -- both items return.
        await pilot.press("f")
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index("fixVersion"))
        await pilot.press("enter")
        await pilot.pause()
        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "(any)"
        await pilot.press("enter")
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


def meta_app(tmp_path: Path, client: FakeMetaClient, monkeypatch: pytest.MonkeyPatch) -> JiraWorkbenchApp:
    monkeypatch.setattr(jira_workbench.cli, "jira_api_client", lambda _config: client)
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
        cache = load_versions(tmp_path)
        assert cache is not None
        assert cache["versions"][0]["name"] == "v1-renamed"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MetaScreen)


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

