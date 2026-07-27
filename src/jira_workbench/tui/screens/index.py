from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from ...metadata import MetadataError
from ...sync import issue_key_sort_key
from ...view import (
    VIRTUAL_NONE,
    cycle_swimlane,
    display_name,
    filter_items,
    filter_items_for_swimlane,
    find_item_index,
    find_next_item_index,
    is_epic_item,
    load_manifest_items,
    modified_issue_keys,
    normalize_swimlane,
    priority_color,
    refresh_index_item,
    sort_items_for_swimlane,
    SORT_FIELDS,
    swimlane_label,
    type_icon,
    with_local_index_fields,
)
from ..widgets.prompts import TextPromptScreen
from ..widgets.tables import ClickableRowDataTable
from .filters import FILTER_FIELDS

LANE_ROW_PREFIX = "__lane__::"

COLUMNS = ("", "Key", "State", "Component", "Summary", "Priority", "Assignee", "Version")
INDENT_WIDTH = 2
ICON_COLUMN_WIDTH_PLAIN = 1
ICON_COLUMN_WIDTH_INDENTED = 1 + INDENT_WIDTH


class IndexScreen(Screen[None]):
    """Work-item list: filter, search, group by swimlane, and open a detail screen."""

    BINDINGS = [
        Binding("a", "toggle_active", "Active"),
        Binding("m", "toggle_modified", "Modified"),
        Binding("f", "filters", "Filters"),
        Binding("S", "cycle_swimlane", "Swimlane"),
        Binding("slash", "search", "Search"),
        Binding("n", "search_next", "Next match"),
        Binding("N", "search_prev", "Prev match"),
        Binding("g", "goto", "Go to"),
        Binding("R", "reload", "Reload"),
        Binding("P", "push_all", "Push all"),
        Binding("M", "open_meta", "Meta"),
        Binding("v", "view_key", "View key"),
        Binding("V", "open_parent", "Parent"),
        Binding("h", "show_help", "Help"),
        Binding("q", "quit_app", "Quit"),
        Binding("escape", "quit_app", "Quit"),
    ]

    def __init__(
        self,
        *,
        component: str | None = None,
        pattern: str | None = None,
        active: bool = True,
        swimlane: str | None = None,
    ) -> None:
        super().__init__()
        self.items: list[dict[str, Any]] = []
        self.field_filters: dict[str, str] = {"component": component} if component else {}
        self.current_filter = pattern
        self.active_only = active
        self.modified_only = False
        self.current_swimlane = normalize_swimlane(swimlane)
        self.search_query: str | None = None
        self.sort_column: str | None = None
        self.sort_reverse = False
        self._row_keys: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="index-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for column in COLUMNS:
            width = ICON_COLUMN_WIDTH_INDENTED if column == "" else None
            label = self._column_label(column) if column in SORT_FIELDS else column
            table.add_column(label, key=column, width=width)
        self.items = load_manifest_items(self.app.jira_dir, self.app.component_field)
        self._rebuild_table()

    def _visible_items(self) -> list[dict[str, Any]]:
        modified_keys = modified_issue_keys(self.app.jira_dir)
        visible = filter_items(
            self.items,
            field_filters=self.field_filters,
            pattern=self.current_filter,
            active=self.active_only,
            modified_keys=modified_keys,
            modified_only=self.modified_only,
        )
        visible = filter_items_for_swimlane(visible, self.current_swimlane)
        sort_field = SORT_FIELDS.get(self.sort_column) if self.sort_column else None
        return sort_items_for_swimlane(visible, self.current_swimlane, sort_field=sort_field, reverse=self.sort_reverse)

    def _lane_identity(self, item: dict[str, Any]) -> str:
        # Stable grouping key, independent of any per-item denormalized display text
        # (e.g. a child issue's cached epicSummary can go stale relative to the epic
        # itself, which would otherwise split one epic's children across two headers).
        if self.current_swimlane == "epic":
            if is_epic_item(item):
                return display_name(item.get("key")).strip() or VIRTUAL_NONE
            return display_name(item.get("epic")).strip() or VIRTUAL_NONE
        return swimlane_label(item, self.current_swimlane) or VIRTUAL_NONE

    def _sync_icon_column_width(self, table: DataTable) -> None:
        # The indent gutter is only meaningful in epic mode -- keep the icon
        # column tight in every other swimlane mode so nothing is wasted.
        icon_column = table.ordered_columns[0]
        width = ICON_COLUMN_WIDTH_INDENTED if self.current_swimlane == "epic" else ICON_COLUMN_WIDTH_PLAIN
        if icon_column.width != width:
            icon_column.width = width
            table._require_update_dimensions = True

    def _column_label(self, name: str) -> str:
        # Always reserve the arrow's width (2 chars, sorted or not) so the
        # label's total length never changes -- DataTable only widens a
        # column when a *cell's* content grows, never on a label-only
        # change, so a label that grows after the fact gets silently
        # clipped. Reserving the space up front avoids relying on that.
        if name == self.sort_column:
            return f"{name} {'▼' if self.sort_reverse else '▲'}"
        return f"{name}  "

    def _sync_column_headers(self, table: DataTable) -> None:
        changed = False
        for column in table.ordered_columns:
            name = str(column.key.value)
            if name not in SORT_FIELDS:
                continue
            label = self._column_label(name)
            if str(column.label) != label:
                column.label = Text(label)
                changed = True
        if changed:
            table._require_update_dimensions = True

    def _rebuild_table(self) -> None:
        table = self.query_one(DataTable)
        self._sync_icon_column_width(table)
        self._sync_column_headers(table)
        previous_key = self._current_key()
        table.clear()
        self._row_keys = []
        modified_keys = modified_issue_keys(self.app.jira_dir)
        visible = self._visible_items()
        current_lane_key: str | None = None
        seen_item_keys: set[str] = set()
        for item in visible:
            lane_key = self._lane_identity(item) if self.current_swimlane != "none" else None
            is_epic_lane_head = self.current_swimlane == "epic" and is_epic_item(item)
            if lane_key is not None and lane_key != current_lane_key:
                current_lane_key = lane_key
                if not is_epic_lane_head:
                    # A real epic issue heads its own lane -- its own row
                    # (added below) carries the label, styled to stand out,
                    # so no separate blank-column divider is needed. Version
                    # and component lanes (and the epic "(none)" lane) have
                    # no underlying issue, so they still get one.
                    self._add_lane_header_row(table, item, lane_key, modified_keys, seen_item_keys)
            key_value = display_name(item.get("key"))
            if key_value in seen_item_keys:
                continue
            seen_item_keys.add(key_value)
            # Every non-epic-head row is indented under its lane in epic
            # mode -- including the "(none)" lane, for visual consistency.
            indent = self.current_swimlane == "epic" and not is_epic_lane_head
            self._add_item_row(table, item, modified_keys, bold=is_epic_lane_head, indent=indent)
        self._select_key(previous_key)
        self._update_subtitle(len(visible))

    def _add_lane_header_row(
        self,
        table: DataTable,
        item: dict[str, Any],
        lane_key: str,
        modified_keys: set[str],
        seen_item_keys: set[str],
    ) -> None:
        # A component/pattern/active filter can exclude the epic issue
        # itself from the visible list while keeping its children (e.g. the
        # epic has no component set but its children do) -- the epic is
        # then never seen as its own `item` in the loop above. Fetch it
        # directly so the header still gets the epic's real full row
        # instead of just a bold label with blank columns.
        if self.current_swimlane == "epic" and lane_key != VIRTUAL_NONE and lane_key not in seen_item_keys:
            epic_item = with_local_index_fields(self.app.jira_dir, {"key": lane_key}, self.app.component_field)
            if "summary" in epic_item:
                seen_item_keys.add(lane_key)
                self._add_item_row(table, epic_item, modified_keys, bold=True, indent=False)
                return
        lane_text = swimlane_label(item, self.current_swimlane) or VIRTUAL_NONE
        table.add_row(
            "",
            Text(lane_text, style="bold"),
            "",
            "",
            "",
            "",
            "",
            "",
            key=f"{LANE_ROW_PREFIX}{lane_key}",
        )

    def _add_item_row(
        self, table: DataTable, item: dict[str, Any], modified_keys: set[str], *, bold: bool, indent: bool
    ) -> None:
        key_value = display_name(item.get("key"))
        display_key = f"{key_value}*" if key_value in modified_keys else key_value
        glyph, color = type_icon(item.get("type"))

        def cell(value: str) -> Any:
            return Text(value, style="bold") if bold else value

        # The epic type color is deliberately confined to the Key cell (plus
        # the icon) rather than the whole row -- coloring every cell reads as
        # noisier without making epics any easier to spot than a bold Key.
        key_style = f"bold {color}" if bold else None
        key_cell = Text(display_key, style=key_style) if key_style else display_key

        priority_value = display_name(item.get("priority"))
        priority_style = " ".join(part for part in (("bold" if bold else None), priority_color(item.get("priority"))) if part)
        priority_cell = Text(priority_value, style=priority_style) if priority_style else priority_value

        # The indent lives in the icon column (a fixed-width gutter) rather
        # than as literal spaces on the Key text -- otherwise the Key column
        # auto-sizes to fit the longest indented value, leaving a much
        # bigger gap before State on every un-indented (epic) row.
        icon_text = f"{' ' * INDENT_WIDTH}{glyph}" if indent else glyph
        icon = Text(icon_text, style=f"bold {color}" if bold else color)
        table.add_row(
            icon,
            key_cell,
            cell(display_name(item.get("status"))),
            cell(display_name(item.get("component"))),
            cell(display_name(item.get("summary"))),
            priority_cell,
            cell(display_name(item.get("assignee"))),
            cell(display_name(item.get("fixVersion"))),
            key=key_value,
        )
        self._row_keys.append(key_value)

    def _update_subtitle(self, visible_count: int) -> None:
        parts = [f"{visible_count} items"]
        for spec in FILTER_FIELDS:
            if spec.kind != "choice":
                continue
            value = self.field_filters.get(spec.key)
            if value:
                parts.append(f"{spec.key}={value}")
        if self.current_filter:
            parts.append(f"filter={self.current_filter}")
        if self.modified_only:
            parts.append("modified-only")
        if not self.active_only:
            parts.append("all")
        if self.current_swimlane != "none":
            parts.append(f"swimlane={self.current_swimlane}")
        self.sub_title = "  ".join(parts)

    def _current_key(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        value = row_key.value
        if value is None or value.startswith(LANE_ROW_PREFIX):
            return None
        return value

    def _select_key(self, key: str | None) -> None:
        table = self.query_one(DataTable)
        if not self._row_keys:
            return
        target = key if key in self._row_keys else self._row_keys[0]
        table.move_cursor(row=table.get_row_index(target))

    def action_toggle_active(self) -> None:
        self.active_only = not self.active_only
        self._rebuild_table()

    def action_toggle_modified(self) -> None:
        self.modified_only = not self.modified_only
        self._rebuild_table()

    def action_cycle_swimlane(self) -> None:
        self.current_swimlane = cycle_swimlane(self.current_swimlane)
        self._rebuild_table()

    def action_reload(self) -> None:
        self.items = load_manifest_items(self.app.jira_dir, self.app.component_field)
        self._rebuild_table()
        self.notify(f"reloaded {len(self.items)} items")

    @work
    async def action_push_all(self) -> None:
        keys = sorted(modified_issue_keys(self.app.jira_dir), key=issue_key_sort_key)
        if not keys:
            self.notify("no local shadow changes")
            return
        if not self.app.can_push():
            self.notify("cannot push: missing Jira API configuration", severity="warning")
            return
        from .push_review import PushReviewScreen

        confirmed = await self.app.push_screen_wait(
            PushReviewScreen(self.app.jira_dir, keys, component_field=self.app.component_field)
        )
        if not confirmed:
            return
        try:
            client = self.app.get_api_client()
        except MetadataError as exc:
            self.notify(f"cannot push: {exc}", severity="error")
            return

        from .push_all import PushAllProgressScreen

        result = await self.app.push_screen_wait(
            PushAllProgressScreen(self.app.jira_dir, keys, client, self.app.component_field or "components")
        )
        for key in keys:
            self.app.mark_changed(key)
        for key in self.app.drain_changed_keys():
            refresh_index_item(self.app.jira_dir, self.items, key, self.app.component_field)
        self._rebuild_table()
        if result is not None:
            self.notify(
                f"push all: pushed={result.pushed} skipped={result.skipped} "
                f"blocked={result.blocked} failed={result.failed}"
            )

    @work
    async def action_filters(self) -> None:
        from .filters import FiltersScreen

        result = await self.app.push_screen_wait(
            FiltersScreen(
                self.items,
                active_only=self.active_only,
                modified_only=self.modified_only,
                field_filters=self.field_filters,
                pattern=self.current_filter,
            )
        )
        self.active_only = result.active_only
        self.modified_only = result.modified_only
        self.field_filters = result.field_filters
        self.current_filter = result.pattern
        self._rebuild_table()

    @work
    async def action_search(self) -> None:
        query = await self.app.push_screen_wait(TextPromptScreen("Search:"))
        if not query:
            return
        self.search_query = query
        visible = self._visible_items()
        match = find_item_index(visible, query)
        if match is not None:
            self._select_key(display_name(visible[match].get("key")))
        else:
            self.notify(f"no match for {query!r}")

    def _search_step(self, direction: int) -> None:
        if not self.search_query:
            self.notify("no active search")
            return
        visible = self._visible_items()
        current_key = self._current_key()
        current_index = next(
            (index for index, item in enumerate(visible) if display_name(item.get("key")) == current_key), 0
        )
        match = find_next_item_index(visible, self.search_query, current_index, direction=direction)
        if match is not None:
            self._select_key(display_name(visible[match].get("key")))

    def action_search_next(self) -> None:
        self._search_step(1)

    def action_search_prev(self) -> None:
        self._search_step(-1)

    @work
    async def action_goto(self) -> None:
        query = await self.app.push_screen_wait(TextPromptScreen("Go to:"))
        if not query:
            return
        visible = self._visible_items()
        match = find_item_index(visible, query)
        if match is not None:
            self._select_key(display_name(visible[match].get("key")))
        else:
            self.notify(f"no match for {query!r}")

    @work
    async def action_view_key(self) -> None:
        key = await self.app.push_screen_wait(TextPromptScreen("View work item key:"))
        if key:
            self._open_detail(key)

    def action_open_parent(self) -> None:
        current_key = self._current_key()
        if current_key is None:
            return
        item = next((item for item in self.items if display_name(item.get("key")) == current_key), None)
        parent_key = display_name(item.get("epic")).strip() if item else ""
        if not parent_key:
            self.notify("no parent on this item")
            return
        self._open_detail(parent_key)

    def action_show_help(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen())

    def action_open_meta(self) -> None:
        from .meta import MetaScreen

        self.app.push_screen(MetaScreen(standalone=False))

    def action_quit_app(self) -> None:
        self.app.exit()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        value = event.row_key.value
        if value is None or value.startswith(LANE_ROW_PREFIX):
            return
        self._open_detail(value)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        column = str(event.column_key.value)
        if column not in SORT_FIELDS:
            return
        if column == self.sort_column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self._rebuild_table()

    def _open_detail(self, key: str) -> None:
        from .detail import DetailScreen

        self.app.push_screen(DetailScreen(key=key), callback=self._on_detail_closed)

    def _on_detail_closed(self, _result: None) -> None:
        for key in self.app.drain_changed_keys():
            refresh_index_item(self.app.jira_dir, self.items, key, self.app.component_field)
        self._rebuild_table()
