from __future__ import annotations

from collections.abc import Awaitable, Callable

from textual import events
from textual.coordinate import Coordinate
from textual.widgets import DataTable
from textual.widgets.data_table import RowKey


class ClickableRowDataTable(DataTable):
    """A row-cursor DataTable where a single click anywhere in a row selects it.

    Textual's own click handling only posts RowSelected when a click's
    (row, column) exactly matches the *existing* cursor coordinate -- so in
    row-cursor mode, clicking any row other than the one already under the
    cursor just moves the cursor there without selecting it, requiring a
    second, now-matching click to actually open it. There's no reason to
    withhold selection on the first click of a row-cursor table (unlike
    cell/column cursors, where "highlight, then click again to confirm"
    can make sense) -- every click on a row selects it immediately.

    Textual's message dispatch calls a "_on_click"-named method separately
    for *every* class in the MRO that defines one (see
    MessagePump._get_dispatch_methods) -- overriding it here always ALSO
    triggers DataTable's own base implementation independently, doubling
    every resulting message (HeaderSelected, RowSelected, ...) for a single
    click, regardless of whether this override calls super() or fully
    reimplements the logic. event.prevent_default() is the documented way
    to suppress those base-class handlers.

    Optionally, a single column can be designated a "link" column: clicking
    a cell in that column calls `on_link_click` with the row's key instead
    of selecting the row -- e.g. Index's Dev/Git status column opening a
    PR/branch in a browser. Every other caller (Detail's table, Meta
    screens) passes neither kwarg and is unaffected.

    `on_link_click` runs via `run_worker` rather than being awaited directly
    -- `_on_click` is a plain message handler, not a worker, and a hook that
    wants to show a modal screen (`push_screen_wait`) can only do that from
    inside an actual worker context.
    """

    def __init__(
        self,
        *args: object,
        link_column: str | None = None,
        on_link_click: Callable[[RowKey], Awaitable[None]] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._link_column = link_column
        self._on_link_click = on_link_click

    async def _on_click(self, event: events.Click) -> None:
        event.prevent_default()
        self._set_hover_cursor(True)
        meta = event.style.meta
        if "row" not in meta or "column" not in meta:
            return
        if self.cursor_type != "row" and meta.get("out_of_bounds", False):
            return

        row_index = meta["row"]
        column_index = meta["column"]
        is_header_click = self.show_header and row_index == -1
        is_row_label_click = self.show_row_labels and column_index == -1
        if is_header_click:
            column = self.ordered_columns[column_index]
            self.post_message(DataTable.HeaderSelected(self, column.key, column_index, label=column.label))
            return
        if is_row_label_click:
            row = self.ordered_rows[row_index]
            self.post_message(DataTable.RowLabelSelected(self, row.key, row_index, label=row.label))
            return
        if not (self.show_cursor and self.cursor_type != "none"):
            return

        if self._link_column is not None and self._on_link_click is not None:
            column = self.ordered_columns[column_index]
            if str(column.key.value) == self._link_column:
                row = self.ordered_rows[row_index]
                self.run_worker(self._on_link_click(row.key))
                event.stop()
                return

        new_coordinate = Coordinate(row_index, column_index)
        if self.cursor_type == "row":
            highlight_click = True
        else:
            highlight_click = new_coordinate == self.cursor_coordinate
        self.cursor_coordinate = new_coordinate
        if highlight_click:
            self._post_selected_message()
        self._scroll_cursor_into_view(animate=True)
        event.stop()
