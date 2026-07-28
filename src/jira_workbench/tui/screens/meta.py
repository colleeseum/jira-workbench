from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, ListItem, ListView, Static

from ...cli import (
    add_meta_component,
    add_meta_component_field_option,
    add_meta_version,
    archive_meta_version,
    delete_meta_version,
    filter_versions,
    load_meta_boards,
    load_meta_versions,
    meta_components_output,
    release_meta_version,
    rename_meta_version,
    version_identifier,
)
from ...metadata import MetadataError, version_name
from ...shadow import ShadowError, refresh_local_issue_after_push, set_field
from ...sync import issue_key_sort_key
from ...view import as_dict, as_list, display_name, label_counts, load_manifest_items, local_issues
from ..render import render_pills
from ..widgets.prompts import ConfirmScreen, ConfirmWithInputScreen, TextPromptScreen
from ..widgets.tables import ClickableRowDataTable

ACTIONS = [
    ("Versions", "List fix versions"),
    ("Components", "List effective workbench components"),
    ("Boards", "List Kanban boards and membership"),
    ("Labels", "List labels and their issue counts"),
]


class MetaScreen(Screen[None]):
    """Top-level metadata menu: open the versions browser, or view component metadata."""

    DEFAULT_CSS = """
    MetaScreen ListView {
        height: auto;
    }
    MetaScreen #meta-output {
        height: 1fr;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("n", "add_selected", "New"),
        Binding("q", "close", "Back"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(self, *, standalone: bool = True) -> None:
        super().__init__()
        self.standalone = standalone

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(
            *(ListItem(Label(f"{label:<12} {description}")) for label, description in ACTIONS),
            id="meta-actions",
        )
        yield Static("Press Enter to view metadata.", id="meta-output")
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = self.query_one(ListView).index or 0
        self.action_open_selected(index)

    @work
    async def action_open_selected(self, index: int | None = None) -> None:
        if index is None:
            index = self.query_one(ListView).index or 0
        if index == 0:
            self.app.push_screen(VersionsScreen(default_filter=self.app.versions_filter))
            return
        if index == 2:
            self.app.push_screen(BoardsScreen())
            return
        if index == 3:
            self.app.push_screen(LabelsScreen())
            return
        output_widget = self.query_one("#meta-output", Static)
        try:
            text = meta_components_output(
                self.app.jira_dir,
                self.app.project,
                self.app.component_field,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            text = f"error: {exc}"
        output_widget.update(Text(text))

    @work
    async def action_add_selected(self) -> None:
        index = self.query_one(ListView).index or 0
        if index == 2:
            self.notify("Boards are read-only; edit membership by changing the underlying component/label")
            return
        if index == 3:
            self.notify("Open Labels (Enter) to add, rename, or delete a label")
            return
        label = "version" if index == 0 else "component"
        name = await self.app.push_screen_wait(TextPromptScreen(f"Add {label}:"))
        if not name:
            return
        output_widget = self.query_one("#meta-output", Static)
        try:
            if index == 0:
                message = add_meta_version(
                    self.app.jira_dir,
                    name,
                    self.app.project,
                    self.app.jira_url,
                    self.app.jira_email,
                    self.app.jira_api_token,
                )
            elif self.app.component_field and self.app.component_field != "components":
                context_id = await self.app.push_screen_wait(
                    TextPromptScreen(f"Context id for {self.app.component_field}:")
                )
                message = add_meta_component_field_option(
                    self.app.jira_dir,
                    name,
                    self.app.project,
                    self.app.component_field,
                    context_id,
                    self.app.jira_url,
                    self.app.jira_email,
                    self.app.jira_api_token,
                )
            else:
                message = add_meta_component(
                    self.app.jira_dir,
                    name,
                    self.app.project,
                    self.app.jira_url,
                    self.app.jira_email,
                    self.app.jira_api_token,
                )
        except MetadataError as exc:
            message = f"error: {exc}"
        output_widget.update(Text(message))

    def action_close(self) -> None:
        if self.standalone:
            self.app.exit()
        else:
            self.dismiss(None)


class VersionsScreen(Screen[None]):
    """Interactive fix-version browser: filter, add, rename, release, archive, delete."""

    BINDINGS = [
        Binding("slash", "filter", "Filter"),
        Binding("backslash", "clear_filter", "Clear filter"),
        Binding("e", "rename", "Rename"),
        Binding("r", "release", "Release"),
        Binding("a", "archive", "Archive"),
        Binding("d", "delete", "Delete"),
        Binding("n", "add", "New"),
        Binding("R", "reload", "Reload"),
        Binding("q", "close", "Back"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(self, *, default_filter: str | None = None) -> None:
        super().__init__()
        self.versions: list[dict[str, Any]] = []
        self.current_filter = default_filter

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="versions-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Name", key="name")
        table.add_column("State", key="state")
        table.add_column("Archived", key="archived")
        table.add_column("ID", key="id")
        self._reload()

    def _reload(self) -> None:
        try:
            self.versions = load_meta_versions(
                self.app.jira_dir,
                self.app.project,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            self.versions = []
        self._rebuild_table()

    def _visible(self) -> list[dict[str, Any]]:
        visible, error = filter_versions(self.versions, self.current_filter)
        if error:
            self.notify(error, severity="error")
        return visible

    def _rebuild_table(self) -> None:
        table = self.query_one(DataTable)
        previous = self._current_identifier()
        table.clear()
        visible = self._visible()
        for version in visible:
            table.add_row(
                render_pills([version_name(version)]),
                "released" if version.get("released") else "unreleased",
                "yes" if version.get("archived") else "",
                str(version.get("id") or ""),
                key=version_identifier(version),
            )
        if table.row_count and previous in {version_identifier(v) for v in visible}:
            table.move_cursor(row=table.get_row_index(previous))
        subtitle = f"{table.row_count} versions"
        if self.current_filter:
            subtitle += f"  filter={self.current_filter}"
        self.sub_title = subtitle

    def _current_identifier(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value

    def _current_version(self) -> dict[str, Any] | None:
        identifier = self._current_identifier()
        if identifier is None:
            return None
        return next((v for v in self._visible() if version_identifier(v) == identifier), None)

    @work
    async def action_filter(self) -> None:
        value = await self.app.push_screen_wait(
            TextPromptScreen("Version regex filter:", initial=self.current_filter or "")
        )
        self.current_filter = value
        self._rebuild_table()

    def action_clear_filter(self) -> None:
        self.current_filter = None
        self._rebuild_table()

    @work
    async def action_add(self) -> None:
        name = await self.app.push_screen_wait(TextPromptScreen("Add version:"))
        if not name:
            return
        try:
            message = add_meta_version(
                self.app.jira_dir,
                name,
                self.app.project,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            return
        self.notify(message.splitlines()[0])
        self._reload()

    @work
    async def action_rename(self) -> None:
        version = self._current_version()
        if version is None:
            self.notify("no versions found")
            return
        current_name = version_name(version)
        new_name = await self.app.push_screen_wait(TextPromptScreen(f"Rename {current_name} to:"))
        if not new_name:
            return
        try:
            message = rename_meta_version(
                self.app.jira_dir,
                version_identifier(version),
                new_name,
                self.app.project,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            return
        self.notify(message.splitlines()[0])
        # Renaming only updates the live Jira version and this screen's own
        # versions.json cache -- every locally synced issue's own fixVersions
        # still has the old name baked in until it's re-fetched, which is
        # why Index/Detail/reports kept showing it even though Meta already
        # had the new one.
        await self._refresh_issues_referencing_version(current_name)
        self._reload()

    async def _refresh_issues_referencing_version(self, old_name: str) -> None:
        keys = []
        for issue in local_issues(self.app.jira_dir):
            fields = as_dict(issue.get("fields"))
            names = {display_name(value) for value in as_list(fields.get("fixVersions"))}
            if old_name in names:
                key = display_name(issue.get("key"))
                if key:
                    keys.append(key)
        if not keys:
            return
        keys = sorted(keys, key=issue_key_sort_key)
        try:
            client = self.app.get_api_client()
        except MetadataError:
            # The rename already succeeded in Jira -- a missing API client
            # just means these local copies stay stale until the next full
            # sync, not that the rename itself failed.
            return
        component_field = self.app.component_field or "components"
        refreshed = 0
        for key in keys:
            try:
                await asyncio.to_thread(refresh_local_issue_after_push, self.app.jira_dir, key, client, component_field)
            except ShadowError:
                continue
            self.app.mark_changed(key)
            refreshed += 1
        if refreshed:
            self.notify(f"refreshed {refreshed} local issue(s) referencing this version")

    @work
    async def action_release(self) -> None:
        version = self._current_version()
        if version is None:
            self.notify("no versions found")
            return
        release_date = await self.app.push_screen_wait(
            TextPromptScreen(f"Release date for {version_name(version)} (optional):")
        )
        try:
            message = release_meta_version(
                self.app.jira_dir,
                version_identifier(version),
                release_date,
                self.app.project,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            return
        self.notify(message.splitlines()[0])
        self._reload()

    @work
    async def action_archive(self) -> None:
        version = self._current_version()
        if version is None:
            self.notify("no versions found")
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(f"Archive version {version_name(version)} ({version_identifier(version)})?")
        )
        if not confirmed:
            return
        try:
            message = archive_meta_version(
                self.app.jira_dir,
                version_identifier(version),
                self.app.project,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            return
        self.notify(message.splitlines()[0])
        self._reload()

    @work
    async def action_delete(self) -> None:
        version = self._current_version()
        if version is None:
            self.notify("no versions found")
            return
        move_fix_to = await self.app.push_screen_wait(
            ConfirmWithInputScreen(
                f"Delete version {version_name(version)} ({version_identifier(version)})? "
                "Issues using this fixVersion will have it removed unless you set a move target below.",
                input_label="Move fixVersion to (optional):",
            )
        )
        if move_fix_to is None:
            return
        try:
            message = delete_meta_version(
                self.app.jira_dir,
                version_identifier(version),
                move_fix_to or None,
                self.app.project,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            return
        self.notify(message.splitlines()[0])
        self._reload()

    def action_reload(self) -> None:
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class BoardsScreen(Screen[None]):
    """Read-only Kanban board browser: membership (via translated JQL) and
    active/backlog status, both cached from Jira. No add/edit here -- board
    membership is a consequence of an issue's component/labels, so "editing
    a board" means changing those fields on the issue itself, not this
    screen; see the README's Boards section for why."""

    BINDINGS = [
        Binding("R", "reload", "Reload"),
        Binding("q", "close", "Back"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.boards: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="boards-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Name", key="name")
        table.add_column("Type", key="type")
        table.add_column("Status", key="status")
        table.add_column("Backlog", key="backlog")
        self._reload()

    def _reload(self) -> None:
        try:
            self.boards = load_meta_boards(
                self.app.jira_dir,
                self.app.project,
                self.app.component_field,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            self.boards = []
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for board in self.boards:
            reason = board.get("unsupportedReason")
            status = f"unsupported: {reason}" if reason else "ok"
            backlog_keys = board.get("backlogKeys")
            backlog = f"{len(backlog_keys)} issues" if isinstance(backlog_keys, list) else "n/a"
            table.add_row(
                str(board.get("name") or ""),
                str(board.get("type") or ""),
                status,
                backlog,
                key=str(board.get("id") or board.get("name") or ""),
            )
        self.sub_title = f"{table.row_count} boards"

    def action_reload(self) -> None:
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class LabelsScreen(Screen[None]):
    """Label browser: every label used across locally synced issues, with its
    issue count, evaluated against the shadow-merged effective state (same as
    every other local view) so a pending local edit is reflected immediately.

    Jira has no API for labels as an independent entity, unlike Fix Versions/
    Components -- a label is just free text on each issue's `labels` field.
    So unlike VersionsScreen's rename/delete (which call a real Jira Version
    API), rename/delete here find every locally synced issue with the label,
    edit each one's shadow, and push them -- the same thing Jira's own
    bulk-edit issue navigator feature does under the hood, just without a
    dedicated API for it.
    """

    BINDINGS = [
        Binding("e", "rename", "Rename"),
        Binding("d", "delete", "Delete"),
        Binding("n", "add", "New"),
        Binding("R", "reload", "Reload"),
        Binding("q", "close", "Back"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.counts: list[tuple[str, int]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="labels-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Label", key="label")
        table.add_column("Count", key="count")
        self._reload()

    def _reload(self) -> None:
        items = load_manifest_items(self.app.jira_dir, self.app.component_field)
        self.counts = label_counts(items)
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        table = self.query_one(DataTable)
        previous = self._current_label()
        table.clear()
        for label, count in self.counts:
            table.add_row(render_pills([label]), str(count), key=label)
        if table.row_count and previous in {label for label, _ in self.counts}:
            table.move_cursor(row=table.get_row_index(previous))
        self.sub_title = f"{len(self.counts)} labels"

    def _current_label(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value

    @work
    async def action_rename(self) -> None:
        label = self._current_label()
        if label is None:
            self.notify("no labels found")
            return
        if not self.app.can_push():
            self.notify("cannot edit labels: missing Jira API configuration", severity="warning")
            return
        new_name = await self.app.push_screen_wait(TextPromptScreen(f"Rename label '{label}' to:"))
        if not new_name or new_name == label:
            return
        await self._bulk_edit(label, new_name)

    @work
    async def action_delete(self) -> None:
        label = self._current_label()
        if label is None:
            self.notify("no labels found")
            return
        if not self.app.can_push():
            self.notify("cannot edit labels: missing Jira API configuration", severity="warning")
            return
        await self._bulk_edit(label, None)

    async def _bulk_edit(self, label: str, new_name: str | None) -> None:
        items = load_manifest_items(self.app.jira_dir, self.app.component_field)
        affected = [item for item in items if label in (item.get("labels") or [])]
        if not affected:
            self.notify(f"no issues currently have label '{label}'")
            self._reload()
            return

        keys = sorted((display_name(item.get("key")) for item in affected), key=issue_key_sort_key)
        message = (
            f"Rename label '{label}' to '{new_name}' on {len(keys)} issue(s) and push them now?"
            if new_name
            else f"Delete label '{label}' from {len(keys)} issue(s) and push them now?"
        )
        confirmed = await self.app.push_screen_wait(ConfirmScreen(message))
        if not confirmed:
            return

        for item in affected:
            key = display_name(item.get("key"))
            current_labels = item.get("labels") or []
            updated = (
                [new_name if existing == label else existing for existing in current_labels]
                if new_name
                else [existing for existing in current_labels if existing != label]
            )
            set_field(self.app.jira_dir, key, "labels", updated)
            self.app.mark_changed(key)

        try:
            client = self.app.get_api_client()
        except MetadataError as exc:
            self.notify(f"labels updated locally, but could not push: {exc}", severity="warning")
            self._reload()
            return

        from .push_all import PushAllProgressScreen

        result = await self.app.push_screen_wait(
            PushAllProgressScreen(self.app.jira_dir, keys, client, self.app.component_field or "components")
        )
        if result is not None:
            self.notify(
                f"pushed={result.pushed} skipped={result.skipped} blocked={result.blocked} failed={result.failed}"
            )
        self._reload()

    @work
    async def action_add(self) -> None:
        name = await self.app.push_screen_wait(TextPromptScreen("New label:"))
        if not name:
            return
        self.notify(
            f"Jira has no standalone label registry -- apply '{name}' to an issue "
            "(Detail or New issue screen) and it will appear here."
        )

    def action_reload(self) -> None:
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)
