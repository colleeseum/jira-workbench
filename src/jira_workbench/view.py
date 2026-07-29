from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import textwrap
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

from .jql import evaluate_predicate
from .metadata import (
    load_all_boards_with_settings,
    load_all_components,
    load_all_versions,
    load_field_names,
    version_name,
)
from .shadow import load_shadow, render_diff
from .sync import (
    FALLBACK_DONE_STATUS_NAMES,
    find_existing_issue,
    issue_key_sort_key,
    issue_path_sort_key,
    issue_project_key,
    read_json,
    status_category_key,
)


class ViewError(RuntimeError):
    pass
DEFAULT_RESOLUTIONS = ["Done", "Won't Do", "Duplicate", "Cannot Reproduce"]
VIRTUAL_NONE = "(none)"
FILTER_ANY = "(any)"  # UI-only "no filter on this dimension" sentinel; never stored
SWIMLANE_MODES = ("none", "epic", "version", "component", "board")
REPORT_FIELD_LABELS = {
    "assignee": "assignee",
    "fixVersions": "version",
    "parent": "parent",
    "priority": "priority",
    "resolution": "resolution",
    "status": "status",
    "type": "type",
}


def extract_field_names(edit_fields: dict[str, dict[str, Any]] | None) -> dict[str, str]:
    """Pull {field_id: display_name} out of an issue_editmeta response, e.g.
    "customfield_10082" -> "Customers SAT" -- fed into remember_field_names
    so the locally-cached name survives for later, offline report rendering."""
    if not edit_fields:
        return {}
    return {
        field_id: str(meta["name"])
        for field_id, meta in edit_fields.items()
        if isinstance(meta, dict) and isinstance(meta.get("name"), str)
    }


def report_field_label(jira_dir: Path, field: str) -> str:
    """Human-friendly label for a shadow-changed field name in a report --
    the fixed system-field labels first, then whatever custom field name has
    been locally cached (see remember_field_names), falling back to the raw
    field id (e.g. "customfield_10082") only if neither is known."""
    if field in REPORT_FIELD_LABELS:
        return REPORT_FIELD_LABELS[field]
    return load_field_names(jira_dir).get(field, field)


def version_id_to_name_map(jira_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for version in load_all_versions(jira_dir):
        version_id = version.get("id")
        name = version_name(version)
        if isinstance(version_id, str) and name:
            result[version_id] = name
    return result


# Jira Cloud mirrors its "Development" panel data (branches/PRs) into a
# regular custom field on every issue -- this is what a normal sync already
# fetches (fields="*all"), so the list view can show a Git/PR indicator for
# every synced issue with zero extra API calls. The default id is common but
# not guaranteed to be the same on every Jira instance -- see
# [view].dev_status_field.
DEV_STATUS_FIELD_DEFAULT = "customfield_10000"

_DEV_STATUS_JSON_RE = re.compile(r"json=(\{.*\})\}\s*$")


def parse_dev_status_summary(raw: Any) -> dict[str, Any] | None:
    """Parse the "Development" field mirror's semi-structured string value
    into its `summary` dict (one of pullrequest/branch/repository), or None
    if empty/unparseable.

    The field's own value is not real JSON -- it's a Groovy/Java-style
    toString() of an internal object (unquoted `key=value` pairs), except
    for a `json=` sub-value which IS valid JSON once its one extra trailing
    "}" (the outer wrapper's own close) is stripped. Undocumented and
    unofficial, like devstatus.py's fetch_dev_status -- best-effort and
    silent on any failure, never a user-facing error.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    match = _DEV_STATUS_JSON_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    summary = as_dict(parsed).get("cachedValue")
    summary = as_dict(summary).get("summary") if summary else None
    return summary if isinstance(summary, dict) and summary else None


# Darker/more saturated than a bare icon color needs -- same reasoning as
# PILL_PALETTE: white bold text sitting on a filled background needs more
# contrast margin than a colored glyph on the terminal's own background.
# ("500"-level shades here were confirmed too light to read, especially the
# grey/green ones -- these are "600"/"700"-level instead.)
DEV_STATUS_PR_COLORS = {
    "OPEN": "#15803d",
    "DRAFT": "#334155",
    "MERGED": "#7e22ce",
    "DECLINED": "#b91c1c",
}
DEV_STATUS_BRANCH_COLOR = "#1d4ed8"
DEV_STATUS_REPOSITORY_COLOR = "#334155"


def dev_status_indicator(summary: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """(kind, label, hex color) for a list-view Git/PR cell, or None to
    render blank. kind is "pr" | "branch" | "repository" -- richest signal
    (an actual PR, colored by its state) to weakest (just a repo
    reference, no specific branch)."""
    if not isinstance(summary, dict):
        return None
    if "pullrequest" in summary:
        overall = as_dict(summary["pullrequest"]).get("overall")
        state = str(as_dict(overall).get("state") or "OPEN").upper()
        return ("pr", state.title(), DEV_STATUS_PR_COLORS.get(state, DEV_STATUS_REPOSITORY_COLOR))
    if "branch" in summary:
        return ("branch", "Branch", DEV_STATUS_BRANCH_COLOR)
    if "repository" in summary:
        return ("repository", "Linked", DEV_STATUS_REPOSITORY_COLOR)
    return None


def resolve_fix_version_names(
    jira_dir: Path, value: Any, *, id_to_name: dict[str, str] | None = None
) -> list[str]:
    """Fix version display names, resolved by id against the current
    versions cache first. A version can be renamed in Jira after an issue
    was last synced -- Meta's own versions.json is kept current on rename
    (see VersionsScreen.action_rename), but the issue's own locally synced
    fixVersions still has whatever name was embedded back when it was last
    fetched. Falls back to that embedded name only if the id isn't in the
    cache (e.g. offline, or the versions cache has never been refreshed).

    `id_to_name` lets a caller looping over many issues pass in one
    precomputed `version_id_to_name_map(jira_dir)` instead of paying for a
    full versions-cache rebuild (a `load_all_versions` disk scan + sort)
    on every single call -- rebuilding it here by default keeps this cheap
    for the common single-item callers (Detail, shadow reports)."""
    if id_to_name is None:
        id_to_name = version_id_to_name_map(jira_dir)
    names: list[str] = []
    for entry in as_list(value):
        version_id = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(version_id, str) and version_id in id_to_name:
            names.append(id_to_name[version_id])
            continue
        name = display_name(entry)
        if name:
            names.append(name)
    return names


BASE_EDITABLE_FIELDS = [
    ("Summary", "summary"),
    ("Description", "description"),
    ("Type", "type"),
    ("Status", "status"),
    ("Priority", "priority"),
    ("Fix versions", "fixVersions"),
    ("Components", "components"),
    ("Labels", "labels"),
    ("Assignee", "assignee"),
    ("Parent", "parent"),
]


def is_active_item(item: dict[str, Any]) -> bool:
    category = item.get("statusCategory")
    if isinstance(category, str):
        return category != "done"
    return display_name(item.get("status")).strip().lower() not in FALLBACK_DONE_STATUS_NAMES


def field_value(item: dict[str, Any], field: str) -> str:
    """Generic display-value extraction for an equality-filterable field.

    Works for "component", "fixVersion", "assignee", and any future field
    whose name matches an item dict key -- no per-field special-casing.
    """
    return display_name(item.get(field)).strip()


def matches_field(item: dict[str, Any], field: str, values: list[str] | None) -> bool:
    """True if `item`'s own value for `field` is any one of `values` (an
    unset/empty list always matches, same as before this became
    multi-select) -- mirrors the list-membership check matches_board
    already uses for boards, just against a single-valued item field
    instead of a list-valued one."""
    if not values:
        return True
    actual = field_value(item, field)
    normalized = {value.strip().lower() for value in values}
    if VIRTUAL_NONE.lower() in normalized and not actual:
        return True
    return actual.lower() in normalized


def matches_filter(item: dict[str, Any], pattern: str | None) -> bool:
    if not pattern:
        return True
    needle = pattern.strip().lower()
    haystack = " ".join(
        display_name(item.get(field))
        for field in ("key", "summary", "status", "type", "component")
    ).lower()
    return needle in haystack


def item_fix_version(item: dict[str, Any]) -> str:
    return field_value(item, "fixVersion")


def matches_board(item: dict[str, Any], board: str | None, board_scope: str | None) -> bool:
    """Board membership is list-valued (an item can be on several boards), so
    it can't reuse matches_field's single-value equality contract."""
    if not board:
        return True
    if board not in as_list(item.get("boards")):
        return False
    if board_scope and board_scope != "any":
        return item.get("boardStatus", {}).get(board) == board_scope
    return True


def is_stale_done(item: dict[str, Any], *, max_age_days: int) -> bool:
    """True if this item is Done/Closed and has been so for more than
    max_age_days -- a local, explicit stand-in for the "hide old completed
    issues" behavior Jira's own Kanban board UI applies (that setting isn't
    exposed by any API, so this is our own equivalent, not a replica)."""
    if is_active_item(item):
        return False
    changed = item.get("statusCategoryChangeDate")
    if not isinstance(changed, str) or not changed:
        return False
    try:
        changed_at = datetime.fromisoformat(changed.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(changed_at.tzinfo)
    return (now - changed_at).days > max_age_days


def filter_items(
    items: list[dict[str, Any]],
    *,
    field_filters: dict[str, list[str]] | None = None,
    pattern: str | None = None,
    active: bool = True,
    modified_keys: set[str] | None = None,
    modified_only: bool = False,
    board: str | None = None,
    board_scope: str | None = None,
    max_done_age_days: int | None = None,
) -> list[dict[str, Any]]:
    field_filters = field_filters or {}
    modified_keys = modified_keys or set()
    # Modified combines with the other filters like any of them: picking a
    # specific component while Modified is on narrows to that component's
    # modified items; leaving component at "(any)" (an empty field_filters
    # entry) shows every modified item, since matches_field is a no-op for
    # an unset filter.
    return [
        item
        for item in items
        if all(matches_field(item, field, value) for field, value in field_filters.items())
        and matches_filter(item, pattern)
        and (not active or is_active_item(item))
        and (not modified_only or display_name(item.get("key")) in modified_keys)
        and matches_board(item, board, board_scope)
        and (max_done_age_days is None or not is_stale_done(item, max_age_days=max_done_age_days))
    ]


def find_item_index(items: list[dict[str, Any]], query: str) -> int | None:
    normalized = query.strip().lower()
    if not normalized:
        return None
    for index, item in enumerate(items):
        if display_name(item.get("key")).lower() == normalized:
            return index
    for index, item in enumerate(items):
        if matches_filter(item, normalized):
            return index
    return None


def find_next_item_index(
    items: list[dict[str, Any]],
    query: str,
    current: int,
    *,
    direction: int = 1,
) -> int | None:
    normalized = query.strip()
    if not normalized or not items:
        return None
    step = 1 if direction >= 0 else -1
    count = len(items)
    for offset in range(1, count + 1):
        index = (current + step * offset) % count
        if matches_filter(items[index], normalized):
            return index
    return None


def distinct_field_values(
    items: list[dict[str, Any]], field: str, *, empty_bucket: str = VIRTUAL_NONE
) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for item in items:
        value = field_value(item, field) or empty_bucket
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda row: row[0].lower())


def label_counts(items: list[dict[str, Any]]) -> list[tuple[str, int]]:
    # Labels are multi-valued, unlike component/fixVersion/etc., so this
    # can't reuse distinct_field_values (one bucket per item) -- each item
    # can contribute to several labels' counts at once. `items` is expected
    # to already be shadow-merged (e.g. from load_manifest_items), so this
    # reflects local edits immediately, the same as every other local view.
    counts: dict[str, int] = {}
    for item in items:
        for label in item.get("labels") or []:
            if isinstance(label, str) and label:
                counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda row: row[0].lower())


def component_counts(items: list[dict[str, Any]]) -> list[tuple[str, int]]:
    # "_unassigned" is a real directory/component name from sync (see
    # sync.py), not just a display artifact -- preserved as the default
    # empty-bucket label here instead of the generic VIRTUAL_NONE.
    return distinct_field_values(items, "component", empty_bucket="_unassigned")


def format_components(jira_dir: Path) -> str:
    rows = component_counts(load_manifest_items(jira_dir))
    if not rows:
        return "no components found\n"
    width = max(len(component) for component, _ in rows)
    return "\n".join(f"{component:<{width}}  {count}" for component, count in rows) + "\n"


def cycle_component(items: list[dict[str, Any]], current: str | None, direction: int) -> str | None:
    components = [component for component, _ in component_counts(items)]
    if not components:
        return None
    choices: list[str | None] = [None, *components]
    try:
        index = choices.index(current)
    except ValueError:
        index = 0
    return choices[(index + direction) % len(choices)]


def cycle_fix_version(items: list[dict[str, Any]], current: str | None, direction: int) -> str | None:
    versions = sorted({item_fix_version(item) or VIRTUAL_NONE for item in items}, key=str.lower)
    if not versions:
        return None
    choices: list[str | None] = [None, *versions]
    try:
        index = choices.index(current)
    except ValueError:
        index = 0
    return choices[(index + direction) % len(choices)]


def normalize_swimlane(swimlane: str | None) -> str:
    value = display_name(swimlane).strip().lower()
    return value if value in SWIMLANE_MODES else "none"


def cycle_swimlane(current: str | None) -> str:
    current = normalize_swimlane(current)
    index = SWIMLANE_MODES.index(current)
    return SWIMLANE_MODES[(index + 1) % len(SWIMLANE_MODES)]


def virtual_none(value: str) -> str:
    return value.strip() if value.strip() and value.strip() != "_unassigned" else VIRTUAL_NONE


def swimlane_label(item: dict[str, Any], swimlane: str | None) -> str | None:
    mode = normalize_swimlane(swimlane)
    if mode == "none":
        return None
    if mode == "epic":
        if is_epic_item(item):
            epic_key = display_name(item.get("key")).strip()
            epic_summary = display_name(item.get("summary")).strip()
            return f"{epic_key} {epic_summary}".rstrip() if epic_key else VIRTUAL_NONE
        parent_key = display_name(item.get("epic")).strip()
        if not parent_key:
            return VIRTUAL_NONE
        parent_summary = display_name(item.get("epicSummary")).strip()
        return f"{parent_key} {parent_summary}".rstrip()
    if mode == "version":
        return virtual_none(item_fix_version(item))
    if mode == "component":
        return virtual_none(display_name(item.get("component")))
    if mode == "board":
        # An item can genuinely belong to more than one board (board
        # membership is a JQL filter match, not an exclusive field) -- shown
        # as one joined lane per item rather than duplicating rows, since
        # every other swimlane assumes one lane per item.
        boards = as_list(item.get("boards"))
        return ", ".join(str(board) for board in boards) if boards else VIRTUAL_NONE
    return None


PRIORITY_RANK: dict[str, int] = {"highest": 0, "high": 1, "medium": 2, "low": 3, "lowest": 4}

# Column header -> item dict field, for click-to-sort in the index table.
SORT_FIELDS: dict[str, str] = {
    "Key": "key",
    "State": "status",
    "Component": "component",
    "Summary": "summary",
    "Priority": "priority",
    "Assignee": "assignee",
    "Version": "fixVersion",
}


class _ReverseSortValue:
    """Wraps a sort key to invert its ordering within an otherwise-ascending tuple."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: "_ReverseSortValue") -> bool:
        return other.value < self.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ReverseSortValue) and self.value == other.value


def sort_field_value(item: dict[str, Any], field: str) -> Any:
    if field == "key":
        return issue_key_sort_key(display_name(item.get("key")))
    if field == "priority":
        name = display_name(item.get("priority")).strip().lower()
        return PRIORITY_RANK.get(name, len(PRIORITY_RANK))
    return field_value(item, field).lower()


def swimlane_sort_key(
    item: dict[str, Any], swimlane: str | None, *, sort_field: str | None = None, reverse: bool = False
) -> tuple[Any, ...]:
    mode = normalize_swimlane(swimlane)
    lane = swimlane_label(item, mode) or ""
    none_rank = 0 if lane == VIRTUAL_NONE else 1
    sort_tail: tuple[Any, ...] = ()
    if sort_field is not None:
        value = sort_field_value(item, sort_field)
        sort_tail = (_ReverseSortValue(value) if reverse else value,)
    if mode == "epic" and lane != VIRTUAL_NONE:
        lane_key = display_name(item.get("key") if is_epic_item(item) else item.get("epic"))
        item_rank = 0 if is_epic_item(item) else 1
        return (
            none_rank,
            issue_key_sort_key(lane_key),
            item_rank,
            *sort_tail,
            issue_key_sort_key(display_name(item.get("key"))),
        )
    return (none_rank, lane.lower(), *sort_tail, issue_key_sort_key(display_name(item.get("key"))))


def is_epic_item(item: dict[str, Any]) -> bool:
    return display_name(item.get("type")).strip().lower() == "epic"


# Explicit hex, not bare ANSI color names ("blue", "purple", ...) -- those are
# ColorType.STANDARD in Rich, meaning the actual rendered color depends on the
# terminal's own (often themed/remapped) ANSI palette rather than a fixed RGB
# value. Confirmed live: Rich's "blue" resolves to ColorType.STANDARD number=4,
# exactly the ANSI slot many dark terminal themes retint toward violet/indigo
# -- hex values render identically in every terminal regardless of its color
# scheme, the same way Textual's own chrome (e.g. the footer) already does
# via its theme system.
TYPE_ICON_COLORS = {
    "purple": "#a855f7",
    "green": "#22c55e",
    "blue": "#3b82f6",
    "red": "#ef4444",
    "cyan": "#06b6d4",
}

TYPE_ICONS: dict[str, tuple[str, str]] = {
    "epic": ("◆", TYPE_ICON_COLORS["purple"]),  # diamond
    "story": ("●", TYPE_ICON_COLORS["green"]),  # circle
    "task": ("■", TYPE_ICON_COLORS["blue"]),  # square
    "bug": ("▲", TYPE_ICON_COLORS["red"]),  # triangle
    "sub-task": ("▪", TYPE_ICON_COLORS["cyan"]),  # small square
    "subtask": ("▪", TYPE_ICON_COLORS["cyan"]),
}
DEFAULT_TYPE_ICON: tuple[str, str] = ("○", "dim")  # white circle; "dim" is a style attribute, not an ANSI color

# Nerd Font v3 codepoints (verified against the project's own glyphnames.json,
# not guessed from memory -- these are easy to misremember and a wrong
# codepoint just renders as a broken glyph). Opt-in only; see type_icon().
NERD_FONT_TYPE_ICONS: dict[str, tuple[str, str]] = {
    "epic": ("", TYPE_ICON_COLORS["purple"]),  # oct-rocket
    "story": ("\U000f00c0", TYPE_ICON_COLORS["green"]),  # md-bookmark
    "task": ("", TYPE_ICON_COLORS["blue"]),  # fa-square_check
    "bug": ("", TYPE_ICON_COLORS["red"]),  # fa-bug
    "sub-task": ("\U000f060d", TYPE_ICON_COLORS["cyan"]),  # md-subdirectory_arrow_right
    "subtask": ("\U000f060d", TYPE_ICON_COLORS["cyan"]),
}
DEFAULT_NERD_FONT_TYPE_ICON: tuple[str, str] = ("", "dim")  # oct-dot_fill


def type_icon(type_name: Any, *, nerd_font: bool = False) -> tuple[str, str]:
    """Glyph + Rich color for an issue type, echoing Jira's web icon colors.

    Plain geometric shapes by default, so it stays single-width and renders
    consistently across terminals/fonts with no setup needed. Pass
    nerd_font=True (driven by the [view].nerd_font config setting) for the
    Nerd Font v3 glyph set instead -- only sensible if the user's terminal
    font actually has those glyphs patched in.
    """
    icons = NERD_FONT_TYPE_ICONS if nerd_font else TYPE_ICONS
    default = DEFAULT_NERD_FONT_TYPE_ICON if nerd_font else DEFAULT_TYPE_ICON
    return icons.get(display_name(type_name).strip().lower(), default)


PRIORITY_ICON_COLORS = {
    "red": "#ef4444",
    "orange": "#f59e0b",
    "cyan": "#06b6d4",
    "green": "#22c55e",
    # Deliberately not on the red-to-green urgency gradient -- Medium is the
    # baseline, not a signal, so it gets a neutral gray rather than a hue.
    "gray": "#94a3b8",
}

PRIORITY_ICONS: dict[str, tuple[str, str]] = {
    "highest": ("⇈", PRIORITY_ICON_COLORS["red"]),
    "high": ("↑", PRIORITY_ICON_COLORS["orange"]),
    "medium": ("=", PRIORITY_ICON_COLORS["gray"]),
    "low": ("↓", PRIORITY_ICON_COLORS["cyan"]),
    "lowest": ("⇊", PRIORITY_ICON_COLORS["green"]),
}
DEFAULT_PRIORITY_ICON: tuple[str, str] = ("", "dim")  # unrecognized priority name -- not worth guessing at

# Nerd Font v3 codepoints, verified against glyphnames.json the same way as
# NERD_FONT_TYPE_ICONS above.
NERD_FONT_PRIORITY_ICONS: dict[str, tuple[str, str]] = {
    "highest": ("\U000f013f", PRIORITY_ICON_COLORS["red"]),  # md-chevron_double_up
    "high": ("\U000f0143", PRIORITY_ICON_COLORS["orange"]),  # md-chevron_up
    "medium": ("\U000f01fc", PRIORITY_ICON_COLORS["gray"]),  # md-equal
    "low": ("\U000f0140", PRIORITY_ICON_COLORS["cyan"]),  # md-chevron_down
    "lowest": ("\U000f013c", PRIORITY_ICON_COLORS["green"]),  # md-chevron_double_down
}
DEFAULT_NERD_FONT_PRIORITY_ICON: tuple[str, str] = ("", "dim")


def priority_icon(priority_name: Any, *, nerd_font: bool = False) -> tuple[str, str]:
    """Glyph + Rich color for a priority, red (highest) to green (lowest),
    with Medium shown as a neutral gray baseline marker rather than left
    blank. An unrecognized priority name still falls back to a blank,
    plain default -- nothing sensible to guess at for a custom scheme."""
    icons = NERD_FONT_PRIORITY_ICONS if nerd_font else PRIORITY_ICONS
    default = DEFAULT_NERD_FONT_PRIORITY_ICON if nerd_font else DEFAULT_PRIORITY_ICON
    return icons.get(display_name(priority_name).strip().lower(), default)


# Darker/more saturated ("600"-ish) shades than TYPE_ICON_COLORS -- these sit
# under white bold text as a filled background, so need more contrast margin
# than a bare colored glyph does.
PILL_PALETTE: list[str] = [
    "#ef4444",
    "#f97316",
    "#65a30d",
    "#16a34a",
    "#0d9488",
    "#0891b2",
    "#2563eb",
    "#4f46e5",
    "#9333ea",
    "#db2777",
]


def pill_color(value: str) -> str:
    """Deterministic per-name color: the same value always maps to the same
    palette entry, across runs and processes. Uses hashlib rather than the
    builtin hash() -- Python randomizes str hash per-process by default,
    which would make every label's color reshuffle on every launch."""
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return PILL_PALETTE[digest[0] % len(PILL_PALETTE)]


def pill_values(value: Any) -> list[str]:
    """Normalize a raw Jira field value (list, dict, str, None) into the list
    of individual display strings to render as separate pills."""
    if isinstance(value, list):
        return [name for item in value if (name := display_name(item))]
    name = display_name(value)
    return [name] if name else []


def filter_items_for_swimlane(items: list[dict[str, Any]], swimlane: str | None) -> list[dict[str, Any]]:
    return items


def sort_items_for_swimlane(
    items: list[dict[str, Any]], swimlane: str | None, *, sort_field: str | None = None, reverse: bool = False
) -> list[dict[str, Any]]:
    if normalize_swimlane(swimlane) == "none" and sort_field is None:
        return items
    return sorted(items, key=lambda item: swimlane_sort_key(item, swimlane, sort_field=sort_field, reverse=reverse))


def item_index_by_key(items: list[dict[str, Any]], key: str | None) -> int | None:
    if not key:
        return None
    normalized = key.strip().lower()
    for index, item in enumerate(items):
        if display_name(item.get("key")).strip().lower() == normalized:
            return index
    return None


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


SMART_LINK_RE = re.compile(r"\[(?:([^|\[\]]*)\|)?([^|\[\]]+)\|smart-link\]")


def simplify_smart_links(text: str) -> str:
    """Collapse Jira's `[label|url|smart-link]` wiki markup down to plain text.

    Jira's REST API renders inline "smart chip" links as this raw wiki
    markup rather than resolving them, and label/url are usually identical
    -- shown as-is that's `[https://...|https://...|smart-link]` twice over.
    """

    def replace(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if label and label != url:
            return f"{label} ({url})"
        return url

    return SMART_LINK_RE.sub(replace, text)


def text_from_adf(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return simplify_smart_links(value)
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := text_from_adf(item)))
    if not isinstance(value, dict):
        return str(value)

    node_type = value.get("type")
    pieces: list[str] = []
    for child in as_list(value.get("content")):
        child_text = text_from_adf(child)
        if child_text:
            pieces.append(child_text)
    if isinstance(value.get("text"), str):
        pieces.insert(0, value["text"])

    if node_type in {"text", "paragraph", "heading"}:
        text = "".join(pieces)
    else:
        text = "\n".join(pieces)
    if node_type == "listItem":
        return "\n".join(f"- {line}" if index == 0 else f"  {line}" for index, line in enumerate(text.splitlines()))
    return text


def display_name(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "value", "displayName", "key"):
            item = value.get(key)
            if isinstance(item, str):
                return item
        return json.dumps(value, sort_keys=True)
    if isinstance(value, list):
        return ", ".join(part for item in value if (part := display_name(item)))
    return str(value)


def display_component(value: Any) -> str:
    # "_unassigned" is the real on-disk sync sentinel for "no component"
    # (see sync.py's component_slug) -- meaningful for filtering, but not a
    # useful thing to print in a list view where every other unset field
    # (assignee, fix version, ...) just renders blank.
    name = display_name(value)
    return "" if name == "_unassigned" else name


def assignee_first_name_map(assignee_names: list[str]) -> dict[str, str]:
    """Full display name -> first name, but only if every currently visible
    assignee's first name is unique -- e.g. "Alex Smith" and "Alex Jones"
    both showing as "Alex" would be more confusing than useful. Falls back
    to an empty map (show full names, unchanged) the moment two different
    people would collide on the same first name."""
    full_names = {name for name in assignee_names if name}
    first_names = [name.split()[0] for name in full_names]
    if len(set(first_names)) != len(full_names):
        return {}
    return {name: name.split()[0] for name in full_names}


def load_cached_boards(jira_dir: Path) -> list[dict[str, Any]]:
    return load_all_boards_with_settings(jira_dir)


def load_manifest_items(
    jira_dir: Path, component_field: str | None = None, *, dev_status_field: str | None = None
) -> list[dict[str, Any]]:
    manifest_path = jira_dir / "manifest.json"
    if not manifest_path.exists():
        raise ViewError(f"manifest not found under {jira_dir}. Run jira-wb sync first.")
    manifest = read_json(manifest_path)
    items = manifest.get("workItems") if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        raise ViewError(f"manifest at {manifest_path} does not contain workItems")
    boards = load_cached_boards(jira_dir)
    status_categories = observed_status_category_map(jira_dir)
    version_names = version_id_to_name_map(jira_dir)
    return sorted(
        (
            with_local_index_fields(
                jira_dir,
                item,
                component_field,
                boards=boards,
                status_categories=status_categories,
                version_names=version_names,
                dev_status_field=dev_status_field,
            )
            for item in items
            if isinstance(item, dict)
        ),
        key=lambda item: issue_key_sort_key(display_name(item.get("key"))),
    )


def with_local_index_fields(
    jira_dir: Path,
    item: dict[str, Any],
    component_field: str | None = None,
    *,
    boards: list[dict[str, Any]] | None = None,
    status_categories: dict[str, str] | None = None,
    version_names: dict[str, str] | None = None,
    dev_status_field: str | None = None,
) -> dict[str, Any]:
    enriched = dict(item)
    key = display_name(item.get("key"))
    if not key:
        return enriched
    raw_component_hint = item.get("component")
    component_hint = raw_component_hint if isinstance(raw_component_hint, str) else None
    path = find_existing_issue(jira_dir / "components", key, component_hint=component_hint)
    if path is None:
        return enriched
    issue = read_json(path)
    if not isinstance(issue, dict):
        return enriched
    shadow = load_shadow(jira_dir, key, component_hint=component_hint)
    if shadow is not None:
        issue = apply_shadow(issue, shadow)
    fields = as_dict(issue.get("fields"))
    issue_type = fields.get("issuetype")
    status = fields.get("status")
    fix_version_names = resolve_fix_version_names(jira_dir, fields.get("fixVersions"), id_to_name=version_names)
    parent = as_dict(fields.get("parent"))
    parent_fields = as_dict(parent.get("fields"))
    enriched["summary"] = display_name(fields.get("summary"))
    enriched["status"] = display_name(status)
    # `status` is a bare name string (not the real status dict) whenever a
    # local shadow status-change is in effect (see apply_shadow) -- fall
    # back to whatever category this same status name resolves to
    # elsewhere in the locally synced instance, since the shadow itself
    # never stores one.
    category = status_category_key(status)
    if category is None:
        if status_categories is None:
            status_categories = observed_status_category_map(jira_dir)
        category = status_categories.get(enriched["status"])
    enriched["statusCategory"] = category
    enriched["type"] = display_name(issue_type)
    enriched["component"] = hierarchy_component(issue, component_field) or display_name(item.get("component"))
    enriched["fixVersion"] = fix_version_names[0] if fix_version_names else ""
    enriched["priority"] = display_name(fields.get("priority"))
    enriched["assignee"] = display_name(fields.get("assignee"))
    enriched["epic"] = display_name(parent.get("key"))
    enriched["epicSummary"] = display_name(parent_fields.get("summary"))
    enriched["statusCategoryChangeDate"] = display_name(fields.get("statuscategorychangedate"))
    enriched["issueId"] = issue.get("id")
    enriched["project"] = issue_project_key(issue)
    enriched["devStatus"] = parse_dev_status_summary(fields.get(dev_status_field or DEV_STATUS_FIELD_DEFAULT))

    labels = [label for label in as_list(fields.get("labels")) if isinstance(label, str)]
    enriched["labels"] = labels
    if boards is None:
        boards = load_cached_boards(jira_dir)
    component_value = enriched["component"] or None
    matched_boards: list[str] = []
    board_status: dict[str, str] = {}
    for board in boards:
        if board.get("kind") == "local":
            field_filters = board.get("fieldFilters") or {}
            matched = all(matches_field(enriched, field, value) for field, value in field_filters.items())
            matched = matched and matches_filter(enriched, board.get("pattern"))
        else:
            predicate = board.get("predicate")
            if not predicate:
                continue
            matched = evaluate_predicate(predicate, component=component_value, labels=labels, project=enriched["project"])
        if not matched:
            continue
        name = str(board.get("name") or "")
        matched_boards.append(name)
        if board.get("kind") == "local":
            # A local board has no Jira-fetched backlog data at all -- an
            # optional user-defined "active filter" (same fieldFilters+
            # pattern shape as the board's own membership definition) is
            # the only way board_scope applies to one; undefined means no
            # board_status entry, i.e. board_scope stays a no-op for it,
            # same as today.
            active_filter = board.get("activeFilter")
            if active_filter:
                active_field_filters = active_filter.get("fieldFilters") or {}
                is_board_active = all(
                    matches_field(enriched, field, value) for field, value in active_field_filters.items()
                ) and matches_filter(enriched, active_filter.get("pattern"))
                board_status[name] = "active" if is_board_active else "backlog"
        else:
            backlog_keys = board.get("backlogKeys")
            if isinstance(backlog_keys, list):
                board_status[name] = "backlog" if key in backlog_keys else "active"
    enriched["boards"] = matched_boards
    enriched["boardStatus"] = board_status
    return enriched


def refresh_index_item(
    jira_dir: Path,
    items: list[dict[str, Any]],
    key: str,
    component_field: str | None = None,
    *,
    boards: list[dict[str, Any]] | None = None,
    status_categories: dict[str, str] | None = None,
    version_names: dict[str, str] | None = None,
    dev_status_field: str | None = None,
) -> None:
    for index, item in enumerate(items):
        if display_name(item.get("key")) == key:
            items[index] = with_local_index_fields(
                jira_dir,
                item,
                component_field,
                boards=boards,
                status_categories=status_categories,
                version_names=version_names,
                dev_status_field=dev_status_field,
            )
            return


def refresh_stale_index_items(
    jira_dir: Path,
    items: list[dict[str, Any]],
    keys: set[str],
    component_field: str | None = None,
    *,
    dev_status_field: str | None = None,
) -> None:
    boards = load_cached_boards(jira_dir)
    status_categories = observed_status_category_map(jira_dir)
    version_names = version_id_to_name_map(jira_dir)
    for key in sorted(keys, key=issue_key_sort_key):
        refresh_index_item(
            jira_dir,
            items,
            key,
            component_field,
            boards=boards,
            status_categories=status_categories,
            version_names=version_names,
            dev_status_field=dev_status_field,
        )
    keys.clear()


def index_item_parent_key(item: dict[str, Any]) -> str:
    return display_name(item.get("epic")).strip()


def load_issue(jira_dir: Path, key: str) -> dict[str, Any]:
    path = find_existing_issue(jira_dir / "components", key)
    if path is None:
        raise ViewError(f"work item {key} is not synced locally")
    issue = read_json(path)
    if not isinstance(issue, dict):
        raise ViewError(f"local issue file for {key} is not a JSON object")
    return issue


def issue_identity(issue: dict[str, Any]) -> str:
    key = display_name(issue.get("key"))
    fields = as_dict(issue.get("fields"))
    summary = display_name(fields.get("summary"))
    return f"{key} {summary}".rstrip()


def child_issues(jira_dir: Path, parent_key: str) -> list[dict[str, Any]]:
    children = []
    components_dir = jira_dir / "components"
    if not components_dir.exists():
        return []
    for issue_path in sorted(components_dir.glob("*/*/issue.json"), key=issue_path_sort_key):
        issue = read_json(issue_path)
        if not isinstance(issue, dict):
            continue
        shadow = load_shadow(jira_dir, display_name(issue.get("key")))
        if shadow is not None:
            issue = apply_shadow(issue, shadow)
        fields = as_dict(issue.get("fields"))
        parent = as_dict(fields.get("parent"))
        if display_name(parent.get("key")) == parent_key:
            children.append(issue)
    return children


def local_issues(jira_dir: Path) -> list[dict[str, Any]]:
    components_dir = jira_dir / "components"
    if not components_dir.exists():
        return []
    issues = []
    for issue_path in sorted(components_dir.glob("*/*/issue.json"), key=issue_path_sort_key):
        issue = read_json(issue_path)
        if isinstance(issue, dict):
            issues.append(issue)
    return issues


def hierarchy_component(issue: dict[str, Any], component_field: str | None) -> str:
    fields = as_dict(issue.get("fields"))
    if component_field:
        component = display_name(fields.get(component_field)).strip()
        if component:
            return component
    return display_name(fields.get("components")).strip()


def hierarchy_child_identity(issue: dict[str, Any], component_field: str | None) -> str:
    identity = issue_identity(issue)
    component = hierarchy_component(issue, component_field)
    if not component:
        return identity
    key, _, summary = identity.partition(" ")
    return f"{key} [{component}] {summary}".rstrip()


def hierarchy_section(jira_dir: Path, issue: dict[str, Any], component_field: str | None = None) -> list[str]:
    key = display_name(issue.get("key"))
    fields = as_dict(issue.get("fields"))
    parent = as_dict(fields.get("parent"))
    parent_key = display_name(parent.get("key"))
    parent_summary = display_name(as_dict(parent.get("fields")).get("summary"))
    children = child_issues(jira_dir, key)

    if parent_key:
        epic_identity = f"{parent_key} {parent_summary}".rstrip()
        return [f"Epic: {epic_identity}", f"|- {hierarchy_child_identity(issue, component_field)}", ""]
    if children:
        lines = [f"Epic: {issue_identity(issue)}"]
        lines.extend(f"|- {hierarchy_child_identity(child, component_field)}" for child in children)
        lines.append("")
        return lines
    return []


def issue_parent_key(issue: dict[str, Any]) -> str:
    fields = as_dict(issue.get("fields"))
    return display_name(as_dict(fields.get("parent")).get("key")).strip()


def field_section(fields: dict[str, Any], component_field: str | None, jira_dir: Path | None = None) -> list[str]:
    if jira_dir is not None:
        fix_version_names = resolve_fix_version_names(jira_dir, fields.get("fixVersions"))
        fix_versions = ", ".join(fix_version_names) if fix_version_names else "(none)"
    else:
        fix_versions = display_name(fields.get("fixVersions")) or "(none)"
    component_source = component_field or "components"
    components = display_name(fields.get(component_source))
    labels = display_name(fields.get("labels"))
    assignee = display_name(fields.get("assignee")) or "(unassigned)"
    reporter = display_name(fields.get("reporter"))

    rows = [
        ("Type", display_name(fields.get("issuetype"))),
        ("Status", display_name(fields.get("status"))),
        ("Fix versions", fix_versions),
        ("Assignee", assignee),
        ("Reporter", reporter),
        ("Components", components),
        ("Updated", display_name(fields.get("updated"))),
        ("Created", display_name(fields.get("created"))),
    ]
    if labels:
        rows.append(("Labels", labels))
    return format_field_rows(rows)


def editable_field_choices(component_field: str | None = None) -> list[tuple[str, str]]:
    effective_component_field = component_field or "components"
    choices = [
        (label, effective_component_field if field == "components" else field)
        for label, field in BASE_EDITABLE_FIELDS
    ]
    return choices


def editable_field_value(issue: dict[str, Any], field: str) -> str:
    fields = as_dict(issue.get("fields"))
    if field == "type":
        return display_name(fields.get("issuetype"))
    if field == "parent":
        return display_name(as_dict(fields.get("parent")).get("key"))
    if field == "description":
        return text_from_adf(fields.get("description")).strip()
    return display_name(fields.get(field))


def selectable_field_options(
    jira_dir: Path,
    field: str,
    component_field: str | None = None,
    *,
    include_inactive_versions: bool = False,
    current_issue: dict[str, Any] | None = None,
    include_inactive_parents: bool = False,
) -> list[str]:
    if field == "status":
        return observed_field_options(jira_dir, "status")
    if field == "assignee":
        return observed_field_options(jira_dir, "assignee", include_empty="(unassigned)")
    if field == "fixVersions":
        return version_options(jira_dir, include_inactive=include_inactive_versions)
    if field == "priority":
        return observed_field_options(jira_dir, "priority")
    if field == "components":
        return component_options(jira_dir)
    if field == "parent":
        return parent_options(jira_dir, current_issue=current_issue, active=not include_inactive_parents)
    if component_field and field == component_field:
        return observed_field_options(jira_dir, component_field)
    return []


def observed_field_options(jira_dir: Path, field: str, *, include_empty: str | None = None) -> list[str]:
    options = set()
    for issue in local_issues(jira_dir):
        fields = as_dict(issue.get("fields"))
        value = fields.get("issuetype") if field == "type" else fields.get(field)
        name = display_name(value).strip()
        if name:
            options.add(name)
    # Priority has a real severity order (same PRIORITY_RANK the index table
    # sorts by) -- alphabetical would scatter it as Highest/High/Low/Lowest/
    # Medium, putting Medium at the end instead of in the middle where it
    # belongs.
    if field == "priority":
        sorted_options = sorted(options, key=lambda name: PRIORITY_RANK.get(name.strip().lower(), len(PRIORITY_RANK)))
    else:
        sorted_options = sorted(options, key=str.lower)
    if include_empty:
        return [include_empty, *sorted_options]
    return sorted_options


def observed_status_category_map(jira_dir: Path) -> dict[str, str]:
    """Status display name -> Jira's real statusCategory.key ("new"/
    "indeterminate"/"done"), built from every locally synced issue's own
    (never shadow-merged) status. A project's workflow can name its
    statuses anything, but every one still belongs to one of these three
    fixed system categories -- this is what lets a status a user has
    locally changed to (shadow-stored as a bare name, with no category of
    its own -- see apply_shadow) still be classified correctly, as long as
    that name has been observed on some synced issue anywhere in this
    jira_dir."""
    categories: dict[str, str] = {}
    for issue in local_issues(jira_dir):
        status = as_dict(as_dict(issue.get("fields")).get("status"))
        name = display_name(status.get("name")).strip()
        category = status_category_key(status)
        if name and category:
            categories[name] = category
    return categories


def field_label_options(jira_dir: Path, field: str) -> list[str]:
    values: set[str] = set()
    for issue in local_issues(jira_dir):
        fields = as_dict(issue.get("fields"))
        for value in as_list(fields.get(field)):
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
    return sorted(values, key=str.lower)


def label_options(jira_dir: Path) -> list[str]:
    return field_label_options(jira_dir, "labels")


def label_type_fields(edit_fields: dict[str, dict[str, Any]] | None) -> list[tuple[str, str]]:
    # Native "labels" is always included, even without live edit metadata (fetch
    # failed, or no API configured) -- so that one already-known field never
    # regresses just because the live fetch didn't happen this time.
    result: dict[str, str] = {"labels": "Labels"}
    for field_id, meta in (edit_fields or {}).items():
        if not isinstance(meta, dict):
            continue
        schema = meta.get("schema")
        custom = schema.get("custom") if isinstance(schema, dict) else None
        if isinstance(custom, str) and custom.endswith(":labels"):
            result[field_id] = str(meta.get("name") or field_id)
    return sorted(result.items(), key=lambda pair: pair[1].lower())


def component_options(jira_dir: Path) -> list[str]:
    components = load_all_components(jira_dir)
    names = sorted({display_name(component).strip() for component in components if display_name(component).strip()})
    if names:
        return names
    return [component for component, _ in component_counts(load_manifest_items(jira_dir))]


def is_active_version(version: dict[str, Any]) -> bool:
    return not version.get("archived") and not version.get("released")


def version_options(jira_dir: Path, *, include_inactive: bool = False) -> list[str]:
    versions = load_all_versions(jira_dir)
    names = sorted(
        {
            version_name(version).strip()
            for version in versions
            if isinstance(version, dict) and (include_inactive or is_active_version(version))
        }
    )
    return ["(none)", *(name for name in names if name)]


def issue_type_name(issue: dict[str, Any]) -> str:
    return display_name(as_dict(as_dict(issue.get("fields")).get("issuetype")).get("name")).strip()


def issue_hierarchy_level(issue: dict[str, Any]) -> int | None:
    value = as_dict(as_dict(issue.get("fields")).get("issuetype")).get("hierarchyLevel")
    return value if isinstance(value, int) else None


def parent_candidate_label(issue: dict[str, Any]) -> str:
    issue_type = issue_type_name(issue) or "Issue"
    return f"{issue_type}: {issue_identity(issue)}".rstrip()


def is_valid_parent_candidate(candidate: dict[str, Any], current_issue: dict[str, Any] | None = None) -> bool:
    candidate_key = display_name(candidate.get("key"))
    if current_issue is not None and candidate_key == display_name(current_issue.get("key")):
        return False
    candidate_level = issue_hierarchy_level(candidate)
    if candidate_level is None:
        return issue_type_name(candidate).lower() in {"epic", "story"}
    if candidate_level < 0:
        return False
    if current_issue is None:
        return True
    current_level = issue_hierarchy_level(current_issue)
    if current_level is None:
        return candidate_level > 0
    if current_level < 0:
        return candidate_level == 0
    return candidate_level > current_level


def parent_options(
    jira_dir: Path,
    *,
    current_issue: dict[str, Any] | None = None,
    active: bool = True,
) -> list[str]:
    options = [
        parent_candidate_label(issue)
        for issue in local_issues(jira_dir)
        if is_valid_parent_candidate(issue, current_issue)
        and (not active or is_active_issue(issue))
    ]
    return ["(none)", *sorted((option for option in options if option), key=issue_key_option_sort_key)]


def is_active_issue(issue: dict[str, Any]) -> bool:
    fields = as_dict(issue.get("fields"))
    status = fields.get("status")
    category = status_category_key(status)
    if category is not None:
        return category != "done"
    name = as_dict(status).get("name") or status
    return display_name(name).strip().lower() not in FALLBACK_DONE_STATUS_NAMES


def issue_key_option_sort_key(option: str) -> tuple[str, int, str]:
    key = issue_key_from_text(option) or option.split(" ", 1)[0]
    return issue_key_sort_key(key)


def encode_edit_value(field: str, value: str, *, component_field: str | None = None) -> Any:
    stripped = value.strip()
    if stripped == "(unassigned)" and field == "assignee":
        stripped = ""
    if field in {"summary", "description", "type", "status", "assignee"}:
        return stripped
    if field == "priority":
        return {"name": stripped} if stripped else None
    if stripped == "(none)" and field == "fixVersions":
        return []
    if field in {"fixVersions", "components"}:
        return [{"name": part} for part in comma_parts(stripped)]
    if field == "labels":
        return comma_parts(stripped)
    if field == "parent":
        if stripped == "(none)":
            return None
        parent_key = issue_key_from_text(stripped) or stripped.split(" ", 1)[0]
        return {"key": parent_key} if parent_key else None
    if component_field and field == component_field:
        return {"value": stripped} if stripped else None
    return stripped


def comma_parts(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def format_field_rows(rows: list[tuple[str, str]]) -> list[str]:
    visible_rows = [(name, value) for name, value in rows if value]
    if not visible_rows:
        return []
    label_width = max(len(name) for name, _ in visible_rows)
    cells = [f"{name:<{label_width}}  {value}" for name, value in visible_rows]
    left_width = max(len(cell) for cell in cells[::2])
    lines = []
    for index in range(0, len(cells), 2):
        left = cells[index]
        right = cells[index + 1] if index + 1 < len(cells) else ""
        lines.append(f"{left:<{left_width}}  {right}".rstrip())
    return lines


def issue_link_groups(issue: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """[(phrase, [linked issue keys]), ...], e.g. [("blocks", ["SAT-900"]),
    ("is blocked by", ["SAT-100", "SAT-200"])] -- multiple links sharing the
    same phrase are grouped into one row's worth of keys. Read from
    fields.issuelinks, already part of a regular sync (fields="*all"), so
    this needs no extra fetching."""
    fields = as_dict(issue.get("fields"))
    groups: dict[str, list[str]] = {}
    for link in as_list(fields.get("issuelinks")):
        if not isinstance(link, dict):
            continue
        link_type = as_dict(link.get("type"))
        if "outwardIssue" in link:
            phrase = link_type.get("outward")
            other = link.get("outwardIssue")
        elif "inwardIssue" in link:
            phrase = link_type.get("inward")
            other = link.get("inwardIssue")
        else:
            continue
        key = display_name(as_dict(other).get("key"))
        if phrase and key:
            groups.setdefault(str(phrase), []).append(key)
    return sorted(groups.items())


def detail_field_rows(
    issue: dict[str, Any],
    component_field: str | None,
    comments: str | None = None,
    *,
    edit_fields: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, str, str]]:
    fields = as_dict(issue.get("fields"))
    component_source = component_field or "components"
    rows = [
        ("Summary", "summary", display_name(fields.get("summary"))),
        ("Description", "description", description_text(issue)),
    ]
    if comments:
        rows.append(("Comments", "comments", comments))
    rows.extend([
        ("Type", "type", display_name(fields.get("issuetype"))),
        ("Status", "status", display_name(fields.get("status"))),
        ("Priority", "priority", display_name(fields.get("priority"))),
        ("Fix versions", "fixVersions", display_name(fields.get("fixVersions")) or "(none)"),
        ("Assignee", "assignee", display_name(fields.get("assignee")) or "(unassigned)"),
        ("Reporter", "reporter", display_name(fields.get("reporter"))),
        ("Parent", "parent", issue_identity(as_dict(fields.get("parent"))) or "(none)"),
        ("Components", component_source, display_name(fields.get(component_source))),
        ("Labels", "labels", display_name(fields.get("labels"))),
        ("Updated", "updated", display_name(fields.get("updated"))),
        ("Created", "created", display_name(fields.get("created"))),
    ])
    if edit_fields and "duedate" in edit_fields:
        rows.append(("Due date", "duedate", display_name(fields.get("duedate")) or "(none)"))
    for field_id, field_name in label_type_fields(edit_fields):
        if field_id == "labels":
            continue  # already a static row above
        rows.append((field_name, field_id, display_name(fields.get(field_id)) or "(none)"))
    for phrase, keys in issue_link_groups(issue):
        rows.append((phrase.capitalize(), f"link:{phrase}", ", ".join(keys)))
    return [(label, field, value) for label, field, value in rows if value]


def description_text(issue: dict[str, Any]) -> str:
    return text_from_adf(as_dict(issue.get("fields")).get("description")).strip() or "(empty)"


def comments_text(jira_dir: Path, key: str, shadow: dict[str, Any] | None = None) -> str:
    comments = []
    if shadow is not None:
        for comment in reversed(as_list(shadow.get("comments"))):
            if isinstance(comment, dict):
                body = display_name(comment.get("body")).strip()
                if body:
                    state = display_name(comment.get("state")) or "working"
                    created = display_name(comment.get("createdAt"))
                    comments.append((created, f"[local {state}] {body}"))

    comments_path = issue_dir_path(jira_dir, key) / "comments.json"
    if comments_path.exists():
        payload = read_json(comments_path)
        remote_comments = as_list(as_dict(payload).get("comments")) if isinstance(payload, dict) else as_list(payload)
        for comment in sorted(remote_comments, key=comment_created, reverse=True):
            if not isinstance(comment, dict):
                continue
            body = comment_body_text(comment).strip()
            if not body:
                continue
            author = display_name(as_dict(comment.get("author")).get("displayName"))
            created = display_name(comment.get("created"))
            prefix = " ".join(part for part in (created, author) if part)
            comments.append((created, f"{prefix}: {body}" if prefix else body))
    return "\n\n".join(text for _, text in comments)


def issue_dir_path(jira_dir: Path, key: str) -> Path:
    path = find_existing_issue(jira_dir / "components", key)
    if path is None:
        raise ViewError(f"work item {key} is not synced locally")
    return path.parent


def comment_created(comment: Any) -> str:
    return display_name(as_dict(comment).get("created"))


def comment_body_text(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    if isinstance(body, dict):
        return text_from_adf(body)
    return simplify_smart_links(display_name(body))


def list_comments(jira_dir: Path, key: str, shadow: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """One row per comment (not a single blob), each tagged with its local shadow state.

    Row "state" is one of: synced, edited, pending-delete, local-new. "id" is
    the remote comment id for synced/edited/pending-delete rows, or the
    "local-<uuid>" id assigned by add_comment for local-new rows -- callers
    use it to address a specific comment for edit_comment/delete_comment.
    """
    edits = as_dict(shadow.get("commentEdits")) if shadow is not None else {}
    deletes = {str(value) for value in as_list(shadow.get("commentDeletes"))} if shadow is not None else set()
    local_new = as_list(shadow.get("comments")) if shadow is not None else []

    comments_path = issue_dir_path(jira_dir, key) / "comments.json"
    remote_raw: list[Any] = []
    if comments_path.exists():
        payload = read_json(comments_path)
        remote_raw = as_list(as_dict(payload).get("comments")) if isinstance(payload, dict) else as_list(payload)

    rows: list[dict[str, str]] = []
    for comment in remote_raw:
        if not isinstance(comment, dict):
            continue
        comment_id = str(comment.get("id") or "")
        original_body = comment_body_text(comment).strip()
        if comment_id in deletes:
            state = "pending-delete"
            body = original_body
        elif comment_id in edits:
            state = "edited"
            body = str(edits[comment_id])
        else:
            state = "synced"
            body = original_body
        rows.append(
            {
                "id": comment_id,
                "author": display_name(as_dict(comment.get("author")).get("displayName")),
                "created": display_name(comment.get("created")),
                "body": body,
                "state": state,
            }
        )

    for comment in local_new:
        if not isinstance(comment, dict):
            continue
        rows.append(
            {
                "id": display_name(comment.get("id")),
                "author": "(you, unpushed)",
                "created": display_name(comment.get("createdAt")),
                "body": display_name(comment.get("body")),
                "state": "local-new",
            }
        )

    rows.sort(key=lambda row: row["created"], reverse=True)
    return rows


def other_field_rows(issue: dict[str, Any], component_field: str | None) -> list[tuple[str, str]]:
    fields = as_dict(issue.get("fields"))
    hidden = {
        "summary",
        "description",
        "issuetype",
        "status",
        "priority",
        "assignee",
        "reporter",
        "fixVersions",
        "parent",
        "components",
        "labels",
        "updated",
        "created",
        component_field,
    }
    rows = []
    for field in sorted(key for key in fields if key not in hidden):
        value = fields[field]
        if value is not None:
            rows.append((field, display_name(value)))
    return rows


def editable_detail_fields(
    component_field: str | None = None, edit_fields: dict[str, dict[str, Any]] | None = None
) -> set[str]:
    fields = {field for _, field in editable_field_choices(component_field)}
    if edit_fields and "duedate" in edit_fields:
        fields.add("duedate")
    fields.update(field_id for field_id, _ in label_type_fields(edit_fields))
    return fields


def format_issue(
    issue: dict[str, Any],
    *,
    jira_dir: Path | None = None,
    component_field: str | None = None,
    shadow: dict[str, Any] | None = None,
) -> str:
    if shadow is not None:
        issue = apply_shadow(issue, shadow)

    key = display_name(issue.get("key"))
    fields = as_dict(issue.get("fields"))
    summary = display_name(fields.get("summary"))
    description = text_from_adf(fields.get("description")).strip()

    lines = []
    if jira_dir is not None:
        lines.extend(hierarchy_section(jira_dir, issue, component_field))
    lines.extend([f"{key}  {summary}".rstrip(), ""])
    if shadow is not None:
        lines.extend(format_shadow_summary(shadow))
        lines.append("")
    lines.extend(field_section(fields, component_field, jira_dir))
    lines.append("")
    lines.append("Description")
    lines.append("-----------")
    lines.append(description or "(empty)")
    if jira_dir is not None:
        comments = comments_text(jira_dir, key, shadow)
        if comments:
            lines.append("")
            lines.append("Comments")
            lines.append("--------")
            lines.append(comments)

    remaining = sorted(
        field for field in fields if field not in {
            "summary",
            "description",
            "issuetype",
            "status",
            "assignee",
            "reporter",
            "fixVersions",
            "parent",
            "components",
            "labels",
            "updated",
            "created",
            component_field,
        }
    )
    if remaining:
        lines.append("")
        lines.append("Other fields")
        lines.append("------------")
        for field in remaining:
            value = fields[field]
            if value is not None:
                lines.append(f"{field}: {display_name(value)}")
    return "\n".join(lines).rstrip() + "\n"


def effective_comments_text(jira_dir: Path, key: str, shadow: dict[str, Any] | None) -> str:
    """Comments as they'll read once pushed: edits applied, deletions removed,
    new ones included -- unlike comments_text() (kept as-is for the CLI's
    existing plain-text report), which only ever prepends new local
    comments and never reflects an edit or delete of an existing one.
    """
    rows = list_comments(jira_dir, key, shadow=shadow)
    parts: list[tuple[str, str]] = []
    for row in rows:
        if row["state"] == "pending-delete":
            continue
        prefix = " ".join(part for part in (row["created"], row["author"]) if part)
        text = f"{prefix}: {row['body']}" if prefix else row["body"]
        if row["state"] == "local-new":
            text = f"[local new] {text}"
        elif row["state"] == "edited":
            text = f"[local edited] {text}"
        parts.append((row["created"], text))
    parts.sort(key=lambda part: part[0], reverse=True)
    return "\n\n".join(text for _, text in parts)


def diffable_issue_text(
    issue: dict[str, Any],
    jira_dir: Path,
    key: str,
    component_field: str | None,
    shadow: dict[str, Any] | None,
) -> str:
    """Renders one side (before or after) of a full item diff: fields,
    description, and comments -- pass shadow=None for the original/remote
    side, or the loaded shadow for the effective/local side.
    """
    effective = apply_shadow(issue, shadow) if shadow is not None else issue
    fields = as_dict(effective.get("fields"))
    summary = display_name(fields.get("summary"))
    description = text_from_adf(fields.get("description")).strip()

    lines = [f"{key}  {summary}".rstrip(), ""]
    lines.extend(field_section(fields, component_field, jira_dir))
    lines.append("")
    lines.append("Description")
    lines.append("-----------")
    lines.append(description or "(empty)")

    comments = comments_text(jira_dir, key, None) if shadow is None else effective_comments_text(jira_dir, key, shadow)
    if comments:
        lines.append("")
        lines.append("Comments")
        lines.append("--------")
        lines.append(comments)
    return "\n".join(lines)


def full_diff_texts(jira_dir: Path, key: str, component_field: str | None = None) -> tuple[str, str]:
    """Full before/after text for a single item's local shadow changes, for
    a vimdiff-style side-by-side comparison. Includes fields, description,
    and comments (correctly reflecting comment edits/deletes, unlike the
    plain-text comments_text()-based CLI report).
    """
    issue = load_issue(jira_dir, key)
    shadow = load_shadow(jira_dir, key)
    before = diffable_issue_text(issue, jira_dir, key, component_field, None)
    after = diffable_issue_text(issue, jira_dir, key, component_field, shadow)
    return before, after


def wrap_preview_lines(value: str, width: int) -> list[str]:
    """Split into display lines, word-wrapping any paragraph longer than `width`.

    A single-paragraph description has no "\\n" at all, so splitting on
    newlines alone always yields one giant line -- capping line *count*
    then does nothing to show more of it. Wrapping each paragraph to
    `width` first is what actually lets a taller preview show more text.

    break_long_words/break_on_hyphens are off so a URL (or any other single
    long token) stays intact on one line instead of getting chopped -- or
    split at a hyphen -- mid-word.
    """
    if not value:
        return [""]
    lines: list[str] = []
    for paragraph in value.splitlines():
        if not paragraph:
            lines.append("")
            continue
        wrapped = textwrap.wrap(paragraph, width=width, break_long_words=False, break_on_hyphens=False)
        lines.extend(wrapped or [""])
    return lines or [""]


def side_by_side_diff_lines(before_text: str, after_text: str) -> list[tuple[str, str, str]]:
    """Vimdiff-style line-aligned diff: one (tag, left_line, right_line) per
    row, where tag is "equal"/"changed"/"removed"/"added" -- removed-only
    lines leave the right side blank, added-only lines leave the left side
    blank, so the two columns stay vertically aligned line-for-line.
    """
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    rows: list[tuple[str, str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for before_line, after_line in zip(before_lines[i1:i2], after_lines[j1:j2]):
                rows.append(("equal", before_line, after_line))
        elif tag == "replace":
            left = before_lines[i1:i2]
            right = after_lines[j1:j2]
            for before_line, after_line in zip_longest(left, right, fillvalue=""):
                rows.append(("changed", before_line, after_line))
        elif tag == "delete":
            rows.extend(("removed", before_line, "") for before_line in before_lines[i1:i2])
        elif tag == "insert":
            rows.extend(("added", "", after_line) for after_line in after_lines[j1:j2])
    return rows


def apply_shadow(issue: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Any]:
    effective = copy.deepcopy(issue)
    fields = effective.setdefault("fields", {})
    if not isinstance(fields, dict):
        fields = {}
        effective["fields"] = fields

    shadow_fields = shadow.get("fields", {})
    if isinstance(shadow_fields, dict):
        for field, value in shadow_fields.items():
            if field == "type":
                fields["issuetype"] = {"name": value}
            else:
                fields[field] = value
    return effective


def shadow_modified_fields(shadow: dict[str, Any] | None) -> set[str]:
    fields = as_dict(shadow.get("fields")) if shadow is not None else {}
    return set(fields)


def has_shadow_changes(shadow: dict[str, Any] | None) -> bool:
    if shadow is None:
        return False
    fields = as_dict(shadow.get("fields"))
    comments = as_list(shadow.get("comments"))
    comment_edits = as_dict(shadow.get("commentEdits"))
    comment_deletes = as_list(shadow.get("commentDeletes"))
    status_change = as_dict(shadow.get("statusChange"))
    return bool(fields or comments or comment_edits or comment_deletes or status_change)


def format_shadow_summary(shadow: dict[str, Any]) -> list[str]:
    fields = shadow.get("fields", {})
    comments = shadow.get("comments", [])
    comment_edits = shadow.get("commentEdits", {})
    comment_deletes = shadow.get("commentDeletes", [])
    field_count = len(fields) if isinstance(fields, dict) else 0
    comment_count = (
        (len(comments) if isinstance(comments, list) else 0)
        + (len(comment_edits) if isinstance(comment_edits, dict) else 0)
        + (len(comment_deletes) if isinstance(comment_deletes, list) else 0)
    )
    lines = [
        "Local shadow",
        "------------",
        f"State: {display_name(shadow.get('state')) or 'working'}",
        f"Base updated: {display_name(shadow.get('baseUpdated')) or '(unknown)'}",
        f"Fields: {field_count}",
        f"Comments: {comment_count}",
    ]
    status_change = as_dict(shadow.get("statusChange"))
    resolution = display_name(status_change.get("resolution"))
    if resolution:
        lines.append(f"Resolution: {resolution}")
    return lines


def shadow_change_summary(shadow: dict[str, Any], jira_dir: Path, key: str) -> list[str]:
    """Compact, accurate "what's pending" line for the TUI detail header.

    Unlike format_shadow_summary (kept as-is for the plain-text CLI report),
    this names the changed fields and breaks comments down by what actually
    happened to each one, since a bare count can't tell an added comment
    from an edited or deleted one -- and skips the shadow "state" marker,
    which only means something after the CLI-only `shadow commit` step that
    the TUI never uses.
    """
    parts: list[str] = []

    fields = as_dict(shadow.get("fields"))
    if fields:
        names = ", ".join(report_field_label(jira_dir, field) for field in sorted(fields))
        parts.append(f"{len(fields)} field{'s' if len(fields) != 1 else ''} ({names})")

    rows = list_comments(jira_dir, key, shadow=shadow)
    counts = {"local-new": 0, "edited": 0, "pending-delete": 0}
    for row in rows:
        if row["state"] in counts:
            counts[row["state"]] += 1
    for state, label in (("local-new", "added"), ("edited", "edited"), ("pending-delete", "deleted")):
        count = counts[state]
        if count:
            parts.append(f"{count} comment{'s' if count != 1 else ''} {label}")

    status_change = as_dict(shadow.get("statusChange"))
    resolution = display_name(status_change.get("resolution"))
    if resolution:
        parts.append(f"resolution: {resolution}")

    state = display_name(shadow.get("state"))
    if state and state != "working":
        parts.append(f"shadow state: {state}")

    if not parts:
        return []
    return [
        f"Local changes: {'; '.join(parts)}",
        f"Base updated: {display_name(shadow.get('baseUpdated')) or '(unknown)'}",
    ]


def format_work_item(
    jira_dir: Path,
    key: str,
    *,
    component_field: str | None = None,
    mode: str = "shadow",
) -> str:
    if mode == "diff":
        return render_diff(jira_dir, key) + "\n"
    issue = load_issue(jira_dir, key)
    shadow = None if mode == "original" else load_shadow(jira_dir, key)
    return format_issue(issue, jira_dir=jira_dir, component_field=component_field, shadow=shadow)


def normalized_initial_key(jira_dir: Path, key: str | None) -> str | None:
    if not key:
        return None
    issue = load_issue(jira_dir, key)
    return display_name(issue.get("key")) or key


def modified_issue_keys(jira_dir: Path) -> set[str]:
    components_dir = jira_dir / "components"
    if not components_dir.exists():
        return set()
    return {path.parent.name for path in components_dir.glob("*/*/shadow.json")}


def issue_key_from_text(value: str) -> str | None:
    match = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", value)
    return match.group(0) if match else None


def report_empty_value(field: str) -> str:
    return "(unassigned)" if field == "assignee" else "(none)"


def report_value(field: str, value: Any) -> str:
    if field == "parent":
        if value is None:
            return report_empty_value(field)
        parent = as_dict(value)
        if "key" in parent:
            return display_name(parent.get("key")) or report_empty_value(field)
    if field == "description":
        text = text_from_adf(value)
        return text if text else report_empty_value(field)
    text = display_name(value)
    return text if text else report_empty_value(field)


def original_report_value(issue: dict[str, Any], field: str) -> str:
    fields = as_dict(issue.get("fields"))
    if field == "type":
        return report_value(field, fields.get("issuetype"))
    if field == "parent":
        return report_value(field, fields.get("parent"))
    return report_value(field, fields.get(field))


def shadow_report_value(field: str, value: Any) -> str:
    return report_value(field, value)


def report_text_block(label: str, value: str) -> list[str]:
    lines = [f"  {label}:"]
    value_lines = value.splitlines()
    if not value_lines:
        return [*lines, "    (empty)"]
    lines.extend(f"    {line}" if line else "    " for line in value_lines)
    return lines


def detailed_shadow_report_lines(jira_dir: Path, keys: list[str]) -> list[str]:
    lines = [
        f"Local shadow report: {len(keys)} item{'s' if len(keys) != 1 else ''}",
        "",
    ]
    for key in keys:
        shadow = load_shadow(jira_dir, key)
        if shadow is None:
            continue
        issue = load_issue(jira_dir, key)
        fields = as_dict(shadow.get("fields"))
        comments = as_list(shadow.get("comments"))
        state = display_name(shadow.get("state")) or "working"
        original_summary = original_report_value(issue, "summary")
        shadow_summary = shadow_report_value("summary", fields["summary"]) if "summary" in fields else None

        lines.append(f"{key} ({state})")
        if shadow_summary is not None and shadow_summary != original_summary:
            lines.append(f"Summary: {original_summary} -> {shadow_summary}")
        else:
            lines.append(f"Summary: {original_summary}")
        lines.append(f"Fields: {len(fields)}  Comments: {len(comments)}  Base updated: {shadow.get('baseUpdated')}")

        if fields:
            lines.append("Changes:")
            for field in sorted(fields):
                label = report_field_label(jira_dir, field)
                if field == "fixVersions":
                    names = resolve_fix_version_names(jira_dir, as_dict(issue.get("fields")).get("fixVersions"))
                    before = ", ".join(names) if names else report_empty_value(field)
                else:
                    before = original_report_value(issue, field)
                after = shadow_report_value(field, fields[field])
                if field == "description":
                    lines.append(f"- {label}:")
                    lines.extend(report_text_block("Before", before))
                    lines.extend(report_text_block("After", after))
                else:
                    lines.append(f"- {label}: {before} -> {after}")

        status_change = as_dict(shadow.get("statusChange"))
        resolution = status_change.get("resolution")
        if resolution:
            before = original_report_value(issue, "resolution")
            after = shadow_report_value("resolution", resolution)
            if not fields:
                lines.append("Changes:")
            lines.append(f"- resolution: {before} -> {after}")

        if comments:
            lines.append("Comments:")
            for comment in comments:
                if isinstance(comment, dict):
                    lines.append(f"- [{display_name(comment.get('state')) or 'working'}] {comment.get('body', '')}")

        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()
    return lines



