from __future__ import annotations

import asyncio
from pathlib import Path
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
from ...metadata import (
    MetadataError,
    add_local_board,
    delete_local_board,
    rename_local_board,
    set_board_active,
    set_local_board_filters,
    version_name,
)
from ...shadow import ShadowError, refresh_local_issue_after_push, set_field
from ...sync import issue_key_sort_key
from ...view import (
    FILTER_ANY,
    as_dict,
    as_list,
    display_name,
    distinct_field_values,
    label_counts,
    load_cached_boards,
    load_manifest_items,
    local_issues,
)
from ..render import render_pills
from ..widgets.prompts import (
    ConfirmScreen,
    ConfirmWithInputScreen,
    MultiOptionPickerScreen,
    TextPromptScreen,
)
from ..widgets.tables import ClickableRowDataTable
from .filters import BOARD_KEY, BOARD_SCOPE_KEY, PATTERN_KEY, FILTER_FIELDS, FilterField

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

    def __init__(self, *, standalone: bool = True, items: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.standalone = standalone
        # Reuses IndexScreen's already-loaded (and incrementally kept fresh)
        # item list when opened via `M` from the index, instead of Boards/
        # Labels each re-scanning every locally synced issue from scratch --
        # that rescan is what made opening a local board's editor feel slow.
        # None (the standalone `jira-wb meta` entry point, which has no such
        # list) falls back to each screen's own on-demand load.
        self._items = items

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
            self.app.push_screen(BoardsScreen(items=self._items))
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
        if self.app.is_project_read_only(self.app.project):
            self.notify(f"cannot modify metadata: project {self.app.project} is read-only", severity="warning")
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

    def _check_not_read_only(self) -> bool:
        if self.app.is_project_read_only(self.app.project):
            self.notify(f"cannot modify metadata: project {self.app.project} is read-only", severity="warning")
            return False
        return True

    @work
    async def action_add(self) -> None:
        if not self._check_not_read_only():
            return
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
        if not self._check_not_read_only():
            return
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
        if not self._check_not_read_only():
            return
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
        if not self._check_not_read_only():
            return
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
        if not self._check_not_read_only():
            return
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
        # Also drops the cached item list -- same "explicit reload always
        # means fresh" convention as everywhere else, so a manual reload
        # actually re-scans instead of reusing a now-possibly-stale list.
        self._items = None
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class LocalBoardEditScreen(Screen[bool]):
    """Single-pane create/edit for a local board: name plus the same
    filterable dimensions Filters has (component/fixVersion/assignee/
    project/etc, minus the board/board-scope/toggle rows that don't apply
    here) and a text pattern, matched with the exact matches_field/
    matches_filter primitives filter_items already uses -- no JQL involved,
    so it works regardless of what our narrow JQL compiler can or can't
    parse. One screen owns the whole lifecycle (name and filters edited
    together, not a name prompt followed by a separate filter picker) and
    persists on save -- ctrl+s validates (a board needs a name) and saves,
    q/Escape cancels without saving anything, unlike the "always returns
    current state" convention elsewhere in Meta: a blank name has nowhere
    sensible to fall back to, so a real cancel path is needed here."""

    NAME_KEY = "name"
    ACTIVE_FILTER_KEY = "activeFilter"

    BINDINGS = [
        Binding("d", "clear_selected", "Clear"),
        Binding("ctrl+s", "save", "Save"),
        Binding("q", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        jira_dir: Path,
        items: list[dict[str, Any]],
        *,
        existing_name: str | None = None,
        field_filters: dict[str, list[str]] | None = None,
        pattern: str | None = None,
        active_filter: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._jira_dir = jira_dir
        self._items = items
        self._existing_name = existing_name
        self.board_name = existing_name or ""
        self.field_filters: dict[str, list[str]] = {k: list(v) for k, v in (field_filters or {}).items() if v}
        self.pattern = pattern
        self.active_filter: dict[str, Any] | None = active_filter

    def _fields(self) -> list[FilterField]:
        return [
            spec
            for spec in FILTER_FIELDS
            if spec.kind in ("choice", "text") and spec.key not in (BOARD_KEY, BOARD_SCOPE_KEY)
        ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="local-board-edit-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Field", key="field", width=16)
        table.add_column("Value", key="value")
        self._rebuild_rows()

    def _row_value(self, spec: FilterField) -> Any:
        if spec.key == PATTERN_KEY:
            return self.pattern or FILTER_ANY
        values = self.field_filters.get(spec.key)
        if not values:
            return FILTER_ANY
        if spec.key in ("component", "fixVersion"):
            return render_pills(values)
        return ", ".join(values)

    def _active_filter_summary(self) -> str:
        if not self.active_filter:
            return "(none)"
        parts = []
        for key, values in (self.active_filter.get("fieldFilters") or {}).items():
            if values:
                parts.append(f"{key}={','.join(values)}")
        pattern = self.active_filter.get("pattern")
        if pattern:
            parts.append(f"text={pattern}")
        return "; ".join(parts) if parts else "(matches everything)"

    def _rebuild_rows(self) -> None:
        table = self.query_one(DataTable)
        previous = self._current_key()
        table.clear()
        table.add_row("Name", self.board_name or FILTER_ANY, key=self.NAME_KEY)
        for spec in self._fields():
            table.add_row(spec.label, self._row_value(spec), key=spec.key)
        table.add_row("Active filter", self._active_filter_summary(), key=self.ACTIVE_FILTER_KEY)
        valid_keys = {self.NAME_KEY, self.ACTIVE_FILTER_KEY, *(spec.key for spec in self._fields())}
        if previous in valid_keys:
            table.move_cursor(row=table.get_row_index(previous))
        self.sub_title = f"editing {self.board_name}" if self._existing_name else "new local board"

    def _current_key(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value

    def _spec_for(self, key: str) -> FilterField:
        return next(spec for spec in self._fields() if spec.key == key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        self.action_edit(key)

    @work
    async def action_edit(self, key: str) -> None:
        if key == self.NAME_KEY:
            value = await self.app.push_screen_wait(TextPromptScreen("Board name:", initial=self.board_name))
            if value and value.strip():
                self.board_name = value.strip()
            self._rebuild_rows()
            return
        if key == self.ACTIVE_FILTER_KEY:
            current = self.active_filter or {}
            result = await self.app.push_screen_wait(
                LocalBoardActiveFilterScreen(
                    self._items,
                    field_filters=current.get("fieldFilters") or {},
                    pattern=current.get("pattern"),
                )
            )
            self.active_filter = result
            self._rebuild_rows()
            return
        spec = self._spec_for(key)
        if spec.kind == "text":
            value = await self.app.push_screen_wait(TextPromptScreen("Text filter:", initial=self.pattern or ""))
            self.pattern = value
            self._rebuild_rows()
            return
        counts = distinct_field_values(self._items, spec.key, empty_bucket=spec.empty_bucket)
        label_to_value: dict[str, str] = {}
        options: list[str] = []
        current_values = self.field_filters.get(spec.key, [])
        selected_labels: list[str] = []
        for value, count in counts:
            label = f"{value} ({count})"
            options.append(label)
            label_to_value[label] = value
            if value in current_values:
                selected_labels.append(label)
        chosen = await self.app.push_screen_wait(
            MultiOptionPickerScreen(f"{spec.label} filter:", options, selected=selected_labels)
        )
        values = [label_to_value[label] for label in chosen]
        if values:
            self.field_filters[spec.key] = values
        else:
            self.field_filters.pop(spec.key, None)
        self._rebuild_rows()

    def action_clear_selected(self) -> None:
        key = self._current_key()
        if key is None or key == self.NAME_KEY:
            return
        if key == self.ACTIVE_FILTER_KEY:
            self.active_filter = None
            self._rebuild_rows()
            return
        spec = self._spec_for(key)
        if spec.key == PATTERN_KEY:
            self.pattern = None
        else:
            self.field_filters.pop(spec.key, None)
        self._rebuild_rows()

    def action_save(self) -> None:
        clean_name = self.board_name.strip()
        if not clean_name:
            self.notify("board name is required", severity="warning")
            return
        try:
            if self._existing_name is None:
                add_local_board(self._jira_dir, clean_name, self.field_filters, self.pattern, self.active_filter)
            else:
                if clean_name != self._existing_name:
                    rename_local_board(self._jira_dir, self._existing_name, clean_name)
                set_local_board_filters(
                    self._jira_dir, clean_name, self.field_filters, self.pattern, self.active_filter
                )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class LocalBoardActiveFilterScreen(Screen[dict[str, Any] | None]):
    """Nested sub-editor for a local board's optional "active filter" --
    same field-filter-row editing mechanics as LocalBoardEditScreen's own
    membership filter, minus the Name row (this only defines the
    active/backlog split, not board membership). Always closes (q/Esc)
    returning the current fieldFilters+pattern as a dict, never None on its
    own -- this screen only opens once the parent's Active filter row is
    being defined, so there's no separate cancel state to model here;
    clearing back to "no active filter at all" is the parent row's own
    'd' (clear) action, not this screen's job."""

    BINDINGS = [
        Binding("d", "clear_selected", "Clear"),
        Binding("q", "close", "Done"),
        Binding("escape", "close", "Done"),
    ]

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        field_filters: dict[str, list[str]],
        pattern: str | None,
    ) -> None:
        super().__init__()
        self._items = items
        self.field_filters: dict[str, list[str]] = {k: list(v) for k, v in field_filters.items() if v}
        self.pattern = pattern

    def _fields(self) -> list[FilterField]:
        return [
            spec
            for spec in FILTER_FIELDS
            if spec.kind in ("choice", "text") and spec.key not in (BOARD_KEY, BOARD_SCOPE_KEY)
        ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="active-filter-edit-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Field", key="field", width=16)
        table.add_column("Value", key="value")
        self._rebuild_rows()

    def _row_value(self, spec: FilterField) -> Any:
        if spec.key == PATTERN_KEY:
            return self.pattern or FILTER_ANY
        values = self.field_filters.get(spec.key)
        if not values:
            return FILTER_ANY
        if spec.key in ("component", "fixVersion"):
            return render_pills(values)
        return ", ".join(values)

    def _rebuild_rows(self) -> None:
        table = self.query_one(DataTable)
        previous = self._current_key()
        table.clear()
        for spec in self._fields():
            table.add_row(spec.label, self._row_value(spec), key=spec.key)
        valid_keys = {spec.key for spec in self._fields()}
        if previous in valid_keys:
            table.move_cursor(row=table.get_row_index(previous))
        self.sub_title = "editing active filter"

    def _current_key(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value

    def _spec_for(self, key: str) -> FilterField:
        return next(spec for spec in self._fields() if spec.key == key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        self.action_edit(key)

    @work
    async def action_edit(self, key: str) -> None:
        spec = self._spec_for(key)
        if spec.kind == "text":
            value = await self.app.push_screen_wait(TextPromptScreen("Text filter:", initial=self.pattern or ""))
            self.pattern = value
            self._rebuild_rows()
            return
        counts = distinct_field_values(self._items, spec.key, empty_bucket=spec.empty_bucket)
        label_to_value: dict[str, str] = {}
        options: list[str] = []
        current_values = self.field_filters.get(spec.key, [])
        selected_labels: list[str] = []
        for value, count in counts:
            label = f"{value} ({count})"
            options.append(label)
            label_to_value[label] = value
            if value in current_values:
                selected_labels.append(label)
        chosen = await self.app.push_screen_wait(
            MultiOptionPickerScreen(f"{spec.label} filter:", options, selected=selected_labels)
        )
        values = [label_to_value[label] for label in chosen]
        if values:
            self.field_filters[spec.key] = values
        else:
            self.field_filters.pop(spec.key, None)
        self._rebuild_rows()

    def action_clear_selected(self) -> None:
        key = self._current_key()
        if key is None:
            return
        spec = self._spec_for(key)
        if spec.key == PATTERN_KEY:
            self.pattern = None
        else:
            self.field_filters.pop(spec.key, None)
        self._rebuild_rows()

    def action_close(self) -> None:
        self.dismiss({"fieldFilters": dict(self.field_filters), "pattern": self.pattern})


class BoardsScreen(Screen[None]):
    """Kanban/agile board browser, unified with locally-defined "local"
    boards (bespoke saved filters -- see LocalBoardEditScreen). Jira
    boards are list-only here (their membership comes from a real Jira
    board's own filter, so "editing" one means changing the underlying
    Jira board or the issue's own fields, not this screen) -- but every
    board, Jira or local, has a purely local "active" switch you can flip
    to hide/show it in the Filters screen's board picker (this list always
    shows everything, active or not, so you can find and re-enable one)."""

    BINDINGS = [
        Binding("n", "add", "New"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("a", "toggle_active", "Toggle active"),
        Binding("R", "reload", "Reload"),
        Binding("q", "close", "Back"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(self, *, items: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.boards: list[dict[str, Any]] = []
        # See MetaScreen's own `_items` -- reused here so New/Edit don't
        # re-scan every locally synced issue from scratch each time; lazily
        # populated (and cached for the rest of this screen's lifetime) when
        # opened standalone, where no pre-loaded list exists yet.
        self._items = items

    def _manifest_items(self) -> list[dict[str, Any]]:
        if self._items is None:
            self._items = load_manifest_items(self.app.jira_dir, self.app.component_field)
        return self._items

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="boards-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Name", key="name")
        table.add_column("Active", key="active")
        table.add_column("Type", key="type")
        table.add_column("Status", key="status")
        table.add_column("Backlog", key="backlog")
        self._reload()

    def _reload(self) -> None:
        # Ensures the *current* project's boards get live-fetched at least
        # once (same auto-refresh-on-first-open convenience this screen has
        # always had) -- the table itself then always shows every synced
        # project's boards plus local ones, via load_cached_boards.
        try:
            load_meta_boards(
                self.app.jira_dir,
                self.app.project,
                self.app.component_field,
                self.app.jira_url,
                self.app.jira_email,
                self.app.jira_api_token,
            )
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
        self.boards = load_cached_boards(self.app.jira_dir)
        self._rebuild_table()

    def _row_key(self, board: dict[str, Any]) -> str:
        if board.get("kind") == "local":
            return f"local:{board.get('name') or ''}"
        return f"jira:{board.get('id') or board.get('name') or ''}"

    def _rebuild_table(self) -> None:
        table = self.query_one(DataTable)
        previous = self._current_key()
        table.clear()
        for board in self.boards:
            if board.get("kind") == "local":
                board_type = "local"
                status = "-"
                backlog = "-"
            else:
                reason = board.get("unsupportedReason")
                board_type = "jira unsupported" if reason else "jira"
                status = f"unsupported: {reason}" if reason else "ok"
                backlog_keys = board.get("backlogKeys")
                backlog = f"{len(backlog_keys)} issues" if isinstance(backlog_keys, list) else "n/a"
            table.add_row(
                str(board.get("name") or ""),
                "yes" if board.get("active", True) else "",
                board_type,
                status,
                backlog,
                key=self._row_key(board),
            )
        valid_keys = {self._row_key(board) for board in self.boards}
        if table.row_count and previous in valid_keys:
            table.move_cursor(row=table.get_row_index(previous))
        self.sub_title = f"{table.row_count} boards"

    def _current_key(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value

    def _current_board(self) -> dict[str, Any] | None:
        key = self._current_key()
        if key is None:
            return None
        return next((board for board in self.boards if self._row_key(board) == key), None)

    @work
    async def action_add(self) -> None:
        items = self._manifest_items()
        saved = await self.app.push_screen_wait(
            LocalBoardEditScreen(self.app.jira_dir, items, existing_name=None, field_filters={}, pattern=None)
        )
        if saved:
            self._reload()

    @work
    async def action_edit(self) -> None:
        board = self._current_board()
        if board is None:
            self.notify("no boards found")
            return
        if board.get("kind") != "local":
            self.notify("Jira boards can't be edited locally")
            return
        items = self._manifest_items()
        saved = await self.app.push_screen_wait(
            LocalBoardEditScreen(
                self.app.jira_dir,
                items,
                existing_name=str(board.get("name") or ""),
                field_filters=board.get("fieldFilters") or {},
                pattern=board.get("pattern"),
                active_filter=board.get("activeFilter"),
            )
        )
        if saved:
            self._reload()

    @work
    async def action_delete(self) -> None:
        board = self._current_board()
        if board is None:
            self.notify("no boards found")
            return
        if board.get("kind") != "local":
            self.notify("Jira boards can't be deleted locally")
            return
        name = str(board.get("name") or "")
        confirmed = await self.app.push_screen_wait(ConfirmScreen(f"Delete local board {name}?"))
        if not confirmed:
            return
        try:
            delete_local_board(self.app.jira_dir, name)
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            return
        self._reload()

    @work
    async def action_toggle_active(self) -> None:
        board = self._current_board()
        if board is None:
            self.notify("no boards found")
            return
        kind = str(board.get("kind") or "jira")
        identifier = board.get("name") if kind == "local" else board.get("id")
        try:
            set_board_active(self.app.jira_dir, kind, identifier, not board.get("active", True))
        except MetadataError as exc:
            self.notify(f"error: {exc}", severity="error")
            return
        self._reload()

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

        skipped = [item for item in affected if self.app.is_project_read_only(item.get("project"))]
        if skipped:
            affected = [item for item in affected if item not in skipped]
            skipped_projects = sorted({item.get("project") for item in skipped if item.get("project")})
            self.notify(
                f"skipping {len(skipped)} issue(s) in read-only project(s) {', '.join(skipped_projects)}",
                severity="warning",
            )
        if not affected:
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
