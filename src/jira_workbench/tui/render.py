from __future__ import annotations

from typing import Any, Callable

from rich.text import Text

from ..view import pill_color, priority_icon


def render_pills(values: list[str]) -> Any:
    """One colored chip per value, space-separated. Empty list -> "" so a
    pill-able but unset field renders blank, same as any other empty cell."""
    if not values:
        return ""
    result = Text()
    for index, value in enumerate(values):
        if index:
            result.append(" ")
        result.append(f" {value} ", style=f"bold white on {pill_color(value)}")
    return result


def render_icon_label(glyph: str, color: str, label: str) -> Text:
    if not glyph:
        return Text(label)
    text = Text()
    text.append(f"{glyph} ", style=color)
    text.append(label)
    return text


def priority_option_render(*, nerd_font: bool) -> Callable[[str], Text]:
    def render(option: str) -> Text:
        glyph, color = priority_icon(option, nerd_font=nerd_font)
        return render_icon_label(glyph, color, option)

    return render
