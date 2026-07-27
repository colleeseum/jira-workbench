from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from ...view import full_diff_texts, side_by_side_diff_lines, wrap_preview_lines
from ..widgets.tables import ClickableRowDataTable

DIFF_ROW_STYLES = {
    "equal": None,
    "changed": "yellow",
    "removed": "red",
    "added": "green",
}

MIN_COLUMN_WIDTH = 20
# DataTable's cell_padding (1 char on each side of each column) eats into
# the 2-column layout across 3 boundaries (left edge, middle, right edge).
COLUMN_PADDING_OVERHEAD = 6


class SideBySideDiffScreen(Screen[None]):
    """Full vimdiff-style before/after comparison for one item's local
    shadow changes -- fields, description, and comments, line-aligned in
    two columns."""

    BINDINGS = [
        Binding("q", "close", "Back"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(self, jira_dir: Path, key: str, *, component_field: str | None = None) -> None:
        super().__init__()
        self.jira_dir = jira_dir
        self.key = key
        self.component_field = component_field

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="diff-table", cursor_type="row")
        yield Footer()

    def _column_width(self) -> int:
        # Auto-calculated from the actual terminal size (which varies) so
        # long description/comment lines wrap to fit -- no horizontal
        # scrolling needed for normal text, with a functional minimum for
        # very narrow terminals.
        return max(MIN_COLUMN_WIDTH, (self.size.width - COLUMN_PADDING_OVERHEAD) // 2)

    def on_mount(self) -> None:
        self.title = f"{self.key} -- diff (remote vs local)"
        table = self.query_one(DataTable)
        table.add_column("Remote (before)", key="before")
        table.add_column("Local (after)", key="after")
        column_width = self._column_width()
        before_text, after_text = full_diff_texts(self.jira_dir, self.key, self.component_field)
        before_wrapped = "\n".join(wrap_preview_lines(before_text, column_width))
        after_wrapped = "\n".join(wrap_preview_lines(after_text, column_width))
        rows = side_by_side_diff_lines(before_wrapped, after_wrapped)
        for index, (tag, left, right) in enumerate(rows):
            style = DIFF_ROW_STYLES.get(tag)
            left_cell = Text(left, style=style) if style else left
            right_cell = Text(right, style=style) if style else right
            table.add_row(left_cell, right_cell, key=str(index))
        changed = sum(1 for tag, _, _ in rows if tag != "equal")
        self.sub_title = f"{changed} changed line{'s' if changed != 1 else ''} of {len(rows)}"

    def action_close(self) -> None:
        self.dismiss(None)
