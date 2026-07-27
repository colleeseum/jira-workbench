from __future__ import annotations

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
    load_meta_versions,
    meta_components_output,
    release_meta_version,
    rename_meta_version,
    version_identifier,
)
from ...metadata import MetadataError, version_name
from ..widgets.prompts import ConfirmScreen, ConfirmWithInputScreen, TextPromptScreen
from ..widgets.tables import ClickableRowDataTable

ACTIONS = [
    ("Versions", "List fix versions"),
    ("Components", "List effective workbench components"),
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
                version_name(version),
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
        self._reload()

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
