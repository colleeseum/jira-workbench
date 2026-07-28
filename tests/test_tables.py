from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from jira_workbench.tui.widgets.tables import ClickableRowDataTable


class FakeClickEvent:
    """Minimal stand-in for textual.events.Click -- exercises _on_click's own
    logic directly against a real, mounted DataTable's internal state,
    without needing to reproduce Textual's pixel-to-cell click pipeline."""

    def __init__(self, row: int, column: int) -> None:
        self.style = SimpleNamespace(meta={"row": row, "column": column})
        self.prevented = False
        self.stopped = False

    def prevent_default(self) -> None:
        self.prevented = True

    def stop(self) -> None:
        self.stopped = True


class LinkTableApp(App):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self._kwargs = kwargs
        self.selected_rows: list[Any] = []
        self.linked_rows: list[Any] = []

    def compose(self) -> ComposeResult:
        yield ClickableRowDataTable(id="table", cursor_type="row", **self._kwargs)

    async def _on_link(self, row_key: Any) -> None:
        self.linked_rows.append(row_key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.selected_rows.append(event.row_key.value)


async def _table_with_rows(app: App) -> DataTable:
    table = app.query_one(DataTable)
    table.add_column("Key", key="Key")
    table.add_column("Dev", key="Dev")
    table.add_row("SAT-1", "OPEN", key="SAT-1")
    table.add_row("SAT-2", "", key="SAT-2")
    return table


@pytest.mark.asyncio
async def test_click_on_link_column_calls_hook_and_skips_row_select() -> None:
    app = LinkTableApp(link_column="Dev", on_link_click=None)
    app._kwargs["on_link_click"] = app._on_link

    async with app.run_test() as pilot:
        table = await _table_with_rows(app)
        event = FakeClickEvent(row=0, column=1)  # "Dev" column, first row

        await table._on_click(event)
        await pilot.pause()

        assert app.linked_rows == ["SAT-1"]
        assert app.selected_rows == []
        assert event.stopped is True


@pytest.mark.asyncio
async def test_click_on_other_column_still_selects_the_row() -> None:
    app = LinkTableApp(link_column="Dev", on_link_click=None)
    app._kwargs["on_link_click"] = app._on_link

    async with app.run_test() as pilot:
        table = await _table_with_rows(app)
        event = FakeClickEvent(row=1, column=0)  # "Key" column, second row

        await table._on_click(event)
        await pilot.pause()

        assert app.linked_rows == []
        assert app.selected_rows == ["SAT-2"]


@pytest.mark.asyncio
async def test_without_link_column_behaves_exactly_as_before() -> None:
    app = LinkTableApp()

    async with app.run_test() as pilot:
        table = await _table_with_rows(app)
        event = FakeClickEvent(row=0, column=1)  # what would be "Dev" if configured

        await table._on_click(event)
        await pilot.pause()

        assert app.linked_rows == []
        assert app.selected_rows == ["SAT-1"]
