from __future__ import annotations

import pytest
from rich.text import Text
from textual import work
from textual.app import App
from textual.widgets import Input, OptionList

from jira_workbench.tui.render import priority_option_render, render_icon_label, render_pills
from jira_workbench.tui.widgets.prompts import OptionPickerScreen
from jira_workbench.view import pill_color


def test_render_pills_empty_list_is_blank() -> None:
    assert render_pills([]) == ""


def test_render_pills_one_chip_per_value() -> None:
    result = render_pills(["urgent", "flaky"])
    assert isinstance(result, Text)
    assert result.plain == " urgent   flaky "
    spans = {(span.style, result.plain[span.start : span.end]) for span in result.spans}
    assert (f"bold white on {pill_color('urgent')}", " urgent ") in spans
    assert (f"bold white on {pill_color('flaky')}", " flaky ") in spans


def test_render_icon_label_prefixes_glyph_when_present() -> None:
    text = render_icon_label("↑", "#f59e0b", "High")
    assert text.plain == "↑ High"
    assert text.spans[0].style == "#f59e0b"


def test_render_icon_label_returns_bare_text_when_glyph_is_empty() -> None:
    text = render_icon_label("", "dim", "Medium")
    assert text.plain == "Medium"
    assert text.spans == []


def test_priority_option_render_wires_priority_icon_through() -> None:
    render = priority_option_render(nerd_font=False)
    text = render("High")
    assert text.plain == "↑ High"

    render_nerd = priority_option_render(nerd_font=True)
    text_nerd = render_nerd("High")
    assert text_nerd.plain == "\U000f0143 High"


@pytest.mark.asyncio
async def test_option_picker_screen_with_render_shows_icon_but_returns_plain_option() -> None:
    class HostApp(App):
        result: str | None = None

        @work
        async def on_mount(self) -> None:
            self.result = await self.push_screen_wait(
                OptionPickerScreen(
                    "Priority:",
                    ["High", "Low"],
                    render=priority_option_render(nerd_font=False),
                )
            )

    app = HostApp()
    async with app.run_test() as pilot:
        options = app.screen.query_one(OptionList)
        assert str(options.get_option_at_index(0).prompt) == "↑ High"

        options.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()

        assert app.result == "High"


@pytest.mark.asyncio
async def test_option_picker_screen_without_render_is_unaffected() -> None:
    class HostApp(App):
        result: str | None = None

        @work
        async def on_mount(self) -> None:
            self.result = await self.push_screen_wait(OptionPickerScreen("Component:", ["backend", "frontend"]))

    app = HostApp()
    async with app.run_test() as pilot:
        filter_input = app.screen.query_one("#picker-filter", Input)
        filter_input.value = "front"
        await pilot.press("enter")
        await pilot.pause()

        assert app.result == "frontend"
