from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from ...shadow import (
    add_comment,
    delete_comment,
    edit_comment,
    load_shadow,
    remove_local_comment,
    undelete_comment,
)
from ...view import list_comments
from ..widgets.prompts import ConfirmScreen, TextAreaPromptScreen, TextViewScreen
from ..widgets.tables import ClickableRowDataTable

STATE_LABELS = {
    "synced": "",
    "edited": "edited, unpushed",
    "pending-delete": "marked for deletion",
    "local-new": "local, unpushed",
}


class CommentsScreen(Screen[None]):
    """One row per comment -- add, view, edit, or delete a specific comment.

    Editing/deleting an already-synced remote comment queues the change in
    the shadow (applied via issue_edit_comment / a direct DELETE on push);
    editing/deleting a not-yet-pushed local comment just mutates it in
    place, since there is nothing on Jira to reconcile with yet.
    """

    BINDINGS = [
        Binding("n", "add_comment", "New"),
        Binding("d", "delete_comment", "Delete/Undelete"),
        Binding("R", "reload", "Reload"),
        Binding("q", "close", "Back"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(self, jira_dir: Path, key: str) -> None:
        super().__init__()
        self.jira_dir = jira_dir
        self.key = key
        self.changed = False
        self._rows: list[dict[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="comments-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Created", key="created", width=20)
        table.add_column("Author", key="author", width=20)
        table.add_column("State", key="state", width=20)
        table.add_column("Comment", key="body")
        self._reload()

    def _reload(self) -> None:
        table = self.query_one(DataTable)
        previous = self._current_id()
        table.clear()
        self._rows = list_comments(self.jira_dir, self.key, shadow=load_shadow(self.jira_dir, self.key))
        for index, row in enumerate(self._rows):
            preview = row["body"].splitlines()[0][:80] if row["body"] else ""
            table.add_row(
                row["created"][:19],
                row["author"],
                STATE_LABELS.get(row["state"], row["state"]),
                preview,
                key=f"{row['id']}::{index}",
            )
        if table.row_count and previous is not None:
            for index, row in enumerate(self._rows):
                if row["id"] == previous:
                    table.move_cursor(row=index)
                    break
        self.sub_title = f"{len(self._rows)} comments"

    def _current_id(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        value = row_key.value
        if value is None:
            return None
        comment_id, _, _ = value.partition("::")
        return comment_id

    def _current_row(self) -> dict[str, str] | None:
        comment_id = self._current_id()
        if comment_id is None:
            return None
        return next((row for row in self._rows if row["id"] == comment_id), None)

    @work
    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        await self._view_current()

    @work
    async def action_add_comment(self) -> None:
        body = await self.app.push_screen_wait(TextAreaPromptScreen("New comment:"))
        if not body:
            return
        add_comment(self.jira_dir, self.key, body)
        self.changed = True
        self._reload()
        self.notify("comment added")

    async def _view_current(self) -> None:
        row = self._current_row()
        if row is None:
            self.notify("no comments")
            return
        label = f"Comment by {row['author']}" if row["author"] else "Comment"
        # Same viewer as Description: opens read-only with an Edit button that
        # switches the same screen to Save/Close in place -- one consistent
        # view-then-edit flow instead of a separate dedicated edit action.
        editable = row["state"] != "pending-delete"
        result = await self.app.push_screen_wait(TextViewScreen(label, row["body"], editable=editable))
        if result is None:
            return
        edit_comment(self.jira_dir, self.key, row["id"], result)
        self.changed = True
        self._reload()
        self.notify("comment updated")

    @work
    async def action_delete_comment(self) -> None:
        row = self._current_row()
        if row is None:
            self.notify("no comments")
            return
        if row["state"] == "pending-delete":
            undelete_comment(self.jira_dir, self.key, row["id"])
            self.changed = True
            self._reload()
            self.notify("delete undone")
            return
        if row["state"] == "local-new":
            confirmed = await self.app.push_screen_wait(ConfirmScreen("Delete this local (unpushed) comment?"))
            if not confirmed:
                return
            remove_local_comment(self.jira_dir, self.key, row["id"])
            self.notify("local comment discarded")
        else:
            confirmed = await self.app.push_screen_wait(
                ConfirmScreen("Mark this comment for deletion? It is removed from Jira on the next push.")
            )
            if not confirmed:
                return
            delete_comment(self.jira_dir, self.key, row["id"])
            self.notify("comment marked for deletion")
        self.changed = True
        self._reload()

    def action_reload(self) -> None:
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)
