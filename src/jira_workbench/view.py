from __future__ import annotations

import curses
import curses.textpad
import copy
import json
import re
import textwrap
from pathlib import Path
from typing import Any

from .metadata import JiraApiConfig, MetadataError, jira_api_client, load_versions, version_name
from .shadow import (
    ShadowError,
    add_comment,
    delete_shadow,
    load_shadow,
    push_key,
    push_shadows,
    render_diff,
    set_field,
    set_status_change,
)
from .sync import find_existing_issue, issue_key_sort_key, issue_path_sort_key, read_json


class ViewError(RuntimeError):
    pass


DONE_STATUSES = {"close", "closed", "done", "resolved"}
DEFAULT_RESOLUTIONS = ["Done", "Won't Do", "Duplicate", "Cannot Reproduce"]
INDEX_KEY_WIDTH = 9
INDEX_STATE_WIDTH = 12
INDEX_COMPONENT_WIDTH = 14
INDEX_PARENT_WIDTH = 12
VIRTUAL_NONE = "(none)"
SWIMLANE_MODES = ("none", "epic", "version", "component")
DESCRIPTION_PREVIEW_LINES = 8
COMMENTS_PREVIEW_LINES = 6
INLINE_REPORT_FIELDS = {
    "assignee",
    "fixVersions",
    "parent",
    "priority",
    "resolution",
    "status",
    "type",
}
REPORT_FIELD_LABELS = {
    "assignee": "assignee",
    "fixVersions": "version",
    "parent": "parent",
    "priority": "priority",
    "resolution": "resolution",
    "status": "status",
    "type": "type",
}
CTRL_P = 16
CTRL_V = 22
IGNORED_CONTROL_KEYS = {CTRL_P, CTRL_V}
DetailSegment = tuple[str, int]
DetailLine = list[DetailSegment]

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

HELP_LINES = [
    "Jira Workbench Help",
    "",
    "Index",
    "  j/k, arrows     Move selection",
    "  Enter           Open selected work item",
    "  v               View a work item by key",
    "  V               Open selected item's parent",
    "  g               Go to matching row",
    "  /               Search rows",
    "  n / N           Repeat search forward/backward",
    "  a               Toggle active-only filter",
    "  [ and ]         Cycle component filter",
    "  c               Type component filter",
    "  C               Clear component filter",
    "  f               Type text filter",
    "  F               Select fixVersion filter",
    "  < and >         Cycle fixVersion filter",
    "  x               Clear fixVersion filter",
    "  S               Cycle swimlane grouping",
    "  R               Reload local list",
    "  \\               Clear text filter",
    "  m               Toggle modified-only filter",
    "  s               Toggle item second row",
    "  w               Toggle summary wrapping",
    "  P               Push all local shadow changes",
    "  h               Show or hide this help",
    "  q or Esc        Quit",
    "",
    "Issue",
    "  Tab / Shift-Tab Cycle editable fields",
    "  s               Show local shadow view",
    "  o               Show original synced Jira issue",
    "  d               Show local shadow diff",
    "  e               Edit a field into local shadow",
    "  Enter           Edit highlighted field",
    "  c               Add a local shadow comment",
    "  r               Revert this item's local shadow",
    "  p               Push this item's shadow to Jira",
    "  O               Toggle Other fields",
    "  x               Expand or collapse selected text block",
    "  j/k, arrows     Scroll",
    "  Space           Page down",
    "  b               Page up",
    "  h               Show or hide this help",
    "  v               View a work item by key",
    "  V               Open this item's parent",
    "  Esc             Back to index",
    "  q               Quit",
    "",
    "Filters",
    "  Active hides statuses: Close, Closed, Done, Resolved",
    "  --components lists component names before entering curses",
    "  --component starts with a component filter",
    "  --all starts with inactive items included",
]


def is_active_item(item: dict[str, Any]) -> bool:
    return display_name(item.get("status")).strip().lower() not in DONE_STATUSES


def matches_component(item: dict[str, Any], component: str | None) -> bool:
    if not component:
        return True
    return display_name(item.get("component")).strip().lower() == component.strip().lower()


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
    return display_name(item.get("fixVersion")).strip()


def matches_fix_version(item: dict[str, Any], fix_version: str | None) -> bool:
    if not fix_version:
        return True
    value = item_fix_version(item)
    if fix_version == "(none)":
        return not value
    return value.lower() == fix_version.strip().lower()


def filter_items(
    items: list[dict[str, Any]],
    *,
    component: str | None = None,
    pattern: str | None = None,
    active: bool = True,
    modified_keys: set[str] | None = None,
    modified_only: bool = False,
    fix_version: str | None = None,
) -> list[dict[str, Any]]:
    modified_keys = modified_keys or set()
    return [
        item
        for item in items
        if matches_component(item, component)
        and matches_filter(item, pattern)
        and matches_fix_version(item, fix_version)
        and (not active or is_active_item(item))
        and (not modified_only or display_name(item.get("key")) in modified_keys)
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


def component_counts(items: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for item in items:
        component = display_name(item.get("component")) or "_unassigned"
        counts[component] = counts.get(component, 0) + 1
    return sorted(counts.items(), key=lambda row: row[0].lower())


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
    return None


def swimlane_sort_key(item: dict[str, Any], swimlane: str | None) -> tuple[Any, ...]:
    mode = normalize_swimlane(swimlane)
    lane = swimlane_label(item, mode) or ""
    none_rank = 0 if lane == VIRTUAL_NONE else 1
    if mode == "epic" and lane != VIRTUAL_NONE:
        lane_key = display_name(item.get("key") if is_epic_item(item) else item.get("epic"))
        item_rank = 0 if is_epic_item(item) else 1
        return (none_rank, issue_key_sort_key(lane_key), item_rank, issue_key_sort_key(display_name(item.get("key"))))
    return (none_rank, lane.lower(), issue_key_sort_key(display_name(item.get("key"))))


def is_epic_item(item: dict[str, Any]) -> bool:
    return display_name(item.get("type")).strip().lower() == "epic"


def filter_items_for_swimlane(items: list[dict[str, Any]], swimlane: str | None) -> list[dict[str, Any]]:
    return items


def sort_items_for_swimlane(items: list[dict[str, Any]], swimlane: str | None) -> list[dict[str, Any]]:
    if normalize_swimlane(swimlane) == "none":
        return items
    return sorted(items, key=lambda item: swimlane_sort_key(item, swimlane))


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


def text_from_adf(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
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


def load_manifest_items(jira_dir: Path, component_field: str | None = None) -> list[dict[str, Any]]:
    manifest_path = jira_dir / "manifest.json"
    if not manifest_path.exists():
        raise ViewError(f"manifest not found under {jira_dir}. Run jira-wb sync first.")
    manifest = read_json(manifest_path)
    items = manifest.get("workItems") if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        raise ViewError(f"manifest at {manifest_path} does not contain workItems")
    return sorted(
        (with_local_index_fields(jira_dir, item, component_field) for item in items if isinstance(item, dict)),
        key=lambda item: issue_key_sort_key(display_name(item.get("key"))),
    )


def with_local_index_fields(
    jira_dir: Path,
    item: dict[str, Any],
    component_field: str | None = None,
) -> dict[str, Any]:
    enriched = dict(item)
    key = display_name(item.get("key"))
    if not key:
        return enriched
    path = find_existing_issue(jira_dir / "components", key)
    if path is None:
        return enriched
    issue = read_json(path)
    if not isinstance(issue, dict):
        return enriched
    shadow = load_shadow(jira_dir, key)
    if shadow is not None:
        issue = apply_shadow(issue, shadow)
    fields = as_dict(issue.get("fields"))
    issue_type = fields.get("issuetype")
    status = fields.get("status")
    fix_versions = as_list(fields.get("fixVersions"))
    parent = as_dict(fields.get("parent"))
    parent_fields = as_dict(parent.get("fields"))
    enriched["summary"] = display_name(fields.get("summary"))
    enriched["status"] = display_name(status)
    enriched["type"] = display_name(issue_type)
    enriched["component"] = hierarchy_component(issue, component_field) or display_name(item.get("component"))
    enriched["fixVersion"] = display_name(fix_versions[0]) if fix_versions else ""
    enriched["priority"] = display_name(fields.get("priority"))
    enriched["assignee"] = display_name(fields.get("assignee"))
    enriched["epic"] = display_name(parent.get("key"))
    enriched["epicSummary"] = display_name(parent_fields.get("summary"))
    return enriched


def refresh_index_item(
    jira_dir: Path,
    items: list[dict[str, Any]],
    key: str,
    component_field: str | None = None,
) -> None:
    for index, item in enumerate(items):
        if display_name(item.get("key")) == key:
            items[index] = with_local_index_fields(jira_dir, item, component_field)
            return


def refresh_stale_index_items(
    jira_dir: Path,
    items: list[dict[str, Any]],
    keys: set[str],
    component_field: str | None = None,
) -> None:
    for key in sorted(keys, key=issue_key_sort_key):
        refresh_index_item(jira_dir, items, key, component_field)
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


def field_section(fields: dict[str, Any], component_field: str | None) -> list[str]:
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
    sorted_options = sorted(options, key=str.lower)
    if include_empty:
        return [include_empty, *sorted_options]
    return sorted_options


def component_options(jira_dir: Path) -> list[str]:
    cached = jira_dir / "meta" / "components.json"
    if cached.exists():
        value = read_json(cached)
        components = as_list(as_dict(value).get("components"))
        names = sorted({display_name(component).strip() for component in components if display_name(component).strip()})
        if names:
            return names
    return [component for component, _ in component_counts(load_manifest_items(jira_dir))]


def is_active_version(version: dict[str, Any]) -> bool:
    return not version.get("archived") and not version.get("released")


def version_options(jira_dir: Path, *, include_inactive: bool = False) -> list[str]:
    cache = load_versions(jira_dir)
    versions = as_list(as_dict(cache).get("versions"))
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
    return display_name(as_dict(fields.get("status")).get("name") or fields.get("status")).strip().lower() not in DONE_STATUSES


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


def detail_field_rows(
    issue: dict[str, Any],
    component_field: str | None,
    comments: str | None = None,
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
    return display_name(body)


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


def editable_detail_fields(component_field: str | None = None) -> set[str]:
    return {field for _, field in editable_field_choices(component_field)}


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
    lines.extend(field_section(fields, component_field))
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
    status_change = as_dict(shadow.get("statusChange"))
    return bool(fields or comments or status_change)


def format_shadow_summary(shadow: dict[str, Any]) -> list[str]:
    fields = shadow.get("fields", {})
    comments = shadow.get("comments", [])
    field_count = len(fields) if isinstance(fields, dict) else 0
    comment_count = len(comments) if isinstance(comments, list) else 0
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


def item_label(item: dict[str, Any]) -> str:
    return index_row_lines(item, width=120, wrap=False)[0]


def truncate_cell(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + ">"


def index_prefix(
    item: dict[str, Any],
    modified_keys: set[str] | None = None,
    *,
    swimlane: str | None = None,
) -> str:
    modified_keys = modified_keys or set()
    key_value = display_name(item.get("key"))
    key = truncate_cell(f"{key_value}*" if key_value in modified_keys else key_value, INDEX_KEY_WIDTH)
    state = truncate_cell(display_name(item.get("status")), INDEX_STATE_WIDTH)
    component_value = "" if normalize_swimlane(swimlane) == "component" else display_name(item.get("component"))
    component = truncate_cell(component_value, INDEX_COMPONENT_WIDTH)
    return (
        f"{key:<{INDEX_KEY_WIDTH}}  "
        f"{state:<{INDEX_STATE_WIDTH}}  "
        f"{component:<{INDEX_COMPONENT_WIDTH}}  "
    )


def index_header(width: int = 120, swimlane: str | None = None, show_second_row: bool = True) -> str:
    mode = normalize_swimlane(swimlane)
    assignee_width, version_width = metadata_flex_widths(width)
    component_label = "" if mode == "component" else "Component"
    parent_label = "" if mode == "epic" else "Parent"
    version_label = "" if mode == "version" else "Version"
    line1 = (
        f"{'ID':<{INDEX_KEY_WIDTH}}  "
        f"{'State':<{INDEX_STATE_WIDTH}}  "
        f"{component_label:<{INDEX_COMPONENT_WIDTH}}  "
        "Summary"
    )
    if not show_second_row:
        return line1
    line2 = (
        f"{parent_label:<{INDEX_PARENT_WIDTH}}  "
        f"{'Priority':<{INDEX_STATE_WIDTH}}  "
        f"{'Assignee':<{assignee_width}}  "
        f"{version_label:<{version_width}}"
    )
    return f"{line1}\n{line2}"


def index_row_lines(
    item: dict[str, Any],
    *,
    width: int,
    wrap: bool,
    modified_keys: set[str] | None = None,
    swimlane: str | None = None,
    show_second_row: bool = True,
) -> list[str]:
    prefix = index_prefix(item, modified_keys, swimlane=swimlane)
    summary_width = max(1, width - len(prefix))
    summary = display_name(item.get("summary"))
    metadata_line = index_metadata_line(item, width=width, swimlane=swimlane)
    if not wrap:
        lines = [prefix + truncate_cell(summary, summary_width)]
        if show_second_row:
            lines.append(metadata_line)
        return lines
    wrapped = textwrap.wrap(summary, width=summary_width, replace_whitespace=False) or [""]
    lines = [prefix + wrapped[0], *(f"{'':<{len(prefix)}}{line}" for line in wrapped[1:])]
    if show_second_row:
        lines.append(metadata_line)
    return lines


def index_metadata_line(item: dict[str, Any], *, width: int, swimlane: str | None = None) -> str:
    mode = normalize_swimlane(swimlane)
    parent_key = "" if mode == "epic" else display_name(item.get("epic"))
    parent = truncate_cell(f"-> {parent_key}" if parent_key else "", INDEX_PARENT_WIDTH)
    priority = truncate_cell(display_name(item.get("priority")), INDEX_STATE_WIDTH)
    assignee_width, version_width = metadata_flex_widths(width)
    assignee = truncate_cell(display_name(item.get("assignee")), assignee_width)
    version_value = "" if mode == "version" else display_name(item.get("fixVersion"))
    version = truncate_cell(version_value, version_width)
    prefix = (
        f"{parent:<{INDEX_PARENT_WIDTH}}  "
        f"{priority:<{INDEX_STATE_WIDTH}}  "
        f"{assignee:<{assignee_width}}  "
    )
    return prefix + version


def metadata_flex_widths(width: int) -> tuple[int, int]:
    fixed = INDEX_PARENT_WIDTH + 2 + INDEX_STATE_WIDTH + 2
    remaining = max(2, width - fixed)
    assignee_width = max(1, remaining // 2)
    version_width = max(1, remaining - assignee_width - 2)
    return assignee_width, version_width


def wrap_lines(lines: list[str], width: int) -> list[str]:
    if width <= 0:
        return lines
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
        else:
            wrapped.extend(textwrap.wrap(line, width=width, replace_whitespace=False) or [""])
    return wrapped


def wrap_push_lines(lines: list[str], width: int) -> list[str]:
    if width <= 0:
        return lines
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        chunks = textwrap.wrap(
            line,
            width=width,
            subsequent_indent="  ",
            replace_whitespace=False,
        )
        wrapped.extend(chunks or [""])
    return wrapped


def is_push_all_key(key: int) -> bool:
    return key == ord("P")


def is_open_parent_key(key: int) -> bool:
    return key == ord("V")


def interactive_view(
    jira_dir: Path,
    *,
    component_field: str | None = None,
    component: str | None = None,
    pattern: str | None = None,
    active: bool = True,
    jira_url: str | None = None,
    jira_email: str | None = None,
    jira_api_token: str | None = None,
    initial_key: str | None = None,
    initial_mode: str = "shadow",
    swimlane: str | None = None,
) -> None:
    items = load_manifest_items(jira_dir, component_field)
    curses.wrapper(
        _interactive_view,
        jira_dir,
        items,
        component_field,
        component,
        pattern,
        active,
        jira_url,
        jira_email,
        jira_api_token,
        initial_key,
        initial_mode,
        normalize_swimlane(swimlane),
    )


def _interactive_view(
    stdscr: Any,
    jira_dir: Path,
    items: list[dict[str, Any]],
    component_field: str | None,
    component: str | None,
    pattern: str | None,
    active: bool,
    jira_url: str | None,
    jira_email: str | None,
    jira_api_token: str | None,
    initial_key: str | None = None,
    initial_mode: str = "shadow",
    swimlane: str | None = None,
) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    except curses.error:
        pass
    selected = 0
    top = 0
    detail_key: str | None = normalized_initial_key(jira_dir, initial_key)
    detail_top = 0
    detail_selected = 0
    detail_mode = initial_mode if initial_mode in {"shadow", "original", "diff"} else "shadow"
    current_component = component
    current_filter = pattern
    active_only = active
    show_help = False
    help_top = 0
    wrap_index = False
    show_index_second_row = True
    search_query: str | None = None
    message: str | None = None
    show_other_fields = False
    expanded_text_fields: set[str] = set()
    modified_only = False
    current_fix_version: str | None = None
    current_swimlane = normalize_swimlane(swimlane)
    stale_index_keys: set[str] = set()

    while True:
        if not detail_key and stale_index_keys:
            refresh_stale_index_items(jira_dir, items, stale_index_keys, component_field)
        modified_keys = modified_issue_keys(jira_dir)
        visible_items = filter_items(
            items,
            component=current_component,
            pattern=current_filter,
            fix_version=current_fix_version,
            active=active_only,
            modified_keys=modified_keys,
            modified_only=modified_only,
        )
        visible_items = filter_items_for_swimlane(visible_items, current_swimlane)
        visible_items = sort_items_for_swimlane(visible_items, current_swimlane)
        if selected >= len(visible_items):
            selected = max(0, len(visible_items) - 1)
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        if show_help:
            help_top = draw_help(stdscr, help_top, height, width)
        elif detail_key:
            detail_top = draw_detail(
                stdscr,
                jira_dir,
                detail_key,
                component_field,
                detail_mode,
                detail_top,
                detail_selected,
                height,
                width,
                message,
                show_other_fields,
                expanded_text_fields,
            )
        else:
            selected, top = draw_index(
                stdscr,
                visible_items,
                selected,
                top,
                height,
                width,
                component=current_component,
                pattern=current_filter,
                fix_version=current_fix_version,
                active=active_only,
                wrap=wrap_index,
                modified_keys=modified_keys,
                modified_only=modified_only,
                swimlane=current_swimlane,
                show_second_row=show_index_second_row,
                message=message,
            )
        stdscr.refresh()

        key = stdscr.getch()
        message = None
        if key == ord("q"):
            return
        if show_help:
            if key in (ord("h"), 27):
                show_help = False
                help_top = 0
            elif key in (curses.KEY_UP, ord("k")):
                help_top = max(0, help_top - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                help_top += 1
            elif key in (curses.KEY_NPAGE, ord(" ")):
                help_top += max(1, height - 2)
            elif key in (curses.KEY_PPAGE, ord("b")):
                help_top = max(0, help_top - max(1, height - 2))
        elif key in IGNORED_CONTROL_KEYS:
            message = "ignored control key; use V for parent, Shift-P for push all"
        elif key == ord("h"):
            show_help = True
            help_top = 0
        elif key == ord("v"):
            chosen_key = prompt_work_item_key(stdscr, detail_key)
            if chosen_key:
                try:
                    issue = load_issue(jira_dir, chosen_key)
                except ViewError:
                    pass
                else:
                    detail_key = display_name(issue.get("key")) or chosen_key
                    detail_mode = "shadow"
                    detail_top = 0
                    detail_selected = 0
        elif key == ord("g") and not detail_key:
            query = prompt_goto(stdscr)
            if query:
                match = find_item_index(visible_items, query)
                if match is not None:
                    selected = match
                    top = selected
                    search_query = query
        elif key == 27:
            if detail_key:
                stale_index_keys.add(detail_key)
                detail_key = None
                detail_top = 0
                detail_selected = 0
            else:
                return
        elif detail_key:
            detail_issue = load_issue(jira_dir, detail_key)
            detail_shadow = None if detail_mode == "original" else load_shadow(jira_dir, detail_key)
            detail_effective = apply_shadow(detail_issue, detail_shadow) if detail_shadow is not None else detail_issue
            field_rows = detail_field_rows(
                detail_effective,
                component_field,
                comments_text(jira_dir, detail_key, detail_shadow),
            )
            editable_fields = editable_detail_fields(component_field)
            selectable_rows = [index for index, (_, field, _) in enumerate(field_rows) if field in editable_fields]
            if detail_selected >= len(field_rows):
                detail_selected = max(0, len(field_rows) - 1)
            selected_field = field_rows[detail_selected][1] if 0 <= detail_selected < len(field_rows) else ""
            if key == curses.KEY_MOUSE:
                direction = mouse_scroll_direction()
                if direction < 0:
                    detail_top = max(0, detail_top - 3)
                elif direction > 0:
                    detail_top += 3
            elif key == curses.KEY_UP and selected_field in expanded_text_fields:
                detail_top = max(0, detail_top - 1)
            elif key == curses.KEY_DOWN and selected_field in expanded_text_fields:
                detail_top += 1
            elif key in (curses.KEY_UP, ord("k"), curses.KEY_BTAB):
                detail_selected = previous_selectable_index(selectable_rows, detail_selected)
                detail_top = 0
            elif key in (curses.KEY_DOWN, ord("j"), 9):
                detail_selected = next_selectable_index(selectable_rows, detail_selected)
                detail_top = 0
            elif key in (curses.KEY_NPAGE, ord(" ")):
                detail_top += max(1, height - 2)
            elif key in (curses.KEY_PPAGE, ord("b")):
                detail_top = max(0, detail_top - max(1, height - 2))
            elif key == ord("x"):
                if 0 <= detail_selected < len(field_rows):
                    field = field_rows[detail_selected][1]
                    if field in {"description", "comments"}:
                        if field in expanded_text_fields:
                            expanded_text_fields.remove(field)
                            message = f"{field} collapsed"
                        else:
                            expanded_text_fields.add(field)
                            message = f"{field} expanded"
                    else:
                        message = "select Description or Comments to expand"
            elif key == ord("d"):
                detail_mode = "diff"
                detail_top = 0
            elif key == ord("o"):
                detail_mode = "original"
                detail_top = 0
            elif key == ord("s"):
                detail_mode = "shadow"
                detail_top = 0
            elif is_open_parent_key(key):
                parent_key = issue_parent_key(detail_effective)
                if not parent_key:
                    message = "no parent on this item"
                else:
                    try:
                        parent_issue = load_issue(jira_dir, parent_key)
                    except ViewError as exc:
                        message = str(exc)
                    else:
                        detail_key = display_name(parent_issue.get("key")) or parent_key
                        detail_mode = "shadow"
                        detail_top = 0
                        detail_selected = 0
                        show_other_fields = False
                        expanded_text_fields.clear()
            elif key in (ord("e"), 10, 13, curses.KEY_ENTER):
                changed_field = edit_selected_issue_field(
                    stdscr,
                    jira_dir,
                    detail_key,
                    component_field,
                    detail_selected,
                    jira_url,
                    jira_email,
                    jira_api_token,
                )
                if changed_field:
                    refresh_index_item(jira_dir, items, detail_key, component_field)
                    stale_index_keys.add(detail_key)
                    detail_mode = "shadow"
                    detail_top = 0
                    message = f"shadow updated: {changed_field}"
            elif key == ord("c"):
                comment = prompt_textbox(stdscr, "Comment", "")
                if comment:
                    add_comment(jira_dir, detail_key, comment)
                    refresh_index_item(jira_dir, items, detail_key, component_field)
                    stale_index_keys.add(detail_key)
                    detail_mode = "shadow"
                    detail_top = 0
                    message = "comment added to shadow"
            elif key == ord("r"):
                if confirm_simple(stdscr, f"Revert local shadow for {detail_key}?"):
                    delete_shadow(jira_dir, detail_key)
                    refresh_index_item(jira_dir, items, detail_key, component_field)
                    stale_index_keys.add(detail_key)
                    detail_mode = "shadow"
                    detail_top = 0
                    message = "local shadow reverted"
            elif key == ord("p"):
                if not (jira_url and jira_email and jira_api_token):
                    message = "cannot push: missing Jira API configuration"
                elif confirm_simple(stdscr, f"Push shadow for {detail_key} to Jira?"):
                    try:
                        api_client = jira_api_client(JiraApiConfig(jira_url, jira_email, jira_api_token))
                        result = push_key(
                            jira_dir,
                            detail_key,
                            api_client,
                            progress=None,
                            component_field=component_field or "components",
                        )
                    except (MetadataError, ShadowError) as exc:
                        message = f"push failed: {exc}"
                    else:
                        refresh_index_item(jira_dir, items, detail_key, component_field)
                        stale_index_keys.add(detail_key)
                        detail_mode = "shadow"
                        detail_top = 0
                        message = f"push result: {result}"
            elif key == ord("O"):
                show_other_fields = not show_other_fields
                message = "Other fields expanded" if show_other_fields else "Other fields collapsed"
        else:
            max_selected = max(0, len(visible_items) - 1)
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(max_selected, selected + 1)
            elif key in (curses.KEY_NPAGE, ord(" ")):
                selected = min(max_selected, selected + max(1, height - 2))
            elif key in (curses.KEY_PPAGE, ord("b")):
                selected = max(0, selected - max(1, height - 2))
            elif key == ord("a"):
                active_only = not active_only
                selected = 0
                top = 0
            elif key == ord("w"):
                wrap_index = not wrap_index
                top = selected
            elif key == ord("m"):
                modified_only = not modified_only
                selected = 0
                top = 0
            elif key == ord("s"):
                show_index_second_row = not show_index_second_row
                top = selected
            elif key == ord("S"):
                current_swimlane = cycle_swimlane(current_swimlane)
                selected = 0
                top = 0
            elif key == ord("R"):
                selected_key = (
                    display_name(visible_items[selected].get("key"))
                    if visible_items and 0 <= selected < len(visible_items)
                    else None
                )
                items = load_manifest_items(jira_dir, component_field)
                stale_index_keys.clear()
                refreshed_visible_items = sort_items_for_swimlane(
                    filter_items_for_swimlane(
                        filter_items(
                            items,
                            component=current_component,
                            pattern=current_filter,
                            fix_version=current_fix_version,
                            active=active_only,
                            modified_keys=modified_issue_keys(jira_dir),
                            modified_only=modified_only,
                        ),
                        current_swimlane,
                    ),
                    current_swimlane,
                )
                selected = item_index_by_key(refreshed_visible_items, selected_key) or 0
                top = selected
                message = f"reloaded {len(items)} items"
            elif is_push_all_key(key):
                keys = sorted(modified_issue_keys(jira_dir), key=issue_key_sort_key)
                if not keys:
                    message = "no local shadow changes"
                elif not (jira_url and jira_email and jira_api_token):
                    message = "cannot push: missing Jira API configuration"
                elif confirm_push_all(stdscr, jira_dir, keys):
                    progress_lines: list[str] = []

                    def push_progress(line: str) -> None:
                        progress_lines.append(line)
                        draw_push_all_progress(stdscr, progress_lines)

                    try:
                        draw_push_all_progress(stdscr, ["Preparing Jira API client..."])
                        api_client = jira_api_client(JiraApiConfig(jira_url, jira_email, jira_api_token))
                        push_progress(f"Starting push for {len(keys)} item{'s' if len(keys) != 1 else ''}...")
                        result = push_shadows(
                            jira_dir,
                            keys,
                            api_client,
                            progress=push_progress,
                            component_field=component_field or "components",
                        )
                    except (MetadataError, ShadowError) as exc:
                        message = f"push all failed: {exc}"
                        show_push_all_result(
                            stdscr,
                            "Push All Failed",
                            [*progress_lines, "", message],
                        )
                    else:
                        for changed_key in keys:
                            refresh_index_item(jira_dir, items, changed_key, component_field)
                        message = (
                            f"push all: pushed={result.pushed} "
                            f"blocked={result.blocked} failed={result.failed} skipped={result.skipped}"
                        )
                        show_push_all_result(
                            stdscr,
                            "Push All Complete" if result.failed == 0 else "Push All Completed With Errors",
                            [*progress_lines, "", message],
                        )
            elif key == ord("c"):
                current_component = prompt_component(stdscr, current_component)
                selected = 0
                top = 0
            elif key == ord("/"):
                query = prompt_search(stdscr, search_query)
                if query:
                    search_query = query
                    match = find_next_item_index(visible_items, query, selected - 1)
                    if match is not None:
                        selected = match
                        top = selected
            elif key == ord("n"):
                if search_query:
                    match = find_next_item_index(visible_items, search_query, selected)
                    if match is not None:
                        selected = match
                        top = selected
            elif key == ord("N"):
                if search_query:
                    match = find_next_item_index(visible_items, search_query, selected, direction=-1)
                    if match is not None:
                        selected = match
                        top = selected
            elif key == ord("f"):
                current_filter = prompt_filter(stdscr, current_filter)
                selected = 0
                top = 0
            elif key == ord("F"):
                current_fix_version = select_fix_version_filter(stdscr, jira_dir, current_fix_version)
                selected = 0
                top = 0
            elif key == ord(">"):
                current_fix_version = cycle_fix_version(items, current_fix_version, 1)
                selected = 0
                top = 0
            elif key == ord("<"):
                current_fix_version = cycle_fix_version(items, current_fix_version, -1)
                selected = 0
                top = 0
            elif key == ord("x"):
                current_fix_version = None
                selected = 0
                top = 0
            elif key == ord("\\"):
                current_filter = None
                selected = 0
                top = 0
            elif key == ord("C"):
                current_component = None
                selected = 0
                top = 0
            elif key == ord("]"):
                current_component = cycle_component(items, current_component, 1)
                selected = 0
                top = 0
            elif key == ord("["):
                current_component = cycle_component(items, current_component, -1)
                selected = 0
                top = 0
            elif is_open_parent_key(key) and visible_items:
                parent_key = index_item_parent_key(visible_items[selected])
                if not parent_key:
                    message = "selected item has no parent"
                else:
                    try:
                        parent_issue = load_issue(jira_dir, parent_key)
                    except ViewError as exc:
                        message = str(exc)
                    else:
                        detail_key = display_name(parent_issue.get("key")) or parent_key
                        detail_mode = "shadow"
                        detail_top = 0
                        detail_selected = 0
                        show_other_fields = False
            elif key in (10, 13, curses.KEY_ENTER) and visible_items:
                detail_key = display_name(visible_items[selected].get("key"))
                detail_mode = "shadow"
                detail_top = 0
                detail_selected = 0


def draw_index(
    stdscr: Any,
    items: list[dict[str, Any]],
    selected: int,
    top: int,
    height: int,
    width: int,
    *,
    component: str | None,
    pattern: str | None,
    fix_version: str | None,
    active: bool,
    wrap: bool,
    modified_keys: set[str] | None = None,
    modified_only: bool = False,
    swimlane: str | None = None,
    show_second_row: bool = True,
    message: str | None = None,
) -> tuple[int, int]:
    header_lines = index_header(width - 1, swimlane=swimlane, show_second_row=show_second_row).splitlines()
    message_height = 1 if message else 0
    visible_height = max(1, height - 2 - len(header_lines) - message_height)
    if selected < top:
        top = selected

    while top < selected and not index_selection_fits(
        items, selected, top, visible_height, width, wrap, modified_keys, swimlane, show_second_row
    ):
        top += 1

    filters = []
    if component:
        filters.append(f"component={component}")
    if pattern:
        filters.append(f"filter={pattern}")
    if fix_version:
        filters.append(f"fixVersion={fix_version}")
    if active:
        filters.append("active")
    if wrap:
        filters.append("wrap")
    if modified_only:
        filters.append("modified")
    if not show_second_row:
        filters.append("single-row")
    if normalize_swimlane(swimlane) != "none":
        filters.append(f"swimlane={normalize_swimlane(swimlane)}")
    title = index_title(len(items), filters, width)
    stdscr.addnstr(0, 0, title, width - 1, curses.A_BOLD | curses.A_UNDERLINE)
    for header_row, line in enumerate(header_lines, start=1):
        stdscr.addnstr(header_row, 0, line, width - 1, curses.A_BOLD)
    row = 1 + len(header_lines)
    previous_lane: str | None = None
    for index, item in enumerate(items[top:], start=top):
        lane_lines: list[str] = []
        lane = swimlane_label(item, swimlane)
        epic_lane_only = normalize_swimlane(swimlane) == "epic" and is_epic_item(item)
        if lane is not None and lane != previous_lane:
            lane_lines = [swimlane_header(lane, swimlane)]
            previous_lane = lane
        lines = (
            []
            if epic_lane_only
            else index_row_lines(
                item,
                width=width - 1,
                wrap=wrap,
                modified_keys=modified_keys,
                swimlane=swimlane,
                show_second_row=show_second_row,
            )
        )
        if row + len(lane_lines) + len(lines) > height - message_height:
            break
        for line in lane_lines:
            lane_attr = curses.A_REVERSE if epic_lane_only and index == selected else curses.A_BOLD
            stdscr.addnstr(row, 0, line, width - 1, lane_attr)
            row += 1
        attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
        for line in lines:
            stdscr.addnstr(row, 0, line, width - 1, attr)
            row += 1
    if message:
        stdscr.addnstr(height - 1, 0, message, width - 1, curses.A_REVERSE)
    return selected, top


def index_title(item_count: int, filters: list[str], width: int) -> str:
    available = max(1, width - 1)
    base_prefix = f"Jira Workbench: {item_count}"
    base = base_prefix
    if filters:
        filter_text = f" [{' '.join(filters)}]"
        reserved = len("  (h help)")
        filter_width = max(0, available - len(base_prefix) - reserved)
        base += truncate_cell(filter_text, filter_width) if filter_width else ""
    hints = [
        "h help",
        "P push all",
        "Enter open",
        "/ search",
    ]
    title = base
    visible_hints = []
    for hint in hints:
        candidate_hints = "  ".join([*visible_hints, hint])
        candidate = f"{base}  ({candidate_hints})"
        if len(candidate) <= available:
            visible_hints.append(hint)
            title = candidate
        elif not visible_hints and len(base) + 4 <= available:
            title = f"{base}  (h)"
            break
    return truncate_cell(title, available)


def swimlane_header(lane: str, swimlane: str | None) -> str:
    labels = {"epic": "Epic", "version": "Version", "component": "Component"}
    label = labels.get(normalize_swimlane(swimlane), "Swimlane")
    return f"== {label}: {lane} =="


def index_selection_fits(
    items: list[dict[str, Any]],
    selected: int,
    top: int,
    visible_height: int,
    width: int,
    wrap: bool,
    modified_keys: set[str] | None = None,
    swimlane: str | None = None,
    show_second_row: bool = True,
) -> bool:
    row_count = 0
    previous_lane: str | None = None
    for index, item in enumerate(items[top : selected + 1], start=top):
        lane = swimlane_label(item, swimlane)
        if lane is not None and lane != previous_lane:
            row_count += 1
            previous_lane = lane
        if normalize_swimlane(swimlane) == "epic" and is_epic_item(item):
            if row_count > visible_height:
                return False
            if index == selected:
                return True
            continue
        row_count += len(
            index_row_lines(
                item,
                width=width - 1,
                wrap=wrap,
                modified_keys=modified_keys,
                swimlane=swimlane,
                show_second_row=show_second_row,
            )
        )
        if row_count > visible_height:
            return False
        if index == selected:
            return True
    return False


def prompt_component(stdscr: Any, current: str | None) -> str | None:
    height, width = stdscr.getmaxyx()
    prompt = f"Component [{current or 'all'}]: "
    curses.echo()
    try:
        stdscr.addnstr(height - 1, 0, " " * max(0, width - 1), width - 1)
        stdscr.addnstr(height - 1, 0, prompt, width - 1, curses.A_REVERSE)
        value = stdscr.getstr(height - 1, min(len(prompt), max(0, width - 1)), max(0, width - len(prompt) - 1))
    finally:
        curses.noecho()
    decoded = value.decode(errors="ignore").strip()
    return decoded or current


def prompt_work_item_key(stdscr: Any, current: str | None) -> str | None:
    height, width = stdscr.getmaxyx()
    prompt = f"View [{current or ''}]: "
    curses.echo()
    try:
        stdscr.addnstr(height - 1, 0, " " * max(0, width - 1), width - 1)
        stdscr.addnstr(height - 1, 0, prompt, width - 1, curses.A_REVERSE)
        value = stdscr.getstr(height - 1, min(len(prompt), max(0, width - 1)), max(0, width - len(prompt) - 1))
    finally:
        curses.noecho()
    decoded = value.decode(errors="ignore").strip()
    return decoded or current


def prompt_goto(stdscr: Any) -> str | None:
    height, width = stdscr.getmaxyx()
    prompt = "Go to: "
    curses.echo()
    try:
        stdscr.addnstr(height - 1, 0, " " * max(0, width - 1), width - 1)
        stdscr.addnstr(height - 1, 0, prompt, width - 1, curses.A_REVERSE)
        value = stdscr.getstr(height - 1, min(len(prompt), max(0, width - 1)), max(0, width - len(prompt) - 1))
    finally:
        curses.noecho()
    decoded = value.decode(errors="ignore").strip()
    return decoded or None


def prompt_search(stdscr: Any, current: str | None) -> str | None:
    height, width = stdscr.getmaxyx()
    prompt = f"/{current or ''}"
    curses.echo()
    try:
        stdscr.addnstr(height - 1, 0, " " * max(0, width - 1), width - 1)
        stdscr.addnstr(height - 1, 0, prompt, width - 1, curses.A_REVERSE)
        value = stdscr.getstr(height - 1, min(len(prompt), max(0, width - 1)), max(0, width - len(prompt) - 1))
    finally:
        curses.noecho()
    decoded = value.decode(errors="ignore").strip()
    return decoded or current


def prompt_filter(stdscr: Any, current: str | None) -> str | None:
    height, width = stdscr.getmaxyx()
    prompt = f"Filter [{current or 'none'}]: "
    curses.echo()
    try:
        stdscr.addnstr(height - 1, 0, " " * max(0, width - 1), width - 1)
        stdscr.addnstr(height - 1, 0, prompt, width - 1, curses.A_REVERSE)
        value = stdscr.getstr(height - 1, min(len(prompt), max(0, width - 1)), max(0, width - len(prompt) - 1))
    finally:
        curses.noecho()
    decoded = value.decode(errors="ignore").strip()
    return decoded or current


def select_fix_version_filter(stdscr: Any, jira_dir: Path, current: str | None) -> str | None:
    options = version_options(jira_dir, include_inactive=True)
    if "(none)" not in options:
        options.insert(0, "(none)")
    selected = select_field_value(stdscr, "Fix version filter", current or "", options)
    return selected or current


def previous_selectable_index(selectable_rows: list[int], current: int) -> int:
    previous = [index for index in selectable_rows if index < current]
    if previous:
        return previous[-1]
    return selectable_rows[-1] if selectable_rows else current


def next_selectable_index(selectable_rows: list[int], current: int) -> int:
    following = [index for index in selectable_rows if index > current]
    if following:
        return following[0]
    return selectable_rows[0] if selectable_rows else current


def mouse_scroll_direction() -> int:
    try:
        _id, _x, _y, _z, button_state = curses.getmouse()
    except curses.error:
        return 0
    if button_state & getattr(curses, "BUTTON4_PRESSED", 0):
        return -1
    if button_state & getattr(curses, "BUTTON5_PRESSED", 0):
        return 1
    return 0


def edit_selected_issue_field(
    stdscr: Any,
    jira_dir: Path,
    key: str,
    component_field: str | None,
    selected: int,
    jira_url: str | None = None,
    jira_email: str | None = None,
    jira_api_token: str | None = None,
) -> str | None:
    issue = load_issue(jira_dir, key)
    shadow = load_shadow(jira_dir, key)
    effective = apply_shadow(issue, shadow) if shadow is not None else issue
    rows = detail_field_rows(effective, component_field, comments_text(jira_dir, key, shadow))
    editable_fields = editable_detail_fields(component_field)
    if selected >= len(rows):
        return None
    label, field, _ = rows[selected]
    if field not in editable_fields:
        return None
    current = editable_field_value(effective, field)
    if field == "description":
        new_value = prompt_textbox(stdscr, label, current)
    elif field == "parent":
        options = selectable_field_options(jira_dir, field, component_field, current_issue=effective)
        all_options = selectable_field_options(
            jira_dir,
            field,
            component_field,
            current_issue=effective,
            include_inactive_parents=True,
        )
        new_value = select_field_value(
            stdscr,
            label,
            current,
            options,
            all_options=all_options,
            all_label="inactive",
        )
    else:
        options = selectable_field_options(jira_dir, field, component_field)
        if options:
            new_value = select_field_value(stdscr, label, current, options)
        else:
            new_value = prompt_field_value(stdscr, label, current)
    if new_value is None:
        return None
    status_change: tuple[str | None, str | None] | None = None
    if field == "status" and new_value != current:
        if is_done_status(new_value):
            status_change = prompt_status_change(stdscr, new_value)
            if status_change is None:
                return None
        else:
            comment = prompt_optional_status_comment(stdscr)
            status_change = (None, comment)
    set_field(
        jira_dir,
        key,
        field,
        encode_edit_value(field, new_value, component_field=component_field),
    )
    if status_change is not None:
        resolution, comment = status_change
        set_status_change(jira_dir, key, resolution=resolution)
        if comment:
            add_comment(jira_dir, key, comment)
    return label


def is_done_status(value: str) -> bool:
    return value.strip().lower() in DONE_STATUSES


def select_status_resolution(
    stdscr: Any,
    jira_url: str | None,
    jira_email: str | None,
    jira_api_token: str | None,
) -> str | None:
    options = resolution_options(jira_url, jira_email, jira_api_token)
    return select_field_value(stdscr, "Resolution", "Done", options)


def resolution_options(
    jira_url: str | None,
    jira_email: str | None,
    jira_api_token: str | None,
) -> list[str]:
    return DEFAULT_RESOLUTIONS


def prompt_status_change(stdscr: Any, status: str) -> tuple[str, str | None] | None:
    options = resolution_options(None, None, None)
    selected = option_index(options, "Done")
    comment = ""
    focus = "comment"

    try:
        curses.curs_set(1)
        while True:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            stdscr.addnstr(
                0,
                0,
                "Close status  (Tab focus, Ctrl-G save, Esc cancel)",
                width - 1,
                curses.A_REVERSE,
            )
            stdscr.addnstr(2, 2, f"Status:     {status}", width - 3)
            prompt_row = 4
            comment_label = "Comment (optional):"
            comment_attr = curses.A_REVERSE if focus == "comment" else curses.A_NORMAL
            stdscr.addnstr(prompt_row, 2, comment_label, width - 3, comment_attr)
            comment_start = min(22, max(0, width - 2))
            comment_width = max(1, width - comment_start - 1)
            visible_comment = comment[-comment_width:]
            stdscr.addnstr(prompt_row, comment_start, visible_comment, comment_width, comment_attr)

            stdscr.addnstr(6, 2, "Resolution:", width - 3)
            for index, option in enumerate(options):
                row = 7 + index
                if row >= height - 2:
                    break
                prefix = ">" if index == selected else " "
                attr = curses.A_REVERSE if focus == "resolution" and index == selected else curses.A_NORMAL
                stdscr.addnstr(row, 4, f"{prefix} {option}", width - 5, attr)
            if focus == "comment":
                stdscr.move(prompt_row, comment_start + min(len(visible_comment), comment_width - 1))
            else:
                cursor_row = min(7 + selected, height - 2)
                stdscr.move(cursor_row, 6)
            stdscr.refresh()

            key = stdscr.getch()
            if key == 27:
                return None
            if key == 7:
                return options[selected], comment.strip() or None
            if key == 9:
                focus = "resolution" if focus == "comment" else "comment"
                continue
            if key in (10, 13, curses.KEY_ENTER):
                continue
            if key in (curses.KEY_UP,) and focus == "resolution":
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN,) and focus == "resolution":
                selected = min(len(options) - 1, selected + 1)
            elif key in (curses.KEY_BACKSPACE, 8, 127) and focus == "comment":
                comment = comment[:-1]
            elif key == curses.KEY_DC:
                continue
            elif 32 <= key <= 126 and focus == "comment":
                comment += chr(key)
    finally:
        curses.curs_set(0)


def prompt_optional_status_comment(stdscr: Any) -> str | None:
    comment = prompt_field_value(stdscr, "Comment (optional, blank skips)", "")
    return comment.strip() if comment else None


def select_field_value(
    stdscr: Any,
    label: str,
    current: str,
    options: list[str],
    *,
    all_options: list[str] | None = None,
    all_label: str = "all",
) -> str | None:
    active_options = options
    selected = option_index(options, current)
    top = 0
    filter_text = ""
    show_all = False

    while True:
        options = all_options if show_all and all_options is not None else active_options
        visible = [option for option in options if filter_text.lower() in option.lower()]
        if not visible:
            visible = options
            filter_text = ""
        if selected >= len(visible):
            selected = max(0, len(visible) - 1)
        height, width = stdscr.getmaxyx()
        visible_height = max(1, height - 4)
        if selected < top:
            top = selected
        if selected >= top + visible_height:
            top = selected - visible_height + 1

        stdscr.erase()
        toggle_hint = f", a {'hide' if show_all else 'show'} {all_label}" if all_options is not None else ""
        title = f"Select {label}  (j/k move, / filter{toggle_hint}, Enter select, q cancel)"
        stdscr.addnstr(0, 0, title, width - 1, curses.A_REVERSE)
        if filter_text:
            stdscr.addnstr(1, 0, f"filter: {filter_text}", width - 1)
        for row, option in enumerate(visible[top : top + visible_height], start=2):
            index = top + row - 2
            marker = "*" if option == current else " "
            line = f"{marker} {option}"
            attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
            stdscr.addnstr(row, 0, line, width - 1, attr)
        stdscr.refresh()

        pressed = stdscr.getch()
        if pressed in (ord("q"), 27):
            return None
        if pressed in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif pressed in (curses.KEY_DOWN, ord("j")):
            selected = min(len(visible) - 1, selected + 1)
        elif pressed == ord("/"):
            new_filter = prompt_filter_text(stdscr, filter_text)
            if new_filter is not None:
                filter_text = new_filter
                selected = 0
                top = 0
        elif pressed == ord("a") and all_options is not None:
            show_all = not show_all
            selected = option_index(all_options if show_all else active_options, current)
            top = 0
        elif pressed == ord("\\"):
            filter_text = ""
            selected = option_index(options, current)
            top = 0
        elif pressed in (10, 13, curses.KEY_ENTER):
            return visible[selected]


def option_index(options: list[str], current: str) -> int:
    normalized = current.strip().lower()
    current_key = issue_key_from_text(current)
    for index, option in enumerate(options):
        if option.strip().lower() == normalized:
            return index
        if current_key and issue_key_from_text(option) == current_key:
            return index
    return 0


def issue_key_from_text(value: str) -> str | None:
    match = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", value)
    return match.group(0) if match else None


def prompt_filter_text(stdscr: Any, current: str) -> str | None:
    height, width = stdscr.getmaxyx()
    prompt = f"Filter [{current or 'none'}]: "
    curses.echo()
    try:
        curses.curs_set(1)
        stdscr.addnstr(height - 1, 0, " " * max(0, width - 1), width - 1)
        stdscr.addnstr(height - 1, 0, prompt, width - 1, curses.A_REVERSE)
        value = stdscr.getstr(height - 1, min(len(prompt), max(0, width - 1)), max(0, width - len(prompt) - 1))
    finally:
        curses.noecho()
        curses.curs_set(0)
    decoded = value.decode(errors="ignore").strip()
    return decoded


def prompt_field_value(stdscr: Any, label: str, current: str) -> str | None:
    height, width = stdscr.getmaxyx()
    prompt = f"{label}: "
    value = current
    cursor = len(value)
    scroll = 0
    try:
        curses.curs_set(1)
        while True:
            input_x = min(len(prompt), max(0, width - 1))
            input_width = max(1, width - input_x - 1)
            if cursor < scroll:
                scroll = cursor
            if cursor >= scroll + input_width:
                scroll = cursor - input_width + 1
            visible = value[scroll : scroll + input_width]
            stdscr.addnstr(height - 1, 0, " " * max(0, width - 1), width - 1)
            stdscr.addnstr(height - 1, 0, prompt, width - 1, curses.A_BOLD)
            stdscr.addnstr(height - 1, input_x, visible, input_width)
            stdscr.move(height - 1, input_x + min(cursor - scroll, input_width - 1))
            stdscr.refresh()

            key = stdscr.getch()
            value, cursor, done = edit_line_value(value, cursor, key)
            if done == "save":
                return value
            if done == "cancel":
                return None
    finally:
        curses.curs_set(0)


def edit_line_value(value: str, cursor: int, key: int) -> tuple[str, int, str | None]:
    cursor = max(0, min(cursor, len(value)))
    if key == 27:
        return value, cursor, "cancel"
    if key in (10, 13, curses.KEY_ENTER):
        return value, cursor, "save"
    if key in (curses.KEY_LEFT, 2):
        return value, max(0, cursor - 1), None
    if key in (curses.KEY_RIGHT, 6):
        return value, min(len(value), cursor + 1), None
    if key in (curses.KEY_HOME, 1):
        return value, 0, None
    if key in (curses.KEY_END, 5):
        return value, len(value), None
    if key in (curses.KEY_BACKSPACE, 8, 127):
        if cursor == 0:
            return value, cursor, None
        return value[: cursor - 1] + value[cursor:], cursor - 1, None
    if key == curses.KEY_DC:
        if cursor >= len(value):
            return value, cursor, None
        return value[:cursor] + value[cursor + 1 :], cursor, None
    if 32 <= key <= 126:
        return value[:cursor] + chr(key) + value[cursor:], cursor + 1, None
    return value, cursor, None


def prompt_textbox(stdscr: Any, label: str, current: str) -> str | None:
    height, width = stdscr.getmaxyx()
    start_y, start_x, box_height, box_width, rectangle_y2, rectangle_x2 = textbox_geometry(height, width)
    lines = current.splitlines() or [""]
    cursor_y = 0
    cursor_x = 0
    top = 0

    try:
        curses.curs_set(1)
        while True:
            if cursor_y < top:
                top = cursor_y
            if cursor_y >= top + box_height:
                top = cursor_y - box_height + 1
            stdscr.erase()
            stdscr.addnstr(0, 0, f"Edit {label}  (Ctrl-G save, Esc cancel)", width - 1, curses.A_REVERSE)
            stdscr.addnstr(1, 0, "Enter adds a line. Backspace joins lines when at column 0.", width - 1)
            curses.textpad.rectangle(stdscr, start_y - 1, start_x - 1, rectangle_y2, rectangle_x2)
            for row, line in enumerate(lines[top : top + box_height], start=start_y):
                stdscr.addnstr(row, start_x, line, box_width - 1)
            screen_y = start_y + cursor_y - top
            screen_x = start_x + min(cursor_x, box_width - 2)
            stdscr.move(screen_y, screen_x)
            stdscr.refresh()

            key = stdscr.getch()
            if key == 7:
                return "\n".join(lines).rstrip()
            if key == 27:
                return None
            if key == curses.KEY_UP:
                cursor_y = max(0, cursor_y - 1)
                cursor_x = min(cursor_x, len(lines[cursor_y]))
            elif key == curses.KEY_DOWN:
                cursor_y = min(len(lines) - 1, cursor_y + 1)
                cursor_x = min(cursor_x, len(lines[cursor_y]))
            elif key == curses.KEY_LEFT:
                if cursor_x > 0:
                    cursor_x -= 1
                elif cursor_y > 0:
                    cursor_y -= 1
                    cursor_x = len(lines[cursor_y])
            elif key == curses.KEY_RIGHT:
                if cursor_x < len(lines[cursor_y]):
                    cursor_x += 1
                elif cursor_y + 1 < len(lines):
                    cursor_y += 1
                    cursor_x = 0
            elif key in (10, 13, curses.KEY_ENTER):
                line = lines[cursor_y]
                lines[cursor_y] = line[:cursor_x]
                lines.insert(cursor_y + 1, line[cursor_x:])
                cursor_y += 1
                cursor_x = 0
            elif key in (curses.KEY_BACKSPACE, 8, 127):
                if cursor_x > 0:
                    line = lines[cursor_y]
                    lines[cursor_y] = line[: cursor_x - 1] + line[cursor_x:]
                    cursor_x -= 1
                elif cursor_y > 0:
                    previous_length = len(lines[cursor_y - 1])
                    lines[cursor_y - 1] += lines.pop(cursor_y)
                    cursor_y -= 1
                    cursor_x = previous_length
            elif key == curses.KEY_DC:
                line = lines[cursor_y]
                if cursor_x < len(line):
                    lines[cursor_y] = line[:cursor_x] + line[cursor_x + 1 :]
                elif cursor_y + 1 < len(lines):
                    lines[cursor_y] += lines.pop(cursor_y + 1)
            elif 32 <= key <= 126:
                line = lines[cursor_y]
                lines[cursor_y] = line[:cursor_x] + chr(key) + line[cursor_x:]
                cursor_x += 1
    finally:
        curses.curs_set(0)


def textbox_geometry(height: int, width: int) -> tuple[int, int, int, int, int, int]:
    start_y = 2
    start_x = 2
    rectangle_y2 = max(start_y + 2, height - 2)
    rectangle_x2 = max(start_x + 10, width - 2)
    box_height = max(1, rectangle_y2 - start_y)
    box_width = max(10, rectangle_x2 - start_x)
    return start_y, start_x, box_height, box_width, rectangle_y2, rectangle_x2


def confirm_simple(stdscr: Any, prompt: str) -> bool:
    height, width = stdscr.getmaxyx()
    line = f"{prompt} Type y to confirm: "
    curses.echo()
    try:
        curses.curs_set(1)
        stdscr.addnstr(height - 1, 0, " " * max(0, width - 1), width - 1)
        stdscr.addnstr(height - 1, 0, line, width - 1, curses.A_REVERSE)
        value = stdscr.getstr(height - 1, min(len(line), max(0, width - 1)), 1)
    finally:
        curses.noecho()
        curses.curs_set(0)
    return value.decode(errors="ignore").strip().lower() == "y"


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


def report_change_text(jira_dir: Path, key: str, shadow: dict[str, Any]) -> str:
    issue = load_issue(jira_dir, key)
    fields = as_dict(shadow.get("fields"))
    changes: list[str] = []
    for field in sorted(fields):
        label = REPORT_FIELD_LABELS.get(field, field)
        if field in INLINE_REPORT_FIELDS:
            before = original_report_value(issue, field)
            after = shadow_report_value(field, fields[field])
            changes.append(f"{label}: {before} -> {after}")
        else:
            changes.append(label)

    status_change = as_dict(shadow.get("statusChange"))
    resolution = status_change.get("resolution")
    if resolution:
        before = original_report_value(issue, "resolution")
        after = shadow_report_value("resolution", resolution)
        changes.append(f"resolution: {before} -> {after}")

    return ", ".join(changes) if changes else "(comments only)"


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
                label = REPORT_FIELD_LABELS.get(field, field)
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


def shadow_report_lines(jira_dir: Path, keys: list[str]) -> list[str]:
    lines = [
        f"Local shadow changes: {len(keys)} item{'s' if len(keys) != 1 else ''}",
        "",
        "Key       State       Fields  Comments  Changes",
    ]
    for key in keys:
        shadow = load_shadow(jira_dir, key)
        if shadow is None:
            continue
        fields = as_dict(shadow.get("fields"))
        comments = as_list(shadow.get("comments"))
        change_text = report_change_text(jira_dir, key, shadow)
        lines.append(
            f"{key:<9} {display_name(shadow.get('state')) or 'working':<11} "
            f"{len(fields):<7} {len(comments):<9} {change_text}"
        )
    return lines


def push_all_report_lines(jira_dir: Path, keys: list[str]) -> list[str]:
    lines = shadow_report_lines(jira_dir, keys)
    lines[0] = f"Push all local shadow changes: {len(keys)} item{'s' if len(keys) != 1 else ''}"
    lines.extend(
        [
            "",
            "This will push these local shadow changes to live Jira.",
            "Press y to confirm, q or Esc to cancel.",
        ]
    )
    return lines


def confirm_push_all(stdscr: Any, jira_dir: Path, keys: list[str]) -> bool:
    top = 0
    lines = push_all_report_lines(jira_dir, keys)
    while True:
        height, width = stdscr.getmaxyx()
        visible_height = max(1, height - 1)
        top = min(top, max(0, len(lines) - visible_height))
        stdscr.erase()
        stdscr.addnstr(0, 0, "Confirm Push All  (y confirm, q/Esc cancel, j/k scroll)", width - 1, curses.A_REVERSE)
        for row, line in enumerate(lines[top : top + max(1, height - 2)], start=1):
            stdscr.addnstr(row, 0, line, width - 1)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("q"), 27):
            return False
        if key in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            top += 1
        elif key in (curses.KEY_NPAGE, ord(" ")):
            top += max(1, height - 2)
        elif key == curses.KEY_PPAGE:
            top = max(0, top - max(1, height - 2))


def draw_push_all_progress(stdscr: Any, lines: list[str]) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    stdscr.addnstr(0, 0, "Push All  (working...)", width - 1, curses.A_REVERSE)
    wrapped = wrap_push_lines(lines, width - 1)
    visible = wrapped[-max(1, height - 2) :]
    for row, line in enumerate(visible, start=1):
        stdscr.addnstr(row, 0, line, width - 1)
    stdscr.refresh()


def draw_push_all_result(stdscr: Any, title: str, lines: list[str], top: int = 0) -> int:
    height, width = stdscr.getmaxyx()
    wrapped = wrap_push_lines(lines, width - 1)
    top = min(top, max(0, len(wrapped) - max(1, height - 2)))
    stdscr.erase()
    stdscr.addnstr(0, 0, f"{title}  (q/Esc/Enter close, j/k scroll)", width - 1, curses.A_REVERSE)
    for row, line in enumerate(wrapped[top : top + max(1, height - 2)], start=1):
        stdscr.addnstr(row, 0, line, width - 1)
    stdscr.refresh()
    return top


def show_push_all_result(stdscr: Any, title: str, lines: list[str]) -> None:
    top = 0
    while True:
        top = draw_push_all_result(stdscr, title, lines, top)
        key = stdscr.getch()
        if key in (ord("q"), 27, 10, 13):
            return
        if key in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            top += 1
        elif key in (curses.KEY_NPAGE, ord(" ")):
            height, _ = stdscr.getmaxyx()
            top += max(1, height - 2)
        elif key == curses.KEY_PPAGE:
            height, _ = stdscr.getmaxyx()
            top = max(0, top - max(1, height - 2))


def draw_help(stdscr: Any, top: int, height: int, width: int) -> int:
    lines = wrap_lines(HELP_LINES, width - 1)
    top = min(top, max(0, len(lines) - max(1, height - 2)))
    title = "Help  (j/k scroll, h/q/Esc back)"
    stdscr.addnstr(0, 0, title, width - 1, curses.A_BOLD | curses.A_UNDERLINE)
    for row, line in enumerate(lines[top : top + max(1, height - 2)], start=1):
        stdscr.addnstr(row, 0, line, width - 1)
    return top


def draw_detail(
    stdscr: Any,
    jira_dir: Path,
    key: str,
    component_field: str | None,
    mode: str,
    top: int,
    selected: int,
    height: int,
    width: int,
    message: str | None = None,
    show_other_fields: bool = False,
    expanded_text_fields: set[str] | None = None,
) -> int:
    if mode == "diff":
        return draw_detail_text(stdscr, jira_dir, key, component_field, mode, top, height, width, message)
    issue = load_issue(jira_dir, key)
    shadow = None if mode == "original" else load_shadow(jira_dir, key)
    effective = apply_shadow(issue, shadow) if shadow is not None else issue
    rows = detail_field_rows(effective, component_field, comments_text(jira_dir, key, shadow))
    editable_fields = editable_detail_fields(component_field)
    modified_fields = shadow_modified_fields(shadow)
    top = max(0, top)

    fields = as_dict(effective.get("fields"))
    title_text = f"{display_name(effective.get('key'))}  {display_name(fields.get('summary'))}".rstrip()
    modified_marker = " modified" if has_shadow_changes(shadow) else ""
    title = (
        f"{key} [{mode}{modified_marker}]  "
        "(Tab fields, Enter edit, x expand, c comment, V parent, O other, r revert, p push, Esc back, q quit)"
    )
    stdscr.addnstr(0, 0, title, width - 1, curses.A_BOLD | curses.A_UNDERLINE)
    row = 2
    hierarchy = [line for line in hierarchy_section(jira_dir, effective, component_field) if line]
    summary_attr = curses.A_REVERSE if selected == 0 else curses.A_BOLD
    if hierarchy:
        current_key = display_name(effective.get("key"))
        for line in hierarchy:
            is_current_line = (
                line == f"|- {current_key}"
                or line.startswith(f"|- {current_key} ")
                or line == f"Epic: {issue_identity(effective)}"
            )
            attr = summary_attr if is_current_line else (curses.A_BOLD if row == 1 else curses.A_NORMAL)
            stdscr.addnstr(row, 0, line, width - 1, attr)
            row += 1
    else:
        stdscr.addnstr(row, 0, title_text, width - 1, summary_attr)
        row += 1
    if row < height - 1:
        row += 1
    if shadow is not None:
        summary = format_shadow_summary(shadow)
        for line in summary[: min(len(summary), 4)]:
            stdscr.addnstr(row, 0, line, width - 1)
            row += 1
    body_lines = detail_body_line_segments(
        effective,
        component_field,
        rows,
        selected,
        editable_fields,
        modified_fields,
        height,
        width,
        show_other_fields,
        expanded_text_fields or set(),
    )
    body_height = max(1, height - row - 1)
    top = min(top, max(0, len(body_lines) - body_height))
    for line in body_lines[top : top + body_height]:
        draw_detail_segment_line(stdscr, row, line, width)
        row += 1
    if message:
        stdscr.addnstr(height - 1, 0, message, width - 1, curses.A_REVERSE)
    return top


def detail_body_lines(
    issue: dict[str, Any],
    component_field: str | None,
    rows: list[tuple[str, str, str]],
    selected: int,
    editable_fields: set[str],
    modified_fields: set[str],
    height: int,
    width: int,
    show_other_fields: bool,
    expanded_text_fields: set[str] | None = None,
) -> list[tuple[str, int]]:
    return flatten_detail_lines(
        detail_body_line_segments(
            issue,
            component_field,
            rows,
            selected,
            editable_fields,
            modified_fields,
            height,
            width,
            show_other_fields,
            expanded_text_fields,
        )
    )


def detail_body_line_segments(
    issue: dict[str, Any],
    component_field: str | None,
    rows: list[tuple[str, str, str]],
    selected: int,
    editable_fields: set[str],
    modified_fields: set[str],
    height: int,
    width: int,
    show_other_fields: bool,
    expanded_text_fields: set[str] | None = None,
) -> list[DetailLine]:
    lines = detail_field_body_lines(
        rows,
        0,
        selected,
        editable_fields,
        modified_fields,
        height,
        width,
        expanded_text_fields or set(),
    )
    other_rows = other_field_rows(issue, component_field)
    if other_rows:
        lines.append([("", curses.A_NORMAL)])
        suffix = "" if show_other_fields else f" ({len(other_rows)} hidden)"
        lines.append([(f"Other fields{suffix}", curses.A_BOLD)])
        if not show_other_fields:
            lines.append([("  Press O to expand", curses.A_NORMAL)])
        else:
            lines.extend([(f"  {field}: {value}", curses.A_NORMAL)] for field, value in other_rows)
    return lines


def detail_field_body_lines(
    rows: list[tuple[str, str, str]],
    start_index: int,
    selected: int,
    editable_fields: set[str],
    modified_fields: set[str],
    height: int,
    width: int,
    expanded_text_fields: set[str] | None = None,
) -> list[DetailLine]:
    rendered: list[DetailLine] = []
    visible_rows = [(label, field, value) for label, field, value in rows if field != "summary"]
    label_width = max(
        (len(modified_label(label, field, modified_fields)) for label, field, _ in visible_rows),
        default=12,
    )
    pending_cell: tuple[int, str] | None = None
    cell_width = max(20, (width - 3) // 2)
    for offset, (label, field, value) in enumerate(rows):
        if field == "summary":
            continue
        index = start_index + offset
        if field in {"description", "comments"}:
            if pending_cell is not None:
                rendered.append(
                    [
                        (
                            truncate_cell(pending_cell[1], max(1, cell_width)),
                            detail_line_attr(pending_cell[0], selected),
                        )
                    ]
                )
                pending_cell = None
            rendered.extend(
                detail_text_block_lines(
                    index,
                    label,
                    field,
                    value,
                    field in editable_fields,
                    modified_fields,
                    selected,
                    width,
                    height,
                    field in (expanded_text_fields or set()),
                )
            )
            if offset + 1 < len(rows):
                rendered.append([("", curses.A_NORMAL)])
            continue

        cell = (index, detail_cell_text(label, field, value, editable_fields, modified_fields, label_width))
        if pending_cell is None:
            pending_cell = cell
            continue
        left = truncate_cell(pending_cell[1], max(1, cell_width))
        right = truncate_cell(cell[1], max(1, cell_width))
        rendered.append(
            [
                (f"{left:<{cell_width}}", detail_line_attr(pending_cell[0], selected)),
                ("  ", curses.A_NORMAL),
                (right, detail_line_attr(cell[0], selected)),
            ]
        )
        pending_cell = None
    if pending_cell is not None:
        rendered.append(
            [(truncate_cell(pending_cell[1], max(1, cell_width)), detail_line_attr(pending_cell[0], selected))]
        )
    return rendered


def detail_line_attr(index: int, selected: int) -> int:
    return curses.A_REVERSE if index == selected else curses.A_NORMAL


def detail_text_block_lines(
    index: int,
    label: str,
    field: str,
    value: str,
    editable: bool,
    modified_fields: set[str],
    selected: int,
    width: int,
    height: int,
    expanded: bool = False,
) -> list[DetailLine]:
    marker = ">" if editable else " "
    attr = curses.A_REVERSE if index == selected and editable else curses.A_NORMAL
    rendered = [[(f"{marker} {modified_label(label, field, modified_fields)}", attr)]]
    lines = wrap_lines(value.splitlines() or ["(empty)"], max(1, width - 5))
    limit = len(lines) if expanded else text_block_preview_limit(field, height)
    rendered.extend([(f"    {line}", curses.A_NORMAL)] for line in lines[:limit])
    remaining = len(lines) - limit
    if remaining > 0:
        rendered.append([(f"    ... {remaining} more lines (x expand)", curses.A_DIM)])
    return rendered


def flatten_detail_lines(lines: list[DetailLine]) -> list[tuple[str, int]]:
    flattened: list[tuple[str, int]] = []
    for line in lines:
        text = "".join(segment for segment, _attr in line).rstrip()
        attr = next((segment_attr for segment, segment_attr in line if segment_attr != curses.A_NORMAL), curses.A_NORMAL)
        flattened.append((text, attr))
    return flattened


def draw_detail_segment_line(stdscr: Any, row: int, line: DetailLine, width: int) -> None:
    column = 0
    limit = max(1, width - 1)
    for text, attr in line:
        if column >= limit:
            break
        remaining = limit - column
        visible = truncate_cell(text, remaining)
        if visible:
            stdscr.addnstr(row, column, visible, remaining, attr)
            column += len(visible)


def draw_detail_rows(
    stdscr: Any,
    rows: list[tuple[str, str, str]],
    start_index: int,
    selected: int,
    editable_fields: set[str],
    modified_fields: set[str],
    row: int,
    height: int,
    width: int,
    expanded_text_fields: set[str] | None = None,
) -> int:
    visible_rows = [(label, field, value) for label, field, value in rows if field != "summary"]
    label_width = max(
        (len(modified_label(label, field, modified_fields)) for label, field, _ in visible_rows),
        default=12,
    )
    pending_cell: tuple[int, str] | None = None
    cell_width = max(20, (width - 3) // 2)
    for offset, (label, field, value) in enumerate(rows):
        if field == "summary":
            continue
        index = start_index + offset
        if row >= height - 1:
            break
        if field in {"description", "comments"}:
            if pending_cell is not None:
                draw_detail_cell(stdscr, row, 0, cell_width, pending_cell[0], pending_cell[1], selected)
                pending_cell = None
                row += 1
            row = draw_description_block(
                stdscr,
                row,
                height,
                width,
                index,
                label,
                field,
                value,
                field in editable_fields,
                modified_fields,
                selected,
                field in (expanded_text_fields or set()),
            )
            if offset + 1 < len(rows) and row < height - 1:
                row += 1
            continue

        cell = (index, detail_cell_text(label, field, value, editable_fields, modified_fields, label_width))
        if pending_cell is None:
            pending_cell = cell
            continue
        draw_detail_cell(stdscr, row, 0, cell_width, pending_cell[0], pending_cell[1], selected)
        draw_detail_cell(stdscr, row, cell_width + 2, cell_width, cell[0], cell[1], selected)
        pending_cell = None
        row += 1
    if pending_cell is not None and row < height - 1:
        draw_detail_cell(stdscr, row, 0, cell_width, pending_cell[0], pending_cell[1], selected)
        row += 1
    return row


def detail_cell_text(
    label: str,
    field: str,
    value: str,
    editable_fields: set[str],
    modified_fields: set[str],
    label_width: int,
) -> str:
    marker = ">" if field in editable_fields else " "
    label = modified_label(label, field, modified_fields)
    clean_value = value.replace("\n", " ")
    return f"{marker} {label:<{label_width}}  {clean_value}"


def modified_label(label: str, field: str, modified_fields: set[str]) -> str:
    return f"{label}*" if field in modified_fields else label


def draw_detail_cell(
    stdscr: Any,
    row: int,
    column: int,
    width: int,
    index: int,
    text: str,
    selected: int,
) -> None:
    attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
    stdscr.addnstr(row, column, truncate_cell(text, max(1, width)), max(1, width), attr)


def draw_description_block(
    stdscr: Any,
    row: int,
    height: int,
    width: int,
    index: int,
    label: str,
    field: str,
    value: str,
    editable: bool,
    modified_fields: set[str],
    selected: int,
    expanded: bool = False,
) -> int:
    if row >= height - 1:
        return row
    marker = ">" if editable else " "
    attr = curses.A_REVERSE if index == selected and editable else curses.A_NORMAL
    stdscr.addnstr(row, 0, f"{marker} {modified_label(label, field, modified_fields)}", width - 1, attr)
    row += 1
    lines = wrap_lines(value.splitlines() or ["(empty)"], max(1, width - 5))
    limit = len(lines) if expanded else text_block_preview_limit(field, height)
    for line in lines[:limit]:
        if row >= height - 1:
            break
        stdscr.addnstr(row, 4, line, width - 5)
        row += 1
    remaining = len(lines) - limit
    if remaining > 0 and row < height - 1:
        stdscr.addnstr(row, 4, f"... {remaining} more lines (x expand)", width - 5, curses.A_DIM)
        row += 1
    return row


def text_block_preview_limit(field: str, height: int) -> int:
    configured = COMMENTS_PREVIEW_LINES if field == "comments" else DESCRIPTION_PREVIEW_LINES
    return max(2, min(configured, max(2, height // 3)))


def draw_detail_text(
    stdscr: Any,
    jira_dir: Path,
    key: str,
    component_field: str | None,
    mode: str,
    top: int,
    height: int,
    width: int,
    message: str | None = None,
) -> int:
    lines = wrap_lines(
        format_work_item(jira_dir, key, component_field=component_field, mode=mode).splitlines(),
        width - 1,
    )
    top = min(top, max(0, len(lines) - max(1, height - 2)))
    title = f"{key} [{mode}]  (e edit, r revert, p push, s shadow, o original, d diff, v view, h help, Esc back, q quit)"
    stdscr.addnstr(0, 0, title, width - 1, curses.A_REVERSE)
    for row, line in enumerate(lines[top : top + max(1, height - 2)], start=1):
        stdscr.addnstr(row, 0, line, width - 1)
    if message:
        stdscr.addnstr(height - 1, 0, message, width - 1, curses.A_REVERSE)
    return top
