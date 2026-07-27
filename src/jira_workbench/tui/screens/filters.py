from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from ...view import FILTER_ANY, VIRTUAL_NONE, distinct_field_values
from ..widgets.prompts import OptionPickerScreen, TextPromptScreen
from ..widgets.tables import ClickableRowDataTable

PATTERN_KEY = "pattern"
TOGGLE_KEYS = {"active", "modified"}


@dataclass(frozen=True)
class FilterField:
    key: str
    label: str
    kind: str  # "toggle" | "choice" | "text"
    empty_bucket: str = VIRTUAL_NONE


# The single place to touch when adding a future filterable dimension.
FILTER_FIELDS: list[FilterField] = [
    FilterField(key="active", label="Active only", kind="toggle"),
    FilterField(key="modified", label="Modified only", kind="toggle"),
    FilterField(key="component", label="Component", kind="choice", empty_bucket="_unassigned"),
    FilterField(key="fixVersion", label="Fix version", kind="choice"),
    FilterField(key="assignee", label="Assignee", kind="choice"),
    FilterField(key=PATTERN_KEY, label="Text filter", kind="text"),
]


@dataclass(frozen=True)
class FiltersResult:
    active_only: bool
    modified_only: bool
    field_filters: dict[str, str] = field(default_factory=dict)
    pattern: str | None = None


class FiltersScreen(Screen[FiltersResult]):
    """Consolidated filter browser: one row per filterable dimension.

    Enter opens an editor for the selected row (a toggle flips in place; a
    choice/text row opens a picker/prompt). Edits apply to a working copy
    immediately -- closing (q/Esc) always returns the current state, same
    "no buffered cancel" convention as MetaScreen/VersionsScreen.
    """

    BINDINGS = [
        Binding("d", "clear_selected", "Clear"),
        Binding("c", "clear_all", "Clear all"),
        Binding("q", "close", "Back"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        active_only: bool,
        modified_only: bool,
        field_filters: dict[str, str],
        pattern: str | None,
    ) -> None:
        super().__init__()
        self._items = items
        self.active_only = active_only
        self.modified_only = modified_only
        self.field_filters: dict[str, str] = dict(field_filters)
        self.pattern = pattern

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClickableRowDataTable(id="filters-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Filter", key="filter", width=16)
        table.add_column("Value", key="value")
        self._rebuild_rows()

    def _row_value(self, spec: FilterField) -> str:
        if spec.kind == "toggle":
            value = self.active_only if spec.key == "active" else self.modified_only
            return "Yes" if value else "No"
        if spec.key == PATTERN_KEY:
            return self.pattern or FILTER_ANY
        return self.field_filters.get(spec.key) or FILTER_ANY

    def _rebuild_rows(self) -> None:
        table = self.query_one(DataTable)
        previous = self._current_key()
        table.clear()
        for spec in FILTER_FIELDS:
            table.add_row(spec.label, self._row_value(spec), key=spec.key)
        valid_keys = {spec.key for spec in FILTER_FIELDS}
        if previous in valid_keys:
            table.move_cursor(row=table.get_row_index(previous))
        active_count = (
            len(self.field_filters)
            + (1 if self.pattern else 0)
            + (0 if self.active_only else 1)
            + (1 if self.modified_only else 0)
        )
        self.sub_title = f"{active_count} active" if active_count else "no filters"

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
        return next(spec for spec in FILTER_FIELDS if spec.key == key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        self.action_edit(key)

    @work
    async def action_edit(self, key: str) -> None:
        spec = self._spec_for(key)
        if spec.kind == "toggle":
            self._toggle(key)
            return
        if spec.kind == "text":
            value = await self.app.push_screen_wait(TextPromptScreen("Text filter:", initial=self.pattern or ""))
            self.pattern = value
            self._rebuild_rows()
            return
        counts = distinct_field_values(self._items, spec.key, empty_bucket=spec.empty_bucket)
        current_value = self.field_filters.get(spec.key)
        options = [FILTER_ANY]
        label_to_value: dict[str, str | None] = {FILTER_ANY: None}
        current_label = FILTER_ANY
        for value, count in counts:
            label = f"{value} ({count})"
            options.append(label)
            label_to_value[label] = value
            if value == current_value:
                current_label = label
        choice = await self.app.push_screen_wait(
            OptionPickerScreen(f"{spec.label} filter:", options, current=current_label)
        )
        if choice is None:
            return
        value = label_to_value.get(choice)
        if value is None:
            self.field_filters.pop(spec.key, None)
        else:
            self.field_filters[spec.key] = value
        self._rebuild_rows()

    def _toggle(self, key: str) -> None:
        if key == "active":
            self.active_only = not self.active_only
        else:
            self.modified_only = not self.modified_only
        self._rebuild_rows()

    def action_clear_selected(self) -> None:
        key = self._current_key()
        if key is None:
            return
        spec = self._spec_for(key)
        if spec.key == "active":
            self.active_only = True
        elif spec.key == "modified":
            self.modified_only = False
        elif spec.key == PATTERN_KEY:
            self.pattern = None
        else:
            self.field_filters.pop(spec.key, None)
        self._rebuild_rows()

    def action_clear_all(self) -> None:
        self.active_only = True
        self.modified_only = False
        self.field_filters = {}
        self.pattern = None
        self._rebuild_rows()

    def action_close(self) -> None:
        self.dismiss(
            FiltersResult(
                active_only=self.active_only,
                modified_only=self.modified_only,
                field_filters=dict(self.field_filters),
                pattern=self.pattern,
            )
        )
