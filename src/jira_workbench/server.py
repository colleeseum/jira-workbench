from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import ConfigError, WorkbenchConfig, save_view_defaults
from .metadata import MetadataError, component_name
from .service import load_meta_boards, load_meta_components, load_meta_versions
from .shadow import load_shadow
from .sync import find_existing_issue, read_json
from .view import (
    ViewError,
    apply_shadow,
    as_dict,
    as_list,
    component_counts,
    display_component,
    distinct_field_values,
    filter_items,
    hierarchy_component,
    load_cached_boards,
    load_manifest_items,
    version_options,
    sort_items_for_swimlane,
)

# Only ever used to validate a `key` path parameter (untrusted network input)
# before it reaches find_existing_issue's glob/direct-path lookup -- unlike
# every internal caller, which always passes an already-known-good key
# straight from a synced manifest, this one has to guard against a crafted
# value (e.g. "../../etc/passwd") trying to escape components_dir.
ISSUE_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


# --- Data layer: both the JSON API routes and the HTML GUI routes below
# call these directly, so the GUI never touches view.py/service.py itself --
# it only ever sees what the API layer already exposes (per the "one shared
# layer behind every interface" principle).


def all_items(jira_dir: Path, config: WorkbenchConfig) -> list[dict[str, Any]]:
    """Every synced item, unfiltered -- raises ViewError if no manifest.
    Shared by items_data (further filters this) and the GUI's filter panel
    (needs the full set to compute each field's available options/counts,
    independent of whatever's currently selected)."""
    default_project = config.default_project_key()
    component_field = config.effective_component_field(default_project)
    return load_manifest_items(jira_dir, component_field)


def items_data(
    jira_dir: Path,
    config: WorkbenchConfig,
    *,
    field_filters: dict[str, list[str]] | None = None,
    pattern: str | None = None,
    active: bool = True,
    board: str | None = None,
    board_scope: str | None = None,
) -> dict[str, Any]:
    items = all_items(jira_dir, config)
    filtered = filter_items(
        items,
        field_filters=field_filters or {},
        pattern=pattern,
        active=active,
        board=board,
        board_scope=board_scope,
    )
    return {"count": len(filtered), "items": filtered}


def item_detail_data(jira_dir: Path, key: str) -> dict[str, Any] | None:
    if not ISSUE_KEY_PATTERN.match(key):
        return None
    path = find_existing_issue(jira_dir / "components", key)
    if path is None:
        return None
    issue = read_json(path)
    if not isinstance(issue, dict):
        raise MetadataError(f"local issue file for {key} is invalid")
    shadow = load_shadow(jira_dir, key)
    effective = apply_shadow(issue, shadow) if shadow is not None else issue
    comments_path = path.parent / "comments.json"
    attachments_path = path.parent / "attachments.json"
    # comments.json can be either a bare list or Jira's own raw
    # {"comments": [...], "total": ..., ...} shape depending on how it was
    # fetched (see sync.py's fetch_issue_comments) -- normalized the same
    # way view.py's own comments_text already handles both, so callers
    # always get a plain list.
    comments_payload = read_json(comments_path) if comments_path.exists() else []
    comments = (
        as_list(as_dict(comments_payload).get("comments"))
        if isinstance(comments_payload, dict)
        else as_list(comments_payload)
    )
    attachments = as_list(read_json(attachments_path)) if attachments_path.exists() else []
    return {"issue": effective, "comments": comments, "attachments": attachments}


def meta_versions_data(jira_dir: Path, config: WorkbenchConfig, project: str | None) -> list[dict[str, Any]]:
    return load_meta_versions(jira_dir, project, config.jira_url, config.jira_email, config.jira_api_token)


def meta_components_data(
    jira_dir: Path, config: WorkbenchConfig, project: str | None, component_field: str | None = None
) -> list[dict[str, Any]]:
    return load_meta_components(
        jira_dir,
        project,
        component_field or config.effective_component_field(project),
        config.jira_url,
        config.jira_email,
        config.jira_api_token,
    )


def meta_boards_data(
    jira_dir: Path, config: WorkbenchConfig, project: str | None, component_field: str | None = None
) -> list[dict[str, Any]]:
    return load_meta_boards(
        jira_dir,
        project,
        component_field or config.effective_component_field(project),
        config.jira_url,
        config.jira_email,
        config.jira_api_token,
    )


# --- GUI: minimal server-rendered HTML, read-only. Browse/filter/search
# items, item detail, and meta listings -- matching the TUI's read side
# only (no editing/pushing/creating/board-management from here). Plain
# f-string templates, matching this module's existing style, rather than
# adding a templating-engine dependency for a first read-only pass.


# Web icon set -- inline SVG glyphs echoing Jira's own issue-type/priority
# icon shapes and colors (a bug, a lightning bolt for Epic, a bookmark for
# Story, a checkbox for Task, a "child of" corner-arrow for Subtask; ranked
# chevrons for priority), rather than the plain single-width terminal glyphs
# the TUI uses or generic colored circles -- crisp at any size, no font/
# emoji-rendering dependency, styled via `color: currentColor` so one SVG
# works for every type/priority just by wrapping it in a colored span.
_BOLT_SVG = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">'
    '<path d="M14.615 1.595a.75.75 0 01.359.852L12.982 9.75h7.268a.75.75 0 01.548 1.262l-10.5 11.25a.75.75 0 '
    "01-1.272-.71l1.992-7.302H3.75a.75.75 0 01-.548-1.262l10.5-11.25a.75.75 0 01.913-.143z\"/></svg>"
)
_BOOKMARK_SVG = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">'
    '<path d="M6.32 2.577a49.255 49.255 0 0111.36 0c1.497.174 2.57 1.46 2.57 2.93V21a.75.75 0 01-1.152.633L12 '
    "18.113l-7.098 4.52A.75.75 0 013.75 21V5.507c0-1.47 1.073-2.756 2.57-2.93z\"/></svg>"
)
_CHECKBOX_SVG = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="3"/>'
    '<path d="M8 12.5l2.5 2.5L16 9"/></svg>'
)
_BUG_SVG = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="8" height="10" rx="4"/>'
    '<path d="M12 8V5M9 5L7.5 3.5M15 5l1.5-1.5M4 12h4M16 12h4M5 17l3-2M19 17l-3-2M5 8l3 2M19 8l-3 2"/></svg>'
)
_SUBTASK_SVG = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M6 4v6a4 4 0 004 4h8"/><path d="M14 10l4 4-4 4"/></svg>'
)
_CIRCLE_SVG = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">'
    '<circle cx="12" cy="12" r="8"/></svg>'
)

TYPE_ICONS_SVG: dict[str, tuple[str, str]] = {
    "epic": (_BOLT_SVG, "#8B5CF6"),
    "story": (_BOOKMARK_SVG, "#22C55E"),
    "task": (_CHECKBOX_SVG, "#3B82F6"),
    "bug": (_BUG_SVG, "#EF4444"),
    "sub-task": (_SUBTASK_SVG, "#06B6D4"),
    "subtask": (_SUBTASK_SVG, "#06B6D4"),
}
DEFAULT_TYPE_ICON_SVG: tuple[str, str] = (_CIRCLE_SVG, "#94A3B8")

# Single/double chevrons, ranked red (highest) to green (lowest) -- same
# shape Jira's own priority icons use, "=" for Medium as a neutral baseline
# rather than a color on the urgency gradient.
_CHEVRON_UP_SVG = (
    '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M4 13l6-6 6 6"/></svg>'
)
_CHEVRON_UP_UP_SVG = (
    '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M4 16l6-6 6 6M4 10l6-6 6 6"/></svg>'
)
_CHEVRON_DOWN_SVG = (
    '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M4 7l6 6 6-6"/></svg>'
)
_CHEVRON_DOWN_DOWN_SVG = (
    '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M4 4l6 6 6-6M4 10l6 6 6-6"/></svg>'
)
_EQUALS_SVG = (
    '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round"><path d="M4 8h12M4 12h12"/></svg>'
)

PRIORITY_ICONS_SVG: dict[str, tuple[str, str]] = {
    "highest": (_CHEVRON_UP_UP_SVG, "#EF4444"),
    "high": (_CHEVRON_UP_SVG, "#F59E0B"),
    "medium": (_EQUALS_SVG, "#94A3B8"),
    "low": (_CHEVRON_DOWN_SVG, "#06B6D4"),
    "lowest": (_CHEVRON_DOWN_DOWN_SVG, "#22C55E"),
}


def _type_icon_cell(name: str) -> str:
    key = name.strip().lower() if name else ""
    svg, color = TYPE_ICONS_SVG.get(key, DEFAULT_TYPE_ICON_SVG)
    title = html.escape(name) if name else "Unknown type"
    return f'<span style="color: {color}" title="{title}">{svg}</span>'


def _priority_icon_cell(name: str) -> str:
    # A genuinely unset priority (some issue types don't have the field at
    # all) is real Jira data, not a rendering bug -- shown as an explicit
    # muted dash with its own tooltip so it reads as "none", never as a
    # missing/broken icon, and is visually distinct from an actual Medium
    # priority's own icon.
    if not name:
        return '<span style="opacity: .4" title="No priority set">—</span>'
    key = name.strip().lower()
    svg, color = PRIORITY_ICONS_SVG.get(key, DEFAULT_TYPE_ICON_SVG)
    return f'<span style="color: {color}" title="{html.escape(name)}">{svg}</span>'


def page(body: str, *, title: str = "Jira Workbench", sidebar_extra: str = "", active_nav: str = "items") -> str:
    # One <form> per page, owned here, so every page (items/detail/meta)
    # shares the exact same sidebar chrome without ever nesting a second
    # <form> inside it (invalid HTML) -- omitting `action` means it submits
    # back to whatever path it's rendered on, which is already correct for
    # each page (items' own filter fields resubmit to "/", meta's project
    # field resubmits to "/meta"), no per-page action needed.
    def nav_link(href: str, key: str, label: str) -> str:
        css_class = ' class="active"' if key == active_nav else ""
        return f'<a href="{href}"{css_class}>{label}</a>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; line-height: 1.4; }}
    header {{ margin-bottom: 24px; }}
    input, select {{ padding: 6px 8px; }}
    button {{ padding: 6px 10px; }}
    .button-link {{
      display: inline-block; padding: 6px 10px; border: 1px solid #8885; border-radius: 4px;
      text-decoration: none; color: inherit; background: ButtonFace;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #9995; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: Canvas; }}
    pre {{ overflow: auto; padding: 12px; background: #8882; white-space: pre-wrap; }}
    a {{ color: LinkText; }}
    .filters {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 16px; }}
    .pill {{ display: inline-block; padding: 1px 8px; border-radius: 10px; background: #8883; margin-right: 4px; }}
    .filter-field {{ position: relative; display: inline-block; }}
    .filter-field summary {{
      list-style: none; cursor: pointer; padding: 6px 10px; border: 1px solid #8885; border-radius: 6px;
      user-select: none;
    }}
    .filter-field summary::-webkit-details-marker {{ display: none; }}
    .filter-field[open] summary {{ border-color: LinkText; }}
    .filter-field .badge {{
      display: inline-block; min-width: 1.2em; padding: 0 5px; margin-left: 6px; border-radius: 8px;
      background: LinkText; color: Canvas; font-size: 0.8em; text-align: center;
    }}
    .checklist {{
      position: absolute; top: 100%; left: 0; z-index: 10; margin-top: 4px; padding: 8px;
      background: Canvas; border: 1px solid #8885; border-radius: 6px; box-shadow: 0 4px 12px #0004;
      max-height: 260px; overflow-y: auto; min-width: 220px;
    }}
    .checkbox-row {{ display: flex; align-items: center; gap: 6px; padding: 3px 4px; white-space: nowrap; cursor: pointer; }}
    .checkbox-row .count {{ margin-left: auto; opacity: 0.6; font-size: 0.85em; padding-left: 12px; }}
    .clear-field-link {{
      display: block; padding: 3px 4px 8px; font-size: 0.85em; border-bottom: 1px solid #8885; margin-bottom: 4px;
    }}
    .layout {{ display: flex; align-items: stretch; gap: 0; min-height: 100vh; }}
    .content {{ flex: 1 1 auto; min-width: 0; padding: 24px; }}
    /* Sidebar-collapse checkbox hack -- toggles the whole pane down to a
    thin strip, no JavaScript. Must be a sibling of .layout (not nested
    inside it) for the ~ selector below to reach .sidebar. */
    #sidebar-collapsed {{ display: none; }}
    .sidebar {{
      flex: 0 0 auto; width: 220px; min-width: 160px; max-width: 480px; box-sizing: border-box;
      resize: horizontal; overflow: auto; border-right: 1px solid #8885; padding: 12px;
      transition: width 0.15s ease;
    }}
    .sidebar-toggle {{
      display: block; cursor: pointer; user-select: none; font-size: 1.1em; padding: 4px 0 16px;
    }}
    #sidebar-collapsed:checked ~ .layout .sidebar {{
      width: 40px !important; min-width: 40px; resize: none; overflow: hidden; padding: 12px 8px;
    }}
    #sidebar-collapsed:checked ~ .layout .sidebar-inner {{ display: none; }}
    .sidebar-nav {{ display: flex; flex-direction: column; gap: 2px; margin-bottom: 16px; }}
    .sidebar-nav a {{ padding: 6px 8px; border-radius: 6px; text-decoration: none; }}
    .sidebar-nav a.active {{ background: LinkText; color: Canvas; }}
    .sidebar-section {{ display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }}
    .sidebar-section label {{ font-size: 0.85em; opacity: 0.75; }}
    .sidebar-section select {{ width: 100%; }}
  </style>
</head>
<body>
  <input type="checkbox" id="sidebar-collapsed">
  <form method="get" class="layout">
    <aside class="sidebar">
      <label for="sidebar-collapsed" class="sidebar-toggle" title="Collapse/expand sidebar">☰</label>
      <div class="sidebar-inner">
        <nav class="sidebar-nav">
          {nav_link("/", "items", "Items")}
          {nav_link("/meta", "meta", "Meta")}
        </nav>
        {sidebar_extra}
      </div>
    </aside>
    <main class="content">
{body}
    </main>
  </form>
</body>
</html>
"""


def _options_html(options: list[str], current: str) -> str:
    html_options = ['<option value=""' + (" selected" if not current else "") + ">(any)</option>"]
    for option in options:
        selected = " selected" if option == current else ""
        html_options.append(f'<option value="{html.escape(option)}"{selected}>{html.escape(option)}</option>')
    return "".join(html_options)


def _clear_field_url(query: dict[str, Any], field: str) -> str:
    """Current query with just `field` removed -- everything else
    (other filters, sort/dir) preserved, so clearing one field doesn't
    reset the rest."""
    params = {key: value for key, value in query.items() if key != field and value}
    return f"/?{urlencode(params, doseq=True)}"


def _checkbox_field(
    label: str,
    name: str,
    options: list[tuple[str, int]],
    selected: list[str],
    query: dict[str, Any],
    extra: str = "",
    force_open: bool = False,
) -> str:
    """A Jira-style "Basic filter" field: a chip that expands into a
    checkbox list with per-value counts, badge-tallying how many are
    currently selected -- mirrors the reference screenshot's Status/Work
    type/Components fields, rather than a free-text input. `extra` is an
    optional bit of field-specific markup (e.g. Fix version's "show
    released" toggle) rendered between the clear link and the checkboxes.

    Opens by default whenever this field already has a selection (or
    `force_open` says so) -- otherwise a control that submits the form from
    inside here (like the "show released" checkbox) reloads the whole page
    and the freshly-rendered <details> snaps shut again, undoing the very
    thing you just clicked open to look at."""
    open_attr = " open" if selected or force_open else ""
    selected_lower = {value.strip().lower() for value in selected}
    badge = f'<span class="badge">{len(selected)}</span>' if selected else ""
    clear_link = (
        f'<a href="{_clear_field_url(query, name)}" class="clear-field-link">Clear {html.escape(label.lower())}</a>'
        if selected
        else ""
    )
    rows = []
    for value, count in options:
        checked = " checked" if value.strip().lower() in selected_lower else ""
        # "_unassigned" is sync.py's real on-disk sentinel for "no
        # component" (see component_slug) -- a valid, submittable filter
        # value, but not something to print raw; every other filter field
        # this can't happen for (status/assignee/etc. always have a name).
        display = "(unassigned)" if value == "_unassigned" else value
        rows.append(
            f'<label class="checkbox-row"><input type="checkbox" name="{name}" value="{html.escape(value)}"{checked}>'
            f' {html.escape(display)}<span class="count">{count}</span></label>'
        )
    return f"""
    <details class="filter-field"{open_attr}>
      <summary>{html.escape(label)}{badge}</summary>
      <div class="checklist">
        {clear_link}
        {extra}
        {"".join(rows)}
      </div>
    </details>
    """


def _board_scope_section(query: dict[str, Any], boards: list[str]) -> str:
    """Items-page-only sidebar content -- Board/Scope today; the natural
    home for whatever other view-level (not item-level) controls get added
    later (saved views, project switcher, etc.), which is exactly why this
    is its own section rather than folded into the filter chips. Nested in
    a <details> so it can fold away on its own independent of the whole
    sidebar's collapse (see page()'s #sidebar-collapsed checkbox).

    Board/Scope submit on change (the one deliberate bit of JavaScript in
    this whole page, everything else here is JS-free by design) -- unlike
    the checkbox filter fields, where you usually want to tick several
    values before applying, a single-value dropdown reads as "pick one and
    it takes effect immediately"; requiring a separate Apply click here was
    confusing (looked like changing it did nothing)."""
    board_value = query.get("board") or ""
    board_scope = query.get("board_scope") or ""
    return f"""
    <details open>
      <summary>View</summary>
      <div class="sidebar-section">
        <label for="sidebar-board">Board</label>
        <select name="board" id="sidebar-board" onchange="this.form.submit()">{_options_html(boards, board_value)}</select>
      </div>
      <div class="sidebar-section">
        <label for="sidebar-board-scope">Scope</label>
        <select name="board_scope" id="sidebar-board-scope" onchange="this.form.submit()">
          <option value="">(any scope)</option>
          <option value="active" {"selected" if board_scope == "active" else ""}>Active</option>
          <option value="backlog" {"selected" if board_scope == "backlog" else ""}>Backlog</option>
        </select>
      </div>
    </details>
    """


def _clear_all_url(query: dict[str, Any]) -> str:
    # Sort/dir are view state, not a filter -- kept so "start over on
    # filters" doesn't also scramble whatever column you had sorted.
    params = {key: query[key] for key in ("sort", "dir") if query.get(key)}
    return f"/?{urlencode(params, doseq=True)}"


def _filter_form(query: dict[str, Any], base_items: list[dict[str, Any]], allowed_version_names: set[str]) -> str:
    project_field = _checkbox_field(
        "Project", "project", distinct_field_values(base_items, "project"), query["project"], query
    )
    # Status/Component/Fix version/Assignee are all scoped to whichever
    # project(s) are currently selected -- no point offering a status,
    # component, version, or assignee that doesn't occur anywhere in the
    # selected project(s); it can't match anything. Unscoped (shows every
    # value across every synced project) when no project filter is set yet.
    selected_projects = {value.strip().lower() for value in query["project"]}
    project_scoped_items = (
        [item for item in base_items if str(item.get("project") or "").strip().lower() in selected_projects]
        if selected_projects
        else base_items
    )
    status_field = _checkbox_field(
        "Status", "status", distinct_field_values(project_scoped_items, "status"), query["status"], query
    )
    component_field = _checkbox_field(
        "Component", "component", component_counts(project_scoped_items), query["component"], query
    )
    # Same policy as the TUI's version picker: archived versions never
    # show, released ones are hidden unless the "show released" toggle is
    # on -- assigning new work to an already-shipped version is unusual
    # enough to want a deliberate second step. allowed_version_names is
    # precomputed in the route (it needs the real versions cache, not just
    # what's on currently-synced items) via view.py's version_options, the
    # same helper the TUI's picker itself calls. A version that's already
    # checked stays visible even if the toggle would otherwise hide it, so
    # switching the toggle off never silently un-submits your selection.
    selected_versions_lower = {value.strip().lower() for value in query["fixVersion"]}
    fix_version_options = [
        (name, count)
        for name, count in distinct_field_values(project_scoped_items, "fixVersion")
        if name.strip().lower() in allowed_version_names or name.strip().lower() in selected_versions_lower
    ]
    show_released_checked = "checked" if query.get("show_released") == "1" else ""
    version_toggle = f"""
    <label class="checkbox-row" title="Also list versions Jira already shows as Released (archived versions never show, matching Jira's own Fix Version picker)">
      <input type="checkbox" name="show_released" value="1" {show_released_checked} onchange="this.form.submit()"> Show released versions
    </label>
    """
    fix_version_field = _checkbox_field(
        "Fix version",
        "fixVersion",
        fix_version_options,
        query["fixVersion"],
        query,
        extra=version_toggle,
        force_open=query.get("show_released") == "1",
    )
    assignee_field = _checkbox_field(
        "Assignee", "assignee", distinct_field_values(project_scoped_items, "assignee"), query["assignee"], query
    )
    pattern = html.escape(query.get("pattern") or "")
    active_checked = "checked" if query.get("active", "1") == "1" else ""
    sort_value = html.escape(query.get("sort") or "")
    dir_value = html.escape(query.get("dir") or "asc")
    return f"""
    <div class="filters">
      <input name="pattern" value="{pattern}" placeholder="Search key, summary, status, type" style="flex: 1; min-width: 240px;">
      {project_field}
      {status_field}
      {component_field}
      {fix_version_field}
      {assignee_field}
      <label title="Hide issues whose status is Done/Closed (anything Jira categorizes as a 'done' status)">
        <input type="checkbox" name="active" value="1" {active_checked}> Active only
      </label>
      <input type="hidden" name="sort" value="{sort_value}">
      <input type="hidden" name="dir" value="{dir_value}">
      <button type="submit" title="Reload the list using the project/status/component/etc. selections above">Apply filters</button>
      <button type="submit" formmethod="post" formaction="/save-filter" title="Write the current selections to config.toml as the default view everywhere">Save as default filter</button>
      <a href="{_clear_all_url(query)}" class="button-link" title="Remove every filter below (search, project, status, component, fix version, assignee, active-only, board, scope)">Clear all filters</a>
      <a href="/" class="button-link" title="Discard the selections above and reload using the saved default filter">Reset to saved default filter</a>
    </div>
    """


# Column label -> item dict field, for click-to-sort headers (reuses the
# same sort_items_for_swimlane the TUI's own click-to-sort columns use, so
# priority sorts by real urgency rank rather than alphabetically).
COLUMN_SORT_FIELDS: dict[str, str] = {
    "Key": "key",
    "Status": "status",
    "Component": "component",
    "Summary": "summary",
    "Pr": "priority",
    "Assignee": "assignee",
    "Fix version": "fixVersion",
}


def _sort_header(label: str, query: dict[str, Any]) -> str:
    field = COLUMN_SORT_FIELDS.get(label)
    if field is None:
        return f"<th>{html.escape(label)}</th>"
    current_sort = query.get("sort", "")
    current_dir = query.get("dir", "asc")
    next_dir = "desc" if current_sort == field and current_dir == "asc" else "asc"
    # doseq=True so a list-valued field (project/status/component/...)
    # round-trips as repeated params (?status=A&status=B), not one
    # comma/str-joined value -- query's list fields come straight from
    # FastAPI's own Query(...)/Form(...) list parsing.
    params = {key: value for key, value in query.items() if key not in ("sort", "dir") and value}
    params["sort"] = field
    params["dir"] = next_dir
    indicator = (" ▲" if current_dir == "asc" else " ▼") if current_sort == field else ""
    return f'<th><a href="/?{urlencode(params, doseq=True)}">{html.escape(label)}{indicator}</a></th>'


def render_items_page(
    items: list[dict[str, Any]],
    total: int,
    query: dict[str, Any],
    base_items: list[dict[str, Any]],
    boards: list[str],
    allowed_version_names: set[str],
) -> str:
    # query["sort"] already holds the resolved item-dict field name (see
    # _sort_header, which is the only place that sets it) -- not the
    # display label, so this looks it up by value, never by key.
    sort_field = query.get("sort") or None
    if sort_field in COLUMN_SORT_FIELDS.values():
        items = sort_items_for_swimlane(items, None, sort_field=sort_field, reverse=query.get("dir") == "desc")

    rows = []
    for item in items:
        key = html.escape(str(item.get("key", "")))
        component = html.escape(display_component(item.get("component")))
        rows.append(
            "<tr>"
            f"<td>{_type_icon_cell(str(item.get('type', '')))}</td>"
            f'<td><a href="/items/{key}">{key}</a></td>'
            f"<td>{html.escape(str(item.get('status', '')))}</td>"
            f"<td>{component}</td>"
            f"<td>{html.escape(str(item.get('summary', '')))}</td>"
            f"<td>{_priority_icon_cell(str(item.get('priority', '')))}</td>"
            f"<td>{html.escape(str(item.get('assignee', '')))}</td>"
            f"<td>{html.escape(str(item.get('fixVersion', '')))}</td>"
            "</tr>"
        )
    headers = "".join(
        _sort_header(label, query) for label in ("", "Key", "Status", "Component", "Summary", "Pr", "Assignee", "Fix version")
    )
    body = f"""
        <header>
          <h1>Work items</h1>
          <p>{len(items)} of {total} synced items match the current filter.</p>
          {_filter_form(query, base_items, allowed_version_names)}
        </header>
        <table>
          <thead><tr>{headers}</tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
    """
    return page(body, title="Jira Workbench", sidebar_extra=_board_scope_section(query, boards), active_nav="items")


def render_item_detail_page(key: str, detail: dict[str, Any], component_field: str | None = None) -> str:
    issue = detail["issue"]
    fields = as_dict(issue.get("fields"))
    escaped_key = html.escape(key)

    def field(name: str) -> str:
        value = fields.get(name)
        if isinstance(value, dict):
            value = value.get("displayName") or value.get("name") or value.get("value") or value
        if isinstance(value, list):
            value = ", ".join(str(as_dict(v).get("name", v)) for v in value)
        return html.escape(str(value)) if value else ""

    parent = as_dict(fields.get("parent"))
    parent_key = html.escape(str(parent.get("key", "")))
    parent_link = f'<a href="/items/{parent_key}">{parent_key}</a>' if parent_key else ""

    rows = [
        ("Summary", field("summary")),
        ("Type", field("issuetype")),
        ("Status", field("status")),
        ("Priority", field("priority")),
        ("Component", html.escape(hierarchy_component(issue, component_field))),
        ("Assignee", field("assignee")),
        ("Reporter", field("reporter")),
        ("Fix versions", field("fixVersions")),
        ("Labels", field("labels")),
        ("Parent", parent_link),
    ]
    row_html = "".join(f"<tr><th>{html.escape(label)}</th><td>{value}</td></tr>" for label, value in rows)

    description = fields.get("description")
    description_text = html.escape(str(description)) if isinstance(description, str) else ""

    comment_lines = []
    for comment in detail["comments"]:
        body = as_dict(comment).get("body")
        author = as_dict(as_dict(comment).get("author")).get("displayName", "")
        text = body if isinstance(body, str) else ""
        if text:
            comment_lines.append(f"<p><strong>{html.escape(str(author))}</strong><br>{html.escape(text)}</p>")

    body = f"""
    <header><p><a href="/">Back to items</a></p><h1>{escaped_key}</h1></header>
    <table>{row_html}</table>
    <h2>Description</h2>
    <pre>{description_text}</pre>
    <h2>Comments</h2>
    {"".join(comment_lines) or "<p>No comments.</p>"}
    """
    return page(body, title=f"{key} - Jira Workbench", active_nav="items")


def _boards_rows(boards: list[dict[str, Any]]) -> str:
    # Boards have no "supported" key -- metadata.py's refresh_boards_api
    # stores an `unsupportedReason` string instead (None/absent means
    # supported). "Supported" here is a jira-wb concept, not a Jira one: it
    # means jira-wb's own JQL compiler (compile_jql) could translate this
    # board's saved filter into a local predicate it can evaluate offline
    # (see matches_board) -- e.g. a board whose JQL uses `IN`/`NOT IN`/
    # `Sprint`, which compile_jql doesn't handle, shows "No" here even
    # though the board itself works fine in real Jira. The generic name/id
    # column renderer used for versions/components can't derive Yes/No
    # from that, so boards get their own renderer -- also surfaces the
    # board's real JQL directly, straight from its saved Jira filter.
    head = '<th>Name</th><th>ID</th><th>Type</th><th title="Whether jira-wb\'s own JQL parser could translate this board\'s filter into a local predicate -- unrelated to whether the board works in Jira itself">Supported</th><th>JQL / reason</th>'
    body_rows = []
    for board in boards:
        reason = board.get("unsupportedReason")
        jql = board.get("jql")
        supported = "No" if reason else "Yes"
        # Always show the real JQL alongside the reason (when there is one)
        # so "unsupported" is verifiable at a glance against the actual
        # filter, not just the reason text on its own.
        if reason and jql:
            detail = f"{reason} (jql: {jql})"
        elif reason:
            detail = str(reason)
        else:
            detail = str(jql or "")
        detail = html.escape(detail)
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(str(board.get('name', '')))}</td>"
            f"<td>{html.escape(str(board.get('id', '')))}</td>"
            f"<td>{html.escape(str(board.get('type', '')))}</td>"
            f"<td>{supported}</td>"
            f"<td><code>{detail}</code></td>"
            "</tr>"
        )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _generic_rows(items: list[dict[str, Any]], columns: list[str]) -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body_rows = []
    for item in items:
        cells = "".join(f"<td>{html.escape(str(item.get(c, '')))}</td>" for c in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _filter_by_name(
    items: list[dict[str, Any]], q: str | None, name_fn: Any = None
) -> list[dict[str, Any]]:
    """Versions/components/boards can run into the hundreds for an active
    project -- a simple case-insensitive substring match on each item's
    display name lets a large list be narrowed down without needing a real
    search index. `name_fn` defaults to the plain "name" key (versions and
    boards both always have one); components need metadata.py's own
    `component_name` instead, since a component's name can live under
    "name", "component", or "value" depending on where it was loaded from
    (manifest usage summary vs. a custom field's option cache)."""
    if not q:
        return items
    needle = q.strip().lower()
    if not needle:
        return items
    get_name = name_fn or (lambda item: str(item.get("name") or ""))
    return [item for item in items if needle in get_name(item).lower()]


def _components_rows(components: list[dict[str, Any]]) -> str:
    # Unlike versions/boards, a component dict's shape depends on where it
    # was loaded from: a manifest usage summary uses "component"/"count",
    # while a custom field's option cache (or the native Jira components
    # API) uses "name"/"id"/"active"/"total" -- component_name() and the
    # total/count fallback below normalize across both rather than
    # rendering blank cells for whichever shape wasn't used this time.
    head = "<th>Name</th><th>ID</th><th>Active</th><th>Total</th>"
    body_rows = []
    for component in components:
        name = component_name(component)
        total = component.get("total")
        if total is None:
            total = component.get("count")
        active = component.get("active")
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{html.escape(str(component.get('id') or ''))}</td>"
            f"<td>{html.escape(str(active)) if active is not None else ''}</td>"
            f"<td>{html.escape(str(total)) if total is not None else ''}</td>"
            "</tr>"
        )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


META_SECTIONS: tuple[tuple[str, str], ...] = (
    ("versions", "Versions"),
    ("components", "Components"),
    ("boards", "Boards"),
)


def _meta_sidebar(projects: list[str], project: str | None, section: str | None) -> str:
    """The Meta area's own two-tier nav, distinct from the Items page's
    Board/Scope controls: a "Projects" list (so Meta always starts from
    "which project", per the user's own framing) and, once a project is
    picked, a second "Sections" list (Versions/Components/Boards) -- kept
    as two separate <nav> blocks rather than folding everything onto one
    page, since versions/components/boards for a real project can each run
    long enough that showing all three at once just becomes noise."""
    default_section = section or "versions"

    def project_link(key: str) -> str:
        css_class = ' class="active"' if key == project else ""
        href = f"/meta/{default_section}?{urlencode({'project': key})}"
        return f'<a href="{href}"{css_class}>{html.escape(key)}</a>'

    projects_nav = f"""
    <div class="sidebar-section"><label>Projects</label></div>
    <nav class="sidebar-nav">
      {"".join(project_link(p) for p in projects) or "<p>No projects configured.</p>"}
    </nav>
    """

    if not project:
        return projects_nav

    def section_link(key: str, label: str) -> str:
        css_class = ' class="active"' if key == section else ""
        href = f"/meta/{key}?{urlencode({'project': project})}"
        return f'<a href="{href}"{css_class}>{label}</a>'

    sections_nav = f"""
    <div class="sidebar-section"><label>{html.escape(project)}</label></div>
    <nav class="sidebar-nav">
      {"".join(section_link(key, label) for key, label in META_SECTIONS)}
    </nav>
    """
    return projects_nav + sections_nav


def render_meta_projects_page(projects: list[str], section: str | None = None) -> str:
    default_section = section or "versions"
    links = "".join(
        f'<li><a href="/meta/{default_section}?{urlencode({"project": p})}">{html.escape(p)}</a></li>'
        for p in projects
    )
    body = f"""
    <header><h1>Meta</h1></header>
    {f"<ul>{links}</ul>" if links else "<p>No projects configured -- add one to config.toml.</p>"}
    """
    return page(body, title="Meta - Jira Workbench", sidebar_extra=_meta_sidebar(projects, None, section), active_nav="meta")


def render_meta_section_page(
    section: str, project: str, projects: list[str], table_html: str, q: str | None, empty_message: str
) -> str:
    title = dict(META_SECTIONS).get(section, section.title())
    q_value = html.escape(q or "")
    body = f"""
    <header>
      <h1>{html.escape(project)} &middot; {html.escape(title)}</h1>
      <div class="filters">
        <input type="hidden" name="project" value="{html.escape(project)}">
        <input name="q" value="{q_value}" placeholder="Filter by name">
        <button type="submit">Filter</button>
      </div>
    </header>
    {table_html or f"<p>{html.escape(empty_message)}</p>"}
    """
    return page(
        body,
        title=f"{title} - Jira Workbench",
        sidebar_extra=_meta_sidebar(projects, project, section),
        active_nav="meta",
    )


def create_app(jira_dir: Path, config: WorkbenchConfig, config_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Jira Workbench API")

    @app.get("/api/items")
    def list_items(
        project: list[str] | None = Query(None),
        status: list[str] | None = Query(None),
        component: list[str] | None = Query(None),
        fixVersion: list[str] | None = Query(None),  # noqa: N803 -- matches filter_items' own field_filters key
        assignee: list[str] | None = Query(None),
        pattern: str | None = None,
        active: bool = True,
        board: str | None = None,
        board_scope: str | None = None,
    ) -> dict[str, Any]:
        field_filters = {
            key: values
            for key, values in (
                ("project", [v for v in (project or []) if v]),
                ("status", [v for v in (status or []) if v]),
                ("component", [v for v in (component or []) if v]),
                ("fixVersion", [v for v in (fixVersion or []) if v]),
                ("assignee", [v for v in (assignee or []) if v]),
            )
            if values
        }
        try:
            return items_data(
                jira_dir,
                config,
                field_filters=field_filters,
                pattern=pattern,
                active=active,
                board=board,
                board_scope=board_scope,
            )
        except ViewError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/items/{key}")
    def get_item(key: str) -> dict[str, Any]:
        try:
            detail = item_detail_data(jira_dir, key)
        except MetadataError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if detail is None:
            raise HTTPException(status_code=404, detail=f"work item {key} is not synced locally")
        return detail

    @app.get("/api/meta/versions")
    def get_versions(project: str | None = None) -> dict[str, Any]:
        try:
            return {"versions": meta_versions_data(jira_dir, config, project)}
        except MetadataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/meta/components")
    def get_components(project: str | None = None, component_field: str | None = None) -> dict[str, Any]:
        try:
            return {"components": meta_components_data(jira_dir, config, project, component_field)}
        except MetadataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/meta/boards")
    def get_boards(project: str | None = None, component_field: str | None = None) -> dict[str, Any]:
        try:
            return {"boards": meta_boards_data(jira_dir, config, project, component_field)}
        except MetadataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/", response_class=HTMLResponse)
    def items_page(
        request: Request,
        project: list[str] | None = Query(None),
        status: list[str] | None = Query(None),
        component: list[str] | None = Query(None),
        fixVersion: list[str] | None = Query(None),  # noqa: N803
        assignee: list[str] | None = Query(None),
        pattern: str | None = None,
        active: str | None = None,
        board: str | None = None,
        board_scope: str | None = None,
        show_released: str | None = None,
        sort: str | None = None,
        dir: str | None = None,  # noqa: A002 -- matches the query param name
    ) -> str:
        # A bare "/" (no query string at all -- a fresh nav click, not a
        # filter-form submission) picks up the same saved [view] defaults
        # every interface (CLI/TUI/web) reads from config.toml, so saving a
        # filter from any one of them takes effect in the others on next
        # load. Once any filter param is present the request is explicit
        # and wins outright, including an intentionally-cleared (empty)
        # field -- sort/dir don't gate this, they're view-only and have no
        # saved-default equivalent.
        if not request.query_params:
            project = list(config.view_project or ())
            status = list(config.view_status or ())
            component = list(config.view_component or ())
            fixVersion = list(config.view_fix_version or ())
            assignee = list(config.view_assignee or ())
            pattern = config.view_filter
            active = "1" if config.view_active is None or config.view_active else "0"
            board = config.view_board
            board_scope = config.view_board_scope

        field_filters = {
            key: values
            for key, values in (
                ("project", [v for v in (project or []) if v]),
                ("status", [v for v in (status or []) if v]),
                ("component", [v for v in (component or []) if v]),
                ("fixVersion", [v for v in (fixVersion or []) if v]),
                ("assignee", [v for v in (assignee or []) if v]),
            )
            if values
        }
        active_only = active == "1"
        try:
            base = all_items(jira_dir, config)
        except ViewError:
            return page("<h1>Jira Workbench</h1><p>No manifest found. Run <code>jira-wb sync</code>.</p>")
        filtered = filter_items(
            base,
            field_filters=field_filters,
            pattern=pattern,
            active=active_only,
            board=board or None,
            board_scope=board_scope or None,
        )
        show_released_only = show_released == "1"
        query: dict[str, Any] = {
            "project": project or [],
            "status": status or [],
            "component": component or [],
            "fixVersion": fixVersion or [],
            "assignee": assignee or [],
            "pattern": pattern or "",
            "active": "1" if active_only else "0",
            "board": board or "",
            "board_scope": board_scope or "",
            "show_released": "1" if show_released_only else "",
            "sort": sort or "",
            "dir": dir or "asc",
        }
        boards = sorted(
            {
                str(b.get("name"))
                for b in load_cached_boards(jira_dir)
                if b.get("name") and b.get("active", True)
            }
        )
        # Real versions cache, not just what happens to be assigned on
        # currently-synced items -- same source of truth the TUI's own
        # version picker reads from, so "unreleased/archived" means the
        # same thing in both places.
        selected_projects = [value for value in (project or []) if value]
        if selected_projects:
            allowed_version_names = {
                name
                for selected_project in selected_projects
                for name in version_options(jira_dir, project=selected_project, include_released=show_released_only)
            }
        else:
            allowed_version_names = set(version_options(jira_dir, include_released=show_released_only))
        allowed_version_names = {name.strip().lower() for name in allowed_version_names}
        return render_items_page(filtered, len(base), query, base, boards, allowed_version_names)

    @app.post("/save-filter")
    def save_filter(
        project: list[str] = Form([]),
        status: list[str] = Form([]),
        component: list[str] = Form([]),
        fixVersion: list[str] = Form([]),  # noqa: N803
        assignee: list[str] = Form([]),
        pattern: str = Form(""),
        active: str = Form("0"),
        board: str = Form(""),
        board_scope: str = Form(""),
        show_released: str = Form(""),
        sort: str = Form(""),
        dir: str = Form("asc"),  # noqa: A002 -- matches the form field name
    ) -> RedirectResponse:
        if config_path is None:
            raise HTTPException(status_code=400, detail="no config file location known")
        try:
            save_view_defaults(
                config_path,
                {
                    "project": project,
                    "status": status,
                    "component": component,
                    "fix_version": fixVersion,
                    "assignee": assignee,
                    "filter": pattern or None,
                    "active": active == "1",
                    "board": board or None,
                    "board_scope": board_scope or None,
                    # show_released is deliberately not saved -- like the
                    # TUI's own version-picker toggle, it's a transient
                    # per-view reveal, not a persisted default.
                },
            )
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        query_string = urlencode(
            {
                "project": project,
                "status": status,
                "component": component,
                "fixVersion": fixVersion,
                "assignee": assignee,
                "pattern": pattern,
                "active": active,
                "board": board,
                "board_scope": board_scope,
                "show_released": show_released,
                "sort": sort,
                "dir": dir,
            },
            doseq=True,
        )
        return RedirectResponse(url=f"/?{query_string}", status_code=303)

    @app.get("/items/{key}", response_class=HTMLResponse)
    def item_page(key: str) -> str:
        detail = item_detail_data(jira_dir, key)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"work item {key} is not synced locally")
        component_field = config.effective_component_field(config.default_project_key())
        return render_item_detail_page(key, detail, component_field)

    def _configured_projects() -> list[str]:
        return [p.key for p in config.resolved_projects()]

    @app.get("/meta", response_class=HTMLResponse)
    def meta_page() -> str:
        return render_meta_projects_page(_configured_projects())

    @app.get("/meta/versions", response_class=HTMLResponse)
    def meta_versions_page(project: str | None = None, q: str | None = None) -> str:
        projects = _configured_projects()
        if not project:
            return render_meta_projects_page(projects, section="versions")
        try:
            versions = meta_versions_data(jira_dir, config, project)
        except MetadataError:
            versions = []
        filtered = _filter_by_name(versions, q)
        table = _generic_rows(filtered, ["name", "id", "released", "archived"]) if filtered else ""
        empty_message = "No versions match that filter." if q else "No versions loaded for this project."
        return render_meta_section_page("versions", project, projects, table, q, empty_message)

    @app.get("/meta/components", response_class=HTMLResponse)
    def meta_components_page(project: str | None = None, q: str | None = None) -> str:
        projects = _configured_projects()
        if not project:
            return render_meta_projects_page(projects, section="components")
        try:
            components = meta_components_data(jira_dir, config, project)
        except MetadataError:
            components = []
        filtered = _filter_by_name(components, q, name_fn=component_name)
        table = _components_rows(filtered) if filtered else ""
        empty_message = "No components match that filter." if q else "No components loaded for this project."
        return render_meta_section_page("components", project, projects, table, q, empty_message)

    @app.get("/meta/boards", response_class=HTMLResponse)
    def meta_boards_page(project: str | None = None, q: str | None = None) -> str:
        projects = _configured_projects()
        if not project:
            return render_meta_projects_page(projects, section="boards")
        try:
            boards = meta_boards_data(jira_dir, config, project)
        except MetadataError:
            boards = []
        filtered = _filter_by_name(boards, q)
        table = _boards_rows(filtered) if filtered else ""
        empty_message = "No boards match that filter." if q else "No boards loaded for this project."
        return render_meta_section_page("boards", project, projects, table, q, empty_message)

    return app


def serve(
    jira_dir: Path, host: str, port: int, config: WorkbenchConfig | None = None, config_path: Path | None = None
) -> None:
    app = create_app(jira_dir, config if config is not None else WorkbenchConfig(), config_path)
    print(f"Serving Jira Workbench at http://{host}:{port}")
    uvicorn.run(app, host=host, port=int(port), log_level="warning")
