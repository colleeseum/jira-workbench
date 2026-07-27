from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

TUI_HELP_LINES = [
    "Jira Workbench Help",
    "",
    "Mouse: click a row to open/select it, wheel to scroll any list or table.",
    "",
    "Index",
    "  j/k, arrows     Move selection",
    "  Enter / click   Open selected work item",
    "  click column    Sort by that column; click again to reverse",
    "  v               View a work item by key",
    "  V               Open selected item's parent",
    "  g               Go to matching row",
    "  /               Search rows",
    "  n / N           Repeat search forward/backward",
    "  a               Toggle active-only filter",
    "  m               Toggle modified-only filter",
    "  f               Open Filters (component, fix version, assignee, text)",
    "  S               Cycle swimlane grouping",
    "  R               Reload local list",
    "  P               Push all local shadow changes (review list first)",
    "  M               Open Jira metadata (fix versions, components)",
    "  h               Show or hide this help",
    "  q or Esc        Quit",
    "",
    "Detail",
    "  Enter / click   Edit highlighted field; Description opens a scrollable",
    "                  viewer with an Edit button (x does the same); Comments",
    "                  opens the per-comment screen below (x does the same)",
    "  j/k, arrows     Move between fields",
    "  s               Show local shadow view",
    "  o               Show original synced Jira issue",
    "  d               Show local shadow diff",
    "  c               Add a local shadow comment",
    "  r               Revert this item's local shadow",
    "  p               Push this item's shadow to Jira",
    "  O               Toggle Other fields",
    "  v               View a work item by key",
    "  V               Open this item's parent",
    "  h               Show or hide this help",
    "  Esc             Back to index",
    "  q               Quit",
    "",
    "Comments (opened from the Comments row in Detail)",
    "  Enter / click   View the selected comment; its Edit button switches",
    "                  the same screen to Save, same as Description",
    "  n               Add a new comment",
    "  d               Delete the selected comment, or undo a pending delete",
    "  R               Reload",
    "  q or Esc        Back to Detail",
    "",
    "Metadata (opened with M from the index, or `jira-wb meta`)",
    "  Enter / click   Open fix versions, or show component metadata",
    "  n               Add a version or component",
    "  q or Esc        Back",
    "",
    "Fix versions",
    "  /               Regex filter, \\ clears it",
    "  e               Rename selected version",
    "  r               Release selected version (optional release date)",
    "  a               Archive selected version",
    "  d               Delete selected version (optional move-to target, same screen)",
    "  n               Add a version",
    "  R               Reload",
    "  q or Esc        Back",
    "",
    "Filters (opened with f from the index)",
    "  Enter / click   Edit the selected filter",
    "  d               Clear the selected filter",
    "  c               Clear all filters",
    "  q or Esc        Back to Index (applies the filters)",
    "",
    "Push review (opened with P from the index)",
    "  Enter / click   Open a full side-by-side diff for the selected item",
    "  p               Push all listed items to Jira",
    "  q or Esc        Cancel, back to Index",
    "",
    "Diff viewer (opened from Push review)",
    "  j/k, arrows     Scroll",
    "  q or Esc        Back",
    "",
    "Notes",
    "  Active hides statuses: Close, Closed, Done, Resolved",
    "  In the Filters screen, (any) means no filter on that dimension;",
    "  (none) is a real bucket for items where that field is empty.",
]


class HelpScreen(ModalScreen[None]):
    """Scrollable keybinding reference."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > VerticalScroll {
        width: 90%;
        height: 90%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("h", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("\n".join(TUI_HELP_LINES))

    def action_close(self) -> None:
        self.dismiss(None)
