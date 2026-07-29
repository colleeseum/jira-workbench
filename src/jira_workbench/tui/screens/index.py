from __future__ import annotations

import asyncio
import webbrowser
from collections import Counter
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header
from textual.widgets.data_table import RowKey

from ...devstatus import fetch_dev_status
from ...issue import IssueError, fetch_current_user, fetch_issue_type_fields
from ...metadata import MetadataError
from ...sync import issue_key_sort_key
from ...view import (
    VIRTUAL_NONE,
    assignee_first_name_map,
    cycle_swimlane,
    dev_status_indicator,
    display_component,
    display_name,
    filter_items,
    filter_items_for_swimlane,
    find_item_index,
    find_next_item_index,
    is_epic_item,
    load_cached_boards,
    load_manifest_items,
    modified_issue_keys,
    normalize_swimlane,
    pill_values,
    priority_icon,
    refresh_index_item,
    sort_items_for_swimlane,
    SORT_FIELDS,
    swimlane_label,
    type_icon,
    with_local_index_fields,
)
from ..render import render_pills
from ..widgets.prompts import OptionPickerScreen, TextPromptScreen
from ..widgets.tables import ClickableRowDataTable
from .filters import FILTER_FIELDS

LANE_ROW_PREFIX = "__lane__::"


def _seed_values(value: str | tuple[str, ...] | None) -> list[str]:
    """Normalize a constructor filter-seed value (a bare string from a CLI
    flag, or a tuple from a multi-value config entry) into a field_filters
    list -- unset/empty stays unset, matching every other field's semantics."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)

COLUMNS = ("", "Key", "State", "Component", "Summary", "Priority", "Assignee", "Version", "Dev")
# Priority now shows an icon rather than text (see _add_item_row) -- keeping the
# full "Priority" header would waste the width the icon was meant to save, so
# this is the displayed text only; the column's identity/sort key stays "Priority".
COLUMN_HEADER_LABELS: dict[str, str] = {"Priority": "Pr"}
INDENT_WIDTH = 2
ICON_COLUMN_WIDTH_PLAIN = 1
ICON_COLUMN_WIDTH_INDENTED = 1 + INDENT_WIDTH


class IndexScreen(Screen[None]):
    """Work-item list: filter, search, group by swimlane, and open a detail screen."""

    BINDINGS = [
        Binding("a", "toggle_active", "Active"),
        Binding("m", "toggle_modified", "Modified"),
        Binding("f", "filters", "Filters"),
        Binding("c", "new_issue", "New issue"),
        Binding("S", "cycle_swimlane", "Swimlane"),
        Binding("z", "toggle_lane", "Collapse lane"),
        Binding("Z", "toggle_all_lanes", "Collapse/expand all"),
        Binding("s", "save_filter", "Save filter"),
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
        project: str | tuple[str, ...] | None = None,
        status: str | tuple[str, ...] | None = None,
        component: str | tuple[str, ...] | None = None,
        fix_version: str | tuple[str, ...] | None = None,
        assignee: str | tuple[str, ...] | None = None,
        board: str | None = None,
        board_scope: str | None = None,
        pattern: str | None = None,
        active: bool = True,
        swimlane: str | None = None,
    ) -> None:
        super().__init__()
        self.items: list[dict[str, Any]] = []
        self.field_filters: dict[str, list[str]] = {}
        for key, value in (
            ("project", project),
            ("status", status),
            ("component", component),
            ("fixVersion", fix_version),
            ("assignee", assignee),
        ):
            values = _seed_values(value)
            if values:
                self.field_filters[key] = values
        self.current_filter = pattern
        self.active_only = active
        self.modified_only = False
        self.board: str | None = board
        self.board_scope: str | None = board_scope if board else None
        self.current_swimlane = normalize_swimlane(swimlane)
        self._collapsed_lanes: set[str] = set()
        self.search_query: str | None = None
        self.sort_column: str | None = None
        self.sort_reverse = False
        self._row_keys: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(
            id="index-table", cursor_type="row", link_column="Dev", on_link_click=self._open_dev_status_link
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for column in COLUMNS:
            width = ICON_COLUMN_WIDTH_INDENTED if column == "" else None
            label = self._column_label(column) if column in SORT_FIELDS else column
            table.add_column(label, key=column, width=width)
        self.items = load_manifest_items(
            self.app.jira_dir, self.app.component_field, dev_status_field=self.app.dev_status_field
        )
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
            board=self.board,
            board_scope=self.board_scope,
            max_done_age_days=(self.app.hide_done_after_days if self.board_scope == "active" else None),
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
        display = COLUMN_HEADER_LABELS.get(name, name)
        if name == self.sort_column:
            return f"{display} {'▼' if self.sort_reverse else '▲'}"
        return f"{display}  "

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
        assignee_names = assignee_first_name_map([display_name(item.get("assignee")) for item in visible])
        lane_counts = (
            Counter(self._lane_identity(item) for item in visible) if self.current_swimlane != "none" else Counter()
        )
        current_lane_key: str | None = None
        current_lane_collapsed = False
        seen_item_keys: set[str] = set()
        for item in visible:
            lane_key = self._lane_identity(item) if self.current_swimlane != "none" else None
            is_epic_lane_head = self.current_swimlane == "epic" and is_epic_item(item)
            if lane_key is not None and lane_key != current_lane_key:
                current_lane_key = lane_key
                current_lane_collapsed = lane_key in self._collapsed_lanes
                if not is_epic_lane_head:
                    # A real epic issue heads its own lane -- its own row
                    # (added below) carries the label, styled to stand out,
                    # so no separate blank-column divider is needed. Version
                    # and component lanes (and the epic "(none)" lane) have
                    # no underlying issue, so they still get one.
                    self._add_lane_header_row(
                        table,
                        item,
                        lane_key,
                        modified_keys,
                        seen_item_keys,
                        assignee_names,
                        collapsed=current_lane_collapsed,
                        count=lane_counts[lane_key],
                    )
            key_value = display_name(item.get("key"))
            if key_value in seen_item_keys:
                continue
            seen_item_keys.add(key_value)
            # A collapsed lane hides its children -- but never the epic head
            # itself in epic mode, since that row *is* the lane's header.
            if current_lane_collapsed and not is_epic_lane_head:
                continue
            # Every non-epic-head row is indented under its lane in epic
            # mode -- including the "(none)" lane, for visual consistency.
            indent = self.current_swimlane == "epic" and not is_epic_lane_head
            self._add_item_row(table, item, modified_keys, assignee_names, bold=is_epic_lane_head, indent=indent)
        self._select_key(previous_key)
        self._update_subtitle(len(visible))

    def _add_lane_header_row(
        self,
        table: DataTable,
        item: dict[str, Any],
        lane_key: str,
        modified_keys: set[str],
        seen_item_keys: set[str],
        assignee_names: dict[str, str],
        *,
        collapsed: bool,
        count: int,
    ) -> None:
        # A component/pattern/active filter can exclude the epic issue
        # itself from the visible list while keeping its children (e.g. the
        # epic has no component set but its children do) -- the epic is
        # then never seen as its own `item` in the loop above. Fetch it
        # directly so the header still gets the epic's real full row
        # instead of just a bold label with blank columns. (This path has
        # no room for a collapse arrow since it's a real item row, not a
        # divider -- a rare enough edge case not to warrant its own layout.)
        if self.current_swimlane == "epic" and lane_key != VIRTUAL_NONE and lane_key not in seen_item_keys:
            epic_item = with_local_index_fields(
                self.app.jira_dir, {"key": lane_key}, self.app.component_field, dev_status_field=self.app.dev_status_field
            )
            if "summary" in epic_item:
                seen_item_keys.add(lane_key)
                self._add_item_row(table, epic_item, modified_keys, assignee_names, bold=True, indent=False)
                return
        lane_text = swimlane_label(item, self.current_swimlane) or VIRTUAL_NONE
        arrow = "▶" if collapsed else "▼"
        label = f"{arrow} {lane_text}  ({count})" if collapsed else f"{arrow} {lane_text}"
        table.add_row(
            "",
            "",
            "",
            "",
            # Summary, not Key -- an epic-swimlane label is the epic's own
            # summary text (can run 100+ chars), and Key has no fixed width
            # (it auto-sizes to content, and a DataTable column never
            # shrinks back down once widened). Summary already needs to be
            # wide for real issue summaries, so growing it a bit further is
            # far less jarring than blowing out the narrow Key column.
            Text(label, style="bold"),
            "",
            "",
            "",
            "",
            key=f"{LANE_ROW_PREFIX}{lane_key}",
        )

    def _add_item_row(
        self,
        table: DataTable,
        item: dict[str, Any],
        modified_keys: set[str],
        assignee_names: dict[str, str],
        *,
        bold: bool,
        indent: bool,
    ) -> None:
        key_value = display_name(item.get("key"))
        display_key = f"{key_value}*" if key_value in modified_keys else key_value
        glyph, color = type_icon(item.get("type"), nerd_font=self.app.nerd_font_enabled)

        def cell(value: str) -> Any:
            return Text(value, style="bold") if bold else value

        # The epic type color is deliberately confined to the Key cell (plus
        # the icon) rather than the whole row -- coloring every cell reads as
        # noisier without making epics any easier to spot than a bold Key.
        key_style = f"bold {color}" if bold else None
        key_cell = Text(display_key, style=key_style) if key_style else display_key

        priority_glyph, priority_icon_color = priority_icon(
            item.get("priority"), nerd_font=self.app.nerd_font_enabled
        )
        priority_cell = Text(priority_glyph, style=f"bold {priority_icon_color}" if bold else priority_icon_color)

        # The indent lives in the icon column (a fixed-width gutter) rather
        # than as literal spaces on the Key text -- otherwise the Key column
        # auto-sizes to fit the longest indented value, leaving a much
        # bigger gap before State on every un-indented (epic) row.
        icon_text = f"{' ' * INDENT_WIDTH}{glyph}" if indent else glyph
        icon = Text(icon_text, style=f"bold {color}" if bold else color)

        dev_indicator = dev_status_indicator(item.get("devStatus"))
        dev_cell: Any = ""
        if dev_indicator is not None:
            _, dev_label, dev_color = dev_indicator
            dev_cell = Text(f" {dev_label} ", style=f"bold white on {dev_color}")

        table.add_row(
            icon,
            key_cell,
            cell(display_name(item.get("status"))),
            render_pills(pill_values(display_component(item.get("component")))),
            cell(display_name(item.get("summary"))),
            priority_cell,
            cell(assignee_names.get(display_name(item.get("assignee")), display_name(item.get("assignee")))),
            render_pills(pill_values(item.get("fixVersion"))),
            dev_cell,
            key=key_value,
        )
        self._row_keys.append(key_value)

    def _update_subtitle(self, visible_count: int) -> None:
        parts = [f"{visible_count} items"]
        for spec in FILTER_FIELDS:
            if spec.kind != "choice":
                continue
            values = self.field_filters.get(spec.key)
            if values:
                parts.append(f"{spec.key}={','.join(values)}")
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

    def _epic_context(self) -> dict[str, Any] | None:
        # Whatever's under the cursor drives the new-issue defaults: an Epic
        # row is its own context (create a child under it); any other item's
        # context is its own epic, if it has one (create a sibling) -- the
        # "(none)" epic lane and any non-epic swimlane header both resolve to
        # no context via _current_key() returning None, same as an item with
        # no epic set. Works regardless of swimlane mode.
        current_key = self._current_key()
        if current_key is None:
            return None
        item = next((i for i in self.items if display_name(i.get("key")) == current_key), None)
        if item is None:
            return None
        if is_epic_item(item):
            return item
        epic_key = display_name(item.get("epic")).strip()
        if not epic_key:
            return None
        return next((i for i in self.items if display_name(i.get("key")) == epic_key), None)

    def _lane_key_under_cursor(self) -> str | None:
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
        if value.startswith(LANE_ROW_PREFIX):
            return value.removeprefix(LANE_ROW_PREFIX)
        item = next((i for i in self.items if display_name(i.get("key")) == value), None)
        return self._lane_identity(item) if item is not None else None

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
        # Lane keys from the old grouping mode don't mean anything under the
        # new one (a component name could coincidentally collide with an
        # epic key), so stale collapse state would be at best meaningless
        # and at worst misleading.
        self._collapsed_lanes = set()
        self._rebuild_table()

    def action_toggle_lane(self) -> None:
        if self.current_swimlane == "none":
            return
        lane_key = self._lane_key_under_cursor()
        if lane_key is None:
            return
        if lane_key in self._collapsed_lanes:
            self._collapsed_lanes.discard(lane_key)
        else:
            self._collapsed_lanes.add(lane_key)
        self._rebuild_table()
        table = self.query_one(DataTable)
        for candidate in (f"{LANE_ROW_PREFIX}{lane_key}", lane_key):
            try:
                table.move_cursor(row=table.get_row_index(candidate))
                break
            except Exception:
                continue

    def action_toggle_all_lanes(self) -> None:
        if self.current_swimlane == "none":
            return
        lane_keys = {self._lane_identity(item) for item in self._visible_items()}
        if not lane_keys:
            return
        # If every currently-visible lane is already collapsed, expand them
        # all back; otherwise collapse them all. Judged against what's
        # visible right now, not the full item set, so a stale collapsed
        # lane hidden by an unrelated filter doesn't stop "collapse all"
        # from firing, or get silently expanded by "expand all".
        if lane_keys <= self._collapsed_lanes:
            self._collapsed_lanes -= lane_keys
        else:
            self._collapsed_lanes |= lane_keys
        self._rebuild_table()

    def action_save_filter(self) -> None:
        from ...config import ConfigError, save_view_defaults

        if self.app.config_path is None:
            self.notify("no config file location known", severity="error")
            return
        try:
            save_view_defaults(
                self.app.config_path,
                {
                    "project": self.field_filters.get("project", []),
                    "status": self.field_filters.get("status", []),
                    "component": self.field_filters.get("component", []),
                    "fix_version": self.field_filters.get("fixVersion", []),
                    "assignee": self.field_filters.get("assignee", []),
                    "board": self.board,
                    "board_scope": self.board_scope,
                    "filter": self.current_filter,
                    "swimlane": self.current_swimlane if self.current_swimlane != "none" else None,
                    "active": self.active_only,
                },
            )
        except ConfigError as exc:
            self.notify(f"could not save filter: {exc}", severity="error")
            return
        self.notify(f"saved current filter to {self.app.config_path}")

    def action_reload(self) -> None:
        self.items = load_manifest_items(
            self.app.jira_dir, self.app.component_field, dev_status_field=self.app.dev_status_field
        )
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
            refresh_index_item(
                self.app.jira_dir, self.items, key, self.app.component_field, dev_status_field=self.app.dev_status_field
            )
        self._rebuild_table()
        if result is not None:
            self.notify(
                f"push all: pushed={result.pushed} skipped={result.skipped} "
                f"blocked={result.blocked} failed={result.failed}"
            )

    @work
    async def action_filters(self) -> None:
        from .filters import FiltersScreen

        boards = [
            board["name"]
            for board in load_cached_boards(self.app.jira_dir)
            if board.get("name") and board.get("active", True)
        ]
        result = await self.app.push_screen_wait(
            FiltersScreen(
                self.items,
                active_only=self.active_only,
                modified_only=self.modified_only,
                field_filters=self.field_filters,
                pattern=self.current_filter,
                boards=boards,
                board=self.board,
                board_scope=self.board_scope,
            )
        )
        self.active_only = result.active_only
        self.modified_only = result.modified_only
        self.field_filters = result.field_filters
        self.current_filter = result.pattern
        self.board = result.board
        self.board_scope = result.board_scope
        self._rebuild_table()

    @work
    async def action_new_issue(self) -> None:
        if not self.app.can_push():
            self.notify("cannot create: missing Jira API configuration", severity="warning")
            return
        project = self.app.project
        if not project:
            self.notify("cannot create: no project configured", severity="warning")
            return
        if self.app.is_project_read_only(project):
            self.notify(f"cannot create: project {project} is read-only", severity="warning")
            return
        try:
            client = self.app.get_api_client()
            # Cached per project for the rest of the session, and offloaded
            # to a thread -- issue_createmeta is a synchronous (blocking)
            # HTTP call, so running it inline here would freeze the whole UI
            # for the round trip on every single "new issue" press, not just
            # delay this one screen.
            type_fields = self.app.issue_type_fields_cache.get(project)
            if type_fields is None:
                type_fields = await asyncio.to_thread(fetch_issue_type_fields, client, str(project))
                self.app.issue_type_fields_cache[project] = type_fields
        except (MetadataError, IssueError) as exc:
            self.notify(f"cannot create: {exc}", severity="error")
            return
        if not type_fields:
            self.notify(f"no creatable issue types found for project {project}", severity="warning")
            return

        default_reporter = ""
        default_reporter_account_id: str | None = None
        if any("reporter" in fields for fields in type_fields.values()):
            current_user = self.app.current_user
            if current_user is None:
                try:
                    current_user = await asyncio.to_thread(fetch_current_user, client)
                    self.app.current_user = current_user
                except IssueError:
                    current_user = None
            if current_user is not None:
                default_reporter_account_id = str(current_user.get("accountId") or "") or None
                default_reporter = str(
                    current_user.get("displayName") or current_user.get("emailAddress") or default_reporter_account_id or ""
                )

        from .issue_create import IssueCreateScreen

        epic_item = self._epic_context()
        context_label = f"New issue under {display_name(epic_item.get('key'))}" if epic_item else ""
        key = await self.app.push_screen_wait(
            IssueCreateScreen(
                jira_dir=self.app.jira_dir,
                project=str(project),
                component_field=self.app.component_field or "components",
                type_fields=type_fields,
                client=client,
                epic_item=epic_item,
                default_reporter=default_reporter,
                default_reporter_account_id=default_reporter_account_id,
                context_label=context_label,
            )
        )
        if key is None:
            return

        self.items = load_manifest_items(
            self.app.jira_dir, self.app.component_field, dev_status_field=self.app.dev_status_field
        )
        self._rebuild_table()
        self._select_key(key)
        self.notify(f"created {key}")

    async def _open_dev_status_link(self, row_key: RowKey) -> None:
        # The Dev column's cached indicator (from the already-synced
        # "Development" field mirror) has no real URL -- only clicking
        # triggers the live per-issue fetch_dev_status call to resolve one,
        # exactly like Detail's own dev-status panel. One click = at most
        # one issue's worth of API calls, never a batch fetch across the list.
        key = row_key.value
        if key is None or key.startswith(LANE_ROW_PREFIX):
            return
        item = next((i for i in self.items if display_name(i.get("key")) == key), None)
        if item is None or item.get("devStatus") is None:
            return
        if not self.app.can_push():
            self.notify("cannot open: missing Jira API configuration", severity="warning")
            return
        try:
            client = self.app.get_api_client()
        except MetadataError:
            return
        status = self.app.dev_status_cache.get(key)
        if status is None:
            status = await asyncio.to_thread(fetch_dev_status, client, str(item.get("issueId")))
            self.app.dev_status_cache[key] = status
        links = [(pr.name, pr.url) for pr in status.pull_requests] + [
            (branch.name, branch.url) for branch in status.branches
        ]
        if not links:
            self.notify("no branch/PR details available")
            return
        if len(links) == 1:
            webbrowser.open(links[0][1])
            return
        choice = await self.app.push_screen_wait(OptionPickerScreen("Open:", [name for name, _ in links]))
        if choice:
            webbrowser.open(dict(links)[choice])

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

        self.app.push_screen(MetaScreen(standalone=False, items=self.items), callback=self._on_meta_closed)

    def _on_meta_closed(self, _result: None) -> None:
        # Most Meta actions (versions, boards) don't touch item-level data,
        # but Labels' bulk rename/delete does -- drain and refresh the same
        # way returning from Detail already does, rather than special-casing
        # just that one path.
        for key in self.app.drain_changed_keys():
            refresh_index_item(
                self.app.jira_dir, self.items, key, self.app.component_field, dev_status_field=self.app.dev_status_field
            )
        self._rebuild_table()

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
            refresh_index_item(
                self.app.jira_dir, self.items, key, self.app.component_field, dev_status_field=self.app.dev_status_field
            )
        self._rebuild_table()
