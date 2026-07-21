from __future__ import annotations

import curses
import copy
import json
import textwrap
from pathlib import Path
from typing import Any

from .shadow import load_shadow, render_diff
from .sync import find_existing_issue, read_json


class ViewError(RuntimeError):
    pass


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


def load_manifest_items(jira_dir: Path) -> list[dict[str, Any]]:
    manifest_path = jira_dir / "manifest.json"
    if not manifest_path.exists():
        raise ViewError(f"manifest not found under {jira_dir}. Run jira-wb sync first.")
    manifest = read_json(manifest_path)
    items = manifest.get("workItems") if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        raise ViewError(f"manifest at {manifest_path} does not contain workItems")
    return [item for item in items if isinstance(item, dict)]


def load_issue(jira_dir: Path, key: str) -> dict[str, Any]:
    path = find_existing_issue(jira_dir / "components", key)
    if path is None:
        raise ViewError(f"work item {key} is not synced locally")
    issue = read_json(path)
    if not isinstance(issue, dict):
        raise ViewError(f"local issue file for {key} is not a JSON object")
    return issue


def field_section(fields: dict[str, Any], component_field: str | None) -> list[str]:
    fix_versions = display_name(fields.get("fixVersions"))
    components = display_name(fields.get("components"))
    component = display_name(fields.get(component_field)) if component_field else ""
    labels = display_name(fields.get("labels"))
    assignee = display_name(fields.get("assignee"))
    reporter = display_name(fields.get("reporter"))

    rows = [
        ("Type", display_name(fields.get("issuetype"))),
        ("Status", display_name(fields.get("status"))),
        ("Assignee", assignee),
        ("Reporter", reporter),
        ("Fix versions", fix_versions),
        ("Components", components),
        ("Component field", component),
        ("Labels", labels),
        ("Updated", display_name(fields.get("updated"))),
        ("Created", display_name(fields.get("created"))),
    ]
    width = max(len(name) for name, _ in rows)
    return [f"{name:<{width}}  {value}" for name, value in rows if value]


def format_issue(
    issue: dict[str, Any],
    *,
    component_field: str | None = None,
    shadow: dict[str, Any] | None = None,
) -> str:
    if shadow is not None:
        issue = apply_shadow(issue, shadow)

    key = display_name(issue.get("key"))
    fields = as_dict(issue.get("fields"))
    summary = display_name(fields.get("summary"))
    description = text_from_adf(fields.get("description")).strip()

    lines = [f"{key}  {summary}".rstrip(), ""]
    if shadow is not None:
        lines.extend(format_shadow_summary(shadow))
        lines.append("")
    lines.extend(field_section(fields, component_field))
    lines.append("")
    lines.append("Description")
    lines.append("-----------")
    lines.append(description or "(empty)")

    remaining = sorted(
        field for field in fields if field not in {
            "summary",
            "description",
            "issuetype",
            "status",
            "assignee",
            "reporter",
            "fixVersions",
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
    if isinstance(comments, list) and comments:
        lines.append("")
        lines.append("Local comments")
        for comment in comments:
            if isinstance(comment, dict):
                state = display_name(comment.get("state")) or "working"
                body = display_name(comment.get("body"))
                lines.append(f"- [{state}] {body}")
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
    return format_issue(issue, component_field=component_field, shadow=shadow)


def item_label(item: dict[str, Any]) -> str:
    return "  ".join(
        part
        for part in (
            display_name(item.get("key")),
            display_name(item.get("status")),
            display_name(item.get("component")),
            display_name(item.get("summary")),
        )
        if part
    )


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


def interactive_view(jira_dir: Path, *, component_field: str | None = None) -> None:
    items = load_manifest_items(jira_dir)
    curses.wrapper(_interactive_view, jira_dir, items, component_field)


def _interactive_view(stdscr: Any, jira_dir: Path, items: list[dict[str, Any]], component_field: str | None) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    selected = 0
    top = 0
    detail_key: str | None = None
    detail_top = 0
    detail_mode = "shadow"

    while True:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        if detail_key:
            detail_top = draw_detail(
                stdscr,
                jira_dir,
                detail_key,
                component_field,
                detail_mode,
                detail_top,
                height,
                width,
            )
        else:
            selected, top = draw_index(stdscr, items, selected, top, height, width)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), 27):
            if detail_key:
                detail_key = None
                detail_top = 0
            else:
                return
        elif detail_key:
            if key in (curses.KEY_UP, ord("k")):
                detail_top = max(0, detail_top - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                detail_top += 1
            elif key in (curses.KEY_NPAGE, ord(" ")):
                detail_top += max(1, height - 2)
            elif key in (curses.KEY_PPAGE, ord("b")):
                detail_top = max(0, detail_top - max(1, height - 2))
            elif key == ord("d"):
                detail_mode = "diff"
                detail_top = 0
            elif key == ord("o"):
                detail_mode = "original"
                detail_top = 0
            elif key == ord("s"):
                detail_mode = "shadow"
                detail_top = 0
        else:
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(max(0, len(items) - 1), selected + 1)
            elif key in (curses.KEY_NPAGE, ord(" ")):
                selected = min(max(0, len(items) - 1), selected + max(1, height - 2))
            elif key in (curses.KEY_PPAGE, ord("b")):
                selected = max(0, selected - max(1, height - 2))
            elif key in (10, 13, curses.KEY_ENTER) and items:
                detail_key = display_name(items[selected].get("key"))
                detail_mode = "shadow"
                detail_top = 0


def draw_index(stdscr: Any, items: list[dict[str, Any]], selected: int, top: int, height: int, width: int) -> tuple[int, int]:
    visible_height = max(1, height - 2)
    if selected < top:
        top = selected
    elif selected >= top + visible_height:
        top = selected - visible_height + 1

    title = f"Jira Workbench: {len(items)} items  (j/k or arrows, Enter open, q quit)"
    stdscr.addnstr(0, 0, title, width - 1, curses.A_REVERSE)
    for row, item in enumerate(items[top : top + visible_height], start=1):
        index = top + row - 1
        attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
        stdscr.addnstr(row, 0, item_label(item), width - 1, attr)
    return selected, top


def draw_detail(
    stdscr: Any,
    jira_dir: Path,
    key: str,
    component_field: str | None,
    mode: str,
    top: int,
    height: int,
    width: int,
) -> int:
    lines = wrap_lines(
        format_work_item(jira_dir, key, component_field=component_field, mode=mode).splitlines(),
        width - 1,
    )
    top = min(top, max(0, len(lines) - max(1, height - 2)))
    title = f"{key} [{mode}]  (s shadow, o original, d diff, q back)"
    stdscr.addnstr(0, 0, title, width - 1, curses.A_REVERSE)
    for row, line in enumerate(lines[top : top + max(1, height - 2)], start=1):
        stdscr.addnstr(row, 0, line, width - 1)
    return top
