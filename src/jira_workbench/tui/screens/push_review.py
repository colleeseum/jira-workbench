from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from ...shadow import load_shadow
from ...view import apply_shadow, as_dict, display_name, hierarchy_component, load_issue, shadow_change_summary
from ..widgets.tables import ClickableRowDataTable


class PushReviewScreen(Screen[bool]):
    """Lists the items about to be pushed; Enter (or click) opens a full
    side-by-side diff for the selected item. p/Enter-on-footer confirms the
    push, q/Esc cancels -- this screen itself is the confirmation step,
    replacing the old plain "Push N items?" yes/no with something you can
    actually inspect first.
    """

    BINDINGS = [
        Binding("enter", "view_diff", "View diff"),
        Binding("p", "push", "Push"),
        Binding("q", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, jira_dir: Path, keys: list[str], *, component_field: str | None = None) -> None:
        super().__init__()
        self.jira_dir = jira_dir
        self.keys = keys
        self.component_field = component_field

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="push-review-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Review changes before pushing"
        self.sub_title = f"{len(self.keys)} item(s) -- Enter to view diff, p to push, q to cancel"
        table = self.query_one(DataTable)
        table.add_column("Key", key="key", width=12)
        table.add_column("Component", key="component", width=16)
        table.add_column("Summary", key="summary")
        table.add_column("State", key="state", width=12)
        table.add_column("Changes", key="changes")
        for key in self.keys:
            issue = load_issue(self.jira_dir, key)
            shadow = load_shadow(self.jira_dir, key)
            effective = apply_shadow(issue, shadow) if shadow is not None else issue
            fields = as_dict(effective.get("fields"))
            component = hierarchy_component(effective, self.component_field)
            summary = display_name(fields.get("summary"))
            state = display_name(shadow.get("state")) or "working" if shadow else "working"
            summary_lines = shadow_change_summary(shadow, self.jira_dir, key) if shadow else []
            changes = summary_lines[0].removeprefix("Local changes: ") if summary_lines else "(no local changes)"
            table.add_row(key, component, summary, state, changes, key=key)

    def _current_key(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_view_diff()

    @work
    async def action_view_diff(self) -> None:
        key = self._current_key()
        if key is None:
            return
        from .diff_view import SideBySideDiffScreen

        await self.app.push_screen_wait(SideBySideDiffScreen(self.jira_dir, key, component_field=self.component_field))

    def action_push(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
