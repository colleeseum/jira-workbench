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
    "  click Dev cell  Open its branch/PR (fetches the real URL on click;",
    "                  a picker appears if more than one is linked)",
    "  v               View a work item by key",
    "  V               Open selected item's parent",
    "  g               Go to matching row",
    "  /               Search rows",
    "  n / N           Repeat search forward/backward",
    "  a               Toggle active-only filter",
    "  m               Toggle modified-only filter",
    "  f               Open Filters (component, fix version, assignee, text)",
    "  c               Create a new Jira issue (fields default from the epic",
    "                  under the cursor)",
    "  C               Clone the item under the cursor (opens the same form,",
    "                  prefilled from it -- summary gets a \" (clone)\" suffix)",
    "  s               Save current filter as the default",
    "  S               Cycle swimlane grouping",
    "  z               Collapse/expand the lane under the cursor",
    "  Z               Collapse/expand all lanes",
    "  R               Reload local list",
    "  P               Push all local shadow changes (review list first)",
    "  M               Open Jira metadata (fix versions, components)",
    "  h               Show or hide this help",
    "  q or Esc        Quit",
    "",
    "Detail",
    "  A Development panel (linked branches/PRs) loads automatically when",
    "  Jira API access is configured -- read-only, no extra setup needed. The",
    "  same live check also loads Due date and any labels-type custom field",
    "  (e.g. a \"Customers\" field) this issue's own edit screen in Jira",
    "  supports, once it arrives -- both stay hidden if it fails, same as",
    "  the Development panel, and native Labels editing is unaffected either",
    "  way. Due date takes YYYY-MM-DD (\"(none)\" clears it, blank cancels);",
    "  labels-type fields open the same checklist picker as Labels. Jira",
    "  issue links (blocks, is blocked by, relates to, ...) show as one row",
    "  per phrase, read-only for now.",
    "  Enter / click   Edit highlighted field; Description opens a scrollable",
    "                  viewer with an Edit button (x does the same); Comments",
    "                  opens the per-comment screen below (x does the same)",
    "  j/k, arrows     Move between fields",
    "  s               Show local shadow view",
    "  o               Show original synced Jira issue",
    "  d               Show local shadow diff",
    "  c               Add a local shadow comment",
    "  C               Clone this item (opens the same form as Index's Clone)",
    "  r               Revert this item's local shadow",
    "  p               Push this item's shadow to Jira",
    "  R               Refresh this item from Jira right now (an immediate,",
    "                  single-issue jira-wb sync --force; local shadow kept)",
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
    "  Enter / click   Open fix versions, components, boards, or labels",
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
    "Labels",
    "  Lists every label used across locally synced issues (shadow-merged,",
    "  so a pending local edit shows immediately) with its issue count.",
    "  Jira has no API for labels as an independent entity, unlike Fix",
    "  Versions/Components -- rename/delete here find every locally synced",
    "  issue with the label, edit each one's shadow, and push them, the",
    "  same thing Jira's own bulk-edit issue navigator does under the hood.",
    "  e               Rename the selected label on every issue that has it",
    "                  and push them (asks to confirm first, with the count)",
    "  d               Delete the selected label from every issue that has",
    "                  it and push them (asks to confirm first)",
    "  n               New (Jira has no standalone label registry -- this",
    "                  just explains that; apply a label to an issue instead)",
    "  R               Reload",
    "  q or Esc        Back",
    "",
    "New issue (opened with c from the index, or C to clone from Index/Detail)",
    "  Enter / click   Edit the selected field. Summary, Description, and",
    "                  Type have no default and are required; Status always",
    "                  reads \"To Do\" and isn't editable (Jira has no create-",
    "                  time status field); Component, Version, Priority,",
    "                  Labels, Assignee, Reporter, Parent narrow to what the",
    "                  selected Type's create screen supports, live. Labels",
    "                  opens a checklist of labels already used locally --",
    "                  toggle with Space, or type a genuinely new one into",
    "                  its own input and press Enter to add it",
    "  p               Create the issue (asks to confirm first)",
    "  q or Esc        Cancel, back to Index",
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
