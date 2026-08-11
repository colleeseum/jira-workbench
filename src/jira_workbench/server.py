from __future__ import annotations

import html
import json
import re
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import ConfigError, WorkbenchConfig, save_view_defaults
from .issue import (
    IssueError,
    build_create_fields,
    create_issue,
    fetch_current_user,
    fetch_issue_type_fields,
)
from .metadata import (
    MetadataError,
    add_local_board,
    boards_path,
    component_name,
    delete_local_board,
    fetch_rank_field_id,
    is_project_read_only,
    load_assignees,
    load_boards,
    load_component_field_options,
    load_components,
    load_field_names,
    load_versions,
    move_issue_to_backlog,
    move_issue_to_board,
    normalize_components,
    normalize_versions,
    rank_issue_before,
    refresh_boards_api,
    set_board_active,
    version_name,
)
from .service import (
    LabelBulkEditResult,
    add_meta_component,
    add_meta_component_field_option,
    add_meta_version,
    api_client_from_config,
    archive_meta_version,
    bulk_edit_label,
    delete_meta_version,
    load_meta_boards,
    load_meta_components,
    load_meta_versions,
    release_meta_version,
    rename_meta_version,
    version_identifier,
)
from .shadow import (
    ShadowError,
    add_comment,
    delete_comment,
    delete_shadow,
    edit_comment,
    field_value,
    is_local_comment_id,
    issue_path,
    load_shadow,
    push_shadows,
    refresh_local_issue_after_push,
    remove_local_comment,
    render_diff,
    set_field,
    set_status_change,
    undelete_comment,
    user_payload,
)
from .sync import (
    find_existing_issue,
    issue_project_key,
    read_json,
    write_json,
)
from .view import (
    ViewError,
    apply_shadow,
    as_dict,
    as_list,
    avatar_url,
    comma_parts,
    component_counts,
    display_component,
    display_name,
    distinct_field_values,
    editable_detail_fields,
    editable_field_value,
    encode_edit_value,
    filter_items,
    filtered_manifest_items,
    hierarchy_component,
    is_archived_version,
    issue_key_from_text,
    label_counts,
    list_comments,
    load_cached_boards,
    load_manifest_items,
    modified_issue_keys,
    observed_field_options,
    observed_status_category_map,
    normalize_swimlane,
    parent_options,
    pill_color,
    PRIORITY_RANK,
    selectable_field_options,
    SWIMLANE_MODES,
    swimlane_label,
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


# load_manifest_items used to re-read and re-enrich every individual
# issue.json from scratch on every call -- multiple real seconds on a few
# thousand synced issues, which is why this used to be wrapped in a
# generation-counter + TTL cache. It's now backed by index.db (see db.py)
# and reads in well under a second even on a large real dataset, so the
# cache was removed rather than kept as a safety margin -- it was also a
# repeat source of real staleness bugs (any metadata mutation that forgot
# to bump manifest_generation, e.g. a fix-version rename, silently served
# stale data for up to its TTL). Revisit only if a future profile shows
# this call actually dominating a page's render time again.
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

# Used only for the Parent dropdown's option text (e.g. "⚡ SAT-100 ..."
# instead of "Epic: SAT-100 ...") -- Type/Priority themselves use the real
# colored SVG icons beside their <select> instead (see _type_icon_cell/
# _priority_icon_cell below), to stay visually consistent with the Items
# list table's own icons rather than introducing a second, different-
# looking icon language just because a plain <option> can't render SVG.
TYPE_EMOJI: dict[str, str] = {
    "epic": "⚡",
    "story": "🔖",
    "task": "✅",
    "bug": "🐛",
    "sub-task": "↳",
    "subtask": "↳",
}
DEFAULT_TYPE_EMOJI = "◯"


def _type_icon_cell(name: str) -> str:
    key = name.strip().lower() if name else ""
    svg, color = TYPE_ICONS_SVG.get(key, DEFAULT_TYPE_ICON_SVG)
    title = html.escape(name) if name else "Unknown type"
    return f'<span style="color: {color}" title="{title}">{svg}</span>'


NO_PRIORITY_ICON_HTML = '<span style="opacity: .4">—</span>'


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


def _goto_section(form_wrapped: bool) -> str:
    """A "Goto: <key> [Go]" box in the sidebar of every page -- for jumping
    straight to a specific issue's Detail page even when it isn't part of
    whatever filter (or no filter at all) the current page happens to show.
    Submits GET /goto?goto_key=... , which resolves/validates the key and
    redirects to /items/{KEY}.

    Can't just always wrap itself in its own <form> -- most pages now use
    form_wrapped=False (their own body has independent per-field POST
    forms), but the couple that still don't (New Issue, the project-less
    Meta picker) wrap their *entire* body in one <form method="get">
    already, and nesting a second <form> inside that would be invalid
    HTML. So: own small form when the page has no outer one, or a plain
    input + a formaction-overridden submit button riding along on the
    outer form when it does -- the same override trick this app already
    uses for the New Issue page's "Create issue" button.
    """
    if form_wrapped:
        return """
        <div class="sidebar-section">
          <label for="sidebar-goto">Goto</label>
          <input type="text" name="goto_key" id="sidebar-goto" placeholder="e.g. PROJ-123">
          <button type="submit" formaction="/goto" formmethod="get">Go</button>
        </div>
        """
    return """
    <form method="get" action="/goto" class="sidebar-section">
      <label for="sidebar-goto">Goto</label>
      <input type="text" name="goto_key" id="sidebar-goto" placeholder="e.g. PROJ-123">
      <button type="submit">Go</button>
    </form>
    """


def page(
    body: str,
    *,
    title: str = "Jira Workbench",
    sidebar_extra: str = "",
    active_nav: str = "items",
    form_wrapped: bool = True,
) -> str:
    # One <form> per page, owned here, so every page (items/meta) shares
    # the exact same sidebar chrome without ever nesting a second <form>
    # inside it (invalid HTML) -- omitting `action` means it submits back
    # to whatever path it's rendered on, which is already correct for each
    # page (items' own filter fields resubmit to "/", meta's project field
    # resubmits to its own section path), no per-page action needed.
    #
    # `form_wrapped=False` (Detail page only) swaps that single outer
    # <form> for a plain <div> instead: Detail's sidebar has no GET fields
    # needing a shared submit (sidebar_extra defaults to "" there), and its
    # body needs many independent small <form method="post"> blocks (one
    # per editable field, one per comment, revert/push) -- HTML forbids
    # nesting a <form> inside another <form>, so those would be silently
    # broken by browsers if the outer wrapper stayed a <form> too.
    wrapper_tag = "form" if form_wrapped else "div"
    wrapper_open = '<form method="get" class="layout">' if form_wrapped else '<div class="layout">'

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
    html, body {{ height: 100%; }}
    body {{ margin: 0; line-height: 1.4; overflow: hidden; }}
    header {{ margin-bottom: 24px; }}
    input, select {{ padding: 6px 8px; }}
    button {{ padding: 6px 10px; }}
    .button-link {{
      display: inline-block; padding: 6px 10px; border: 1px solid #8885; border-radius: 4px;
      text-decoration: none; color: inherit; background: ButtonFace;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #9995; padding: 8px; text-align: left; vertical-align: top; }}
    /* z-index -- position: sticky alone doesn't guarantee this paints
    above the tbody rows that scroll underneath it: with no z-index, a
    sticky element and its later DOM siblings sit at the same stacking
    level, so once a row scrolls up to the same screen position as the
    "stuck" header, paint order (not sticky-ness) decides who's on top --
    and later-in-DOM tbody rows would win, making the header look like it
    scrolled *under* the list instead of staying above it. Deliberately
    lower than the sidebar filter's own open .checklist popup (z-index 10)
    and the icon-select-menu popup (z-index 20) -- this only needs to beat
    plain, unpositioned body rows (stacking level 0); it must still lose
    to any real dropdown that happens to open near the same screen
    position while scrolled, or the header would wrongly paint over it. */
    th {{ position: sticky; top: 0; z-index: 1; background: Canvas; }}
    /* A fixed grid (via the <colgroup> widths above), not the browser's
    default content-based sizing -- on a wide screen, auto-sizing was
    handing the Status/Component/Assignee dropdowns (each width: 100% of
    their own td, see .row-field-cell below) way more room than they
    need, at Summary's expense (the one column that actually benefits
    from extra width). The narrow columns stay a fixed, comfortable size;
    Summary (the only <col> left with no explicit width) absorbs
    whatever's left over. */
    .items-table {{ table-layout: fixed; }}
    /* :not(.pill-select-cell) -- every editable field's own open dropdown
    (an absolutely-positioned div, not a native <select>) is a child of its
    td and escapes the td's box on purpose; clipping every td's overflow
    indiscriminately clipped that open menu right along with any long text,
    making it look like the dropdown vanished behind the next row.
    :has(.pill-select-menu) -- Status/Component/Fix Versions' own dropdowns
    live in a .pill-select-row-cell, not .pill-select-cell (they need the
    wider, roomier trigger those get, not the compact icon-only one) --
    same clipped-menu bug as above would otherwise recur there
    specifically. */
    .items-table td:not(.pill-select-cell):not(:has(.pill-select-menu)) {{ overflow: hidden; }}
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
    /* .layout is pinned to the viewport height (not just min-height) so it
    never grows past the screen -- .sidebar and .content each scroll their
    own overflow independently instead of the whole page scrolling as one,
    which used to carry the nav out of view along with long content. */
    .layout {{ display: flex; align-items: stretch; gap: 0; height: 100vh; }}
    .content {{ flex: 1 1 auto; min-width: 0; padding: 24px; overflow-y: auto; height: 100%; box-sizing: border-box; }}
    /* Sidebar-collapse checkbox hack -- toggles the whole pane down to a
    thin strip, no JavaScript. Must be a sibling of .layout (not nested
    inside it) for the ~ selector below to reach .sidebar. */
    #sidebar-collapsed {{ display: none; }}
    .sidebar {{
      flex: 0 0 auto; width: 220px; min-width: 160px; max-width: 480px; box-sizing: border-box;
      resize: horizontal; overflow-y: auto; overflow-x: hidden; height: 100%;
      border-right: 1px solid #8885; padding: 12px;
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
    .sidebar-section input[type="text"] {{ width: 100%; box-sizing: border-box; }}
    /* Item Detail page: a compact "Details" grid (Type/Status/Priority/...)
    instead of one full-width table row per field -- several small fields
    side by side reads the way Jira's own detail panel does, and stops the
    page scrolling forever for fields that only need a few words. */
    .detail-summary {{ display: flex; align-items: center; gap: 6px; }}
    .detail-summary form {{ flex: 1 1 auto; min-width: 0; }}
    .detail-summary input {{ font-size: 1.2em; font-weight: 600; padding: 6px 8px; }}
    .detail-fields {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 4px 20px; margin: 16px 0;
    }}
    .detail-field label, .detail-field-full label {{
      display: block; font-size: 0.75em; opacity: 0.65; margin-bottom: 2px;
      text-transform: uppercase; letter-spacing: 0.02em;
    }}
    .detail-field select, .detail-field input[type="text"] {{ width: 100%; box-sizing: border-box; }}
    .detail-field-full {{ margin-bottom: 16px; }}
    .detail-field-full input[type="text"] {{ width: 100%; box-sizing: border-box; }}
    /* A field present in the local shadow (i.e. edited but not yet pushed)
    gets a small colored accent -- otherwise there's no way to tell "this
    differs from Jira" apart from "this is just what Jira already has"
    without opening the Local diff details below. */
    .detail-field.modified, .detail-field-full.modified,
    .detail-summary.modified, .detail-description.modified {{
      border-left: 3px solid #2563eb; padding-left: 8px; margin-left: -11px;
    }}
    .modified-badge {{ color: #2563eb; font-size: 0.9em; cursor: default; }}
    .local-changes-bar {{ display: flex; align-items: center; gap: 10px; margin: 8px 0; }}
    .local-diff {{ flex: 1 1 auto; min-width: 0; }}
    .local-diff summary {{ cursor: pointer; font-size: 0.85em; opacity: 0.8; }}
    .local-diff pre {{ margin-top: 6px; }}
    /* A fixed grid layout plus explicit column shares -- each modified
    issue gets its own separate diff grid (so one long value in one issue
    can't widen every other issue's columns too), but left to size itself
    from only its own content, the browser put "Before"/"After" at a
    different x position per issue. Explicit shares make every one of
    these line up the same way regardless of its own content. */
    .diff-table {{ width: 100%; margin: 8px 0 16px; border-collapse: collapse; table-layout: fixed; }}
    .diff-table th, .diff-table td {{
      padding: 4px 8px; text-align: left; border-bottom: 1px solid #8883; overflow-wrap: break-word;
    }}
    .diff-table th:first-child, .diff-table td:first-child {{ width: 20%; }}
    .diff-table th:not(:first-child), .diff-table td:not(:first-child) {{ width: 40%; }}
    .diff-table thead th {{ font-size: 0.8em; opacity: 0.7; font-weight: 600; position: static; background: none; }}
    .diff-table tbody th {{ font-weight: 600; white-space: nowrap; }}
    .diff-before {{ color: #b91c1c; text-decoration: line-through; opacity: 0.8; }}
    .diff-after {{ color: #15803d; font-weight: 600; }}
    .btn {{
      padding: 4px 12px; font-size: 0.85em; font-weight: 600; border-radius: 4px;
      border: 1px solid transparent; cursor: pointer; white-space: nowrap;
    }}
    .btn-danger {{ background: #dc2626; border-color: #dc2626; color: #fff; }}
    .btn-danger:hover {{ background: #b91c1c; }}
    .btn-primary {{ background: #2563eb; border-color: #2563eb; color: #fff; }}
    .btn-primary:hover {{ background: #1d4ed8; }}
    /* Custom icon dropdown (Type/Priority on the Detail page) -- a real
    <select>'s <option> list can only ever hold plain text, so the exact
    colored SVG icon shown in the Items list table can't render inside one;
    this is a small custom listbox instead, see _icon_select_editor_html. */
    .icon-select {{ position: relative; }}
    .icon-select-trigger {{
      display: flex; align-items: center; gap: 6px; width: 100%; box-sizing: border-box;
      padding: 6px 8px; border: 1px solid #8885; border-radius: 4px;
      background: Field; color: FieldText; cursor: pointer; font: inherit; text-align: left;
    }}
    /* A native <select> (Status/Component/...) always shows its own
    dropdown-arrow affordance -- without this, this custom widget's own
    trigger looked like a plain button/label with no visual hint that
    clicking it opens a list of choices. Left off the compact Items-list
    variant below (.icon-select-cell) on purpose -- Type/Priority/
    Assignee's triggers are already tight on space, icon-only or icon-
    plus-a-couple-characters, and this would read as clutter there. */
    .icon-select-trigger::after {{ content: "\\25be"; margin-left: auto; opacity: 0.6; flex-shrink: 0; }}
    .icon-select-cell .icon-select-trigger::after {{ display: none; }}
    .icon-select-menu {{
      display: none; position: absolute; top: 100%; left: 0; z-index: 20; margin-top: 2px;
      background: Canvas; border: 1px solid #8885; border-radius: 6px; box-shadow: 0 4px 12px #0004;
      min-width: 180px; max-height: 240px; overflow-y: auto;
    }}
    .icon-select-menu.open {{ display: block; }}
    .icon-select-option {{ display: flex; align-items: center; gap: 6px; padding: 6px 10px; cursor: pointer; white-space: nowrap; }}
    .icon-select-option:hover {{ background: #8882; }}
    .icon-select-option.selected {{ font-weight: 600; }}
    /* Compact variant for the Items list table -- the Detail page's
    field-sized trigger button would make every row noticeably taller. */
    .icon-select-cell form {{ margin: 0; }}
    .icon-select-cell .icon-select-trigger {{
      /* max-width + overflow: hidden on the TRIGGER itself, not the td --
      Type's own label (e.g. "Sub-task") can be wider than its narrow
      column, and the td's own overflow can't clip it without also
      clipping the open dropdown menu below (a sibling, not a descendant
      of this button, so it's unaffected by this rule). Without this, a
      long label spills visibly into the Key column next to it. */
      width: auto; max-width: 100%; padding: 2px 4px; border: none; background: transparent;
      overflow: hidden;
    }}
    .icon-select-cell .icon-select-trigger:hover {{ background: #8882; border-radius: 4px; }}
    /* Same compactness treatment for the other row-level field editors
    (Status/Component/Summary/Assignee/fixVersions) -- the Detail page's
    roomy field-sized inputs would make every row noticeably taller. */
    .row-field-cell form {{ margin: 0; }}
    .row-field-cell select, .row-field-cell input[type="text"], .row-field-cell .icon-select-trigger {{
      width: 100%; box-sizing: border-box; padding: 2px 4px; border: 1px solid transparent; background: transparent;
    }}
    .row-field-cell select:hover, .row-field-cell input[type="text"]:hover, .row-field-cell .icon-select-trigger:hover {{
      border-color: #8885;
    }}
    .row-field-cell select:focus, .row-field-cell input[type="text"]:focus {{ border-color: #8885; background: Field; }}
    .resolution-picker {{ display: block; margin-top: 4px; }}
    /* The Items list's one unified dropdown (Type/Priority/Status/
    Component/Assignee/Fix Version all use this, single- or multi-select
    -- see _pill_select_row_trigger_html/PILL_SELECT_SCRIPT), replacing
    what used to be four different widget implementations. Deliberately
    a <div role="button">, not a real <button>, for the trigger -- a
    multi-select's own pills each carry a real removable "x", and a
    <button> can never legally contain another <button>/interactive
    control (see jiraWbPillRemove's own docstring). */
    .pill-select {{ position: relative; }}
    .pill-select-trigger {{
      display: flex; align-items: center; gap: 4px; flex-wrap: wrap; width: 100%; box-sizing: border-box;
      min-height: 1.6em; padding: 4px 8px; border: 1px solid #8885; border-radius: 4px;
      background: Field; color: FieldText; cursor: pointer;
    }}
    .pill-select-trigger::after {{ content: "\\25be"; margin-left: auto; opacity: 0.6; flex-shrink: 0; }}
    /* The Items list's row triggers (both compact icon-only cells and the
    wider Status/Component/Fix Version ones) drop the dropdown-chevron
    affordance entirely -- no visible "combo box" cue at all, just the
    pill(s); clicking anywhere on them opens the selection menu directly. */
    .pill-select-cell .pill-select-trigger::after, .pill-select-row-cell .pill-select-trigger::after {{ display: none; }}
    .pill-select-pills {{ display: flex; align-items: center; gap: 4px; flex-wrap: wrap; min-width: 0; }}
    .pill-select-pills .pill {{ display: inline-flex; align-items: center; gap: 4px; margin-right: 0; }}
    .pill-select-empty {{ opacity: 0.5; }}
    .pill-remove {{
      display: inline-flex; align-items: center; justify-content: center; width: 1.1em; height: 1.1em;
      margin-left: 2px; border-radius: 50%; cursor: pointer; opacity: 0.6; line-height: 1;
    }}
    .pill-remove:hover {{ opacity: 1; background: #8884; }}
    .pill-select-menu {{
      display: none; position: absolute; top: 100%; left: 0; z-index: 20; margin-top: 2px;
      background: Canvas; border: 1px solid #8885; border-radius: 6px; box-shadow: 0 4px 12px #0004;
      min-width: 220px; max-width: 340px;
    }}
    .pill-select-menu.open {{ display: block; }}
    .pill-select-filter {{
      display: block; width: 100%; box-sizing: border-box; padding: 6px 8px; border: none;
      border-bottom: 1px solid #8885; background: Canvas; color: FieldText;
    }}
    .pill-select-options {{ max-height: 240px; overflow-y: auto; }}
    .pill-select-option {{
      display: flex; align-items: center; gap: 6px; padding: 6px 10px; cursor: pointer; white-space: nowrap;
      border-left: 3px solid transparent;
    }}
    .pill-select-option:hover {{ background: #8882; }}
    /* No checkbox (see _pill_select_options_template_html) -- selection is
    an accent-colored left bar plus a matching checkmark at the row's own
    trailing edge instead, so it reads at a glance rather than needing the
    bold weight alone to be noticed. border-left is reserved (transparent)
    on every row, selected or not, so gaining the color never shifts the
    row's own content sideways. */
    .pill-select-option.selected {{ font-weight: 600; border-left-color: #2563eb; }}
    .pill-select-option.selected::after {{ content: "\\2713"; margin-left: auto; padding-left: 12px; color: #2563eb; }}
    /* Fix Version's Released/Unreleased section headers (see
    fix_version_groups_for) -- matching Jira's own grouped Fix Version
    dropdown look. */
    .pill-select-group-header {{
      padding: 6px 10px 2px; font-size: 0.75em; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.03em; opacity: 0.6;
    }}
    .pill-select-group-header:not(:first-child) {{ margin-top: 4px; padding-top: 8px; border-top: 1px solid #8883; }}
    /* Compact variant for the Items list table -- same reasoning as
    .icon-select-cell/.row-field-cell above. */
    .pill-select-cell form, .pill-select-row-cell form {{ margin: 0; }}
    .pill-select-cell .pill-select-trigger {{
      width: auto; max-width: 100%; padding: 2px 4px; border: none; background: transparent; min-height: 0;
    }}
    .pill-select-row-cell .pill-select-trigger {{ padding: 2px 4px; border-color: transparent; background: transparent; }}
    .pill-select-cell .pill-select-trigger:hover, .pill-select-row-cell .pill-select-trigger:hover {{
      background: #8882; border-radius: 4px;
    }}
    .swimlane-header td {{ background: #8882; font-weight: 700; padding: 6px 8px; cursor: pointer; user-select: none; }}
    .swimlane-header td::before {{ content: "\\25be  "; }}
    .swimlane-header.collapsed td::before {{ content: "\\25b8  "; }}
    .new-issue-fab {{
      display: inline-flex; align-items: center; justify-content: center; width: 36px; height: 36px;
      border-radius: 50%; background: #2563eb; color: #fff; font-size: 1.5em; line-height: 1;
      text-decoration: none; margin-top: 12px;
    }}
    .new-issue-fab:hover {{ background: #1d4ed8; }}
    /* flash-success/flash-error render as plain text elsewhere on
    purpose (this app's default is to add styling only when it earns its
    keep) -- warning gets a real accent because it's specifically the
    "created, but you can't see it from here" case, meant to catch the
    eye and invite a click through to the item, not just confirm a
    routine save. */
    .flash-warning {{
      border-left: 3px solid #d97706; background: #fef3c7; padding: 6px 10px; color: #92400e;
    }}
    .flash-warning a {{ color: inherit; font-weight: 600; }}
    .new-issue-fab-small {{
      display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px;
      border-radius: 50%; background: #2563eb; color: #fff; font-size: 0.9em; line-height: 1;
      text-decoration: none; vertical-align: middle;
    }}
    .new-issue-fab-small:hover {{ background: #1d4ed8; }}
    tr[draggable="true"] {{ cursor: grab; }}
    tr[draggable="true"]:active {{ cursor: grabbing; }}
    .avatar-circle {{
      display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
      width: 22px; height: 22px; border-radius: 50%; color: #fff; font-size: 0.65em; font-weight: 700;
    }}
    img.avatar-circle {{ display: inline-block; object-fit: cover; }}
    .avatar-unassigned {{ background: #8888; }}
    .btn-spinner {{
      display: inline-block; width: 0.8em; height: 0.8em; margin-right: 0.4em;
      border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%;
      vertical-align: -0.15em; animation: jira-wb-spin 0.7s linear infinite;
    }}
    @keyframes jira-wb-spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <input type="checkbox" id="sidebar-collapsed">
  {wrapper_open}
    <aside class="sidebar">
      <label for="sidebar-collapsed" class="sidebar-toggle" title="Collapse/expand sidebar">☰</label>
      <div class="sidebar-inner">
        <nav class="sidebar-nav">
          {nav_link("/", "items", "Items")}
          {nav_link("/meta", "meta", "Meta")}
        </nav>
        {_goto_section(form_wrapped)}
        {sidebar_extra}
      </div>
    </aside>
    <main class="content">
{body}
    </main>
  </{wrapper_tag}>
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

    Stays closed by default even with a selection already made -- only the
    badge count shows until you actually click it open -- except when
    `force_open` says otherwise: a control that submits the form from
    inside here (like the "show released" checkbox) reloads the whole page,
    and the freshly-rendered <details> would otherwise snap shut again,
    undoing the very thing you just clicked open to look at."""
    open_attr = " open" if force_open else ""
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


# The Items page's own filter <form> (see _filter_form) -- the sidebar's
# Board/Scope selects live in <aside>, physically outside that form's DOM
# subtree (page()'s own template keeps <aside> and <main> as siblings),
# so they use HTML5's form="..." attribute to submit as part of it anyway
# rather than needing their own separate (and value-losing) form.
ITEMS_FILTER_FORM_ID = "items-filter-form"

# A synthetic "board" -- not a real Jira board, not even a user-defined
# local board (metadata.py's add_local_board, which still matches by real
# field values) -- selecting it filters to whatever currently has a local
# shadow (view.py's modified_issue_keys), bypassing real board-membership
# matching entirely. Deliberately excluded from sections_mode/swimlane
# grouping (server.py's render_items_page, _board_scope_section): "modified"
# isn't a real board with an Active/Backlog split, so it always renders as
# one flat list -- picking it forces swimlane off and hides the Scope
# select rather than leaving either as an inert, confusing control.
MODIFIED_BOARD_NAME = "Modified Items"

# Remembers the last filter/sort/swimlane state *per board* (including the
# no-board-selected case, keyed by "") -- read back both on a bare "/" nav
# (plain reload) and whenever the board itself changes (the sidebar's Board
# <select> resubmits the whole filter form, carrying the *previous* board's
# other filter values along with it; those are stale for the newly picked
# board, so this swaps in whatever was last used for that board instead of a
# single global "last view"). config.toml's saved [view] defaults still win
# on a genuinely first-ever visit (no cookie at all yet) -- this is a
# per-browser "last view per board", not a replacement for the cross-
# interface "saved default" /save-filter already writes to config.toml.
# JSON-encoded: {"current_board": "<board or "">", "per_board": {"<board>":
# "<urlencoded query string, without the board field itself>"}}.
LAST_VIEW_COOKIE = "jira_wb_last_view"


def _load_view_cookie(request: Request) -> dict[str, Any]:
    raw = request.cookies.get(LAST_VIEW_COOKIE)
    empty: dict[str, Any] = {"current_board": None, "per_board": {}}
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except ValueError:
        # Also covers an old-format cookie from before this per-board
        # scheme (a bare query string, not JSON) -- treated as "no cookie
        # yet" rather than crashing; the very next request re-establishes
        # it in the new shape.
        return empty
    if not isinstance(data, dict):
        return empty
    current_board = data.get("current_board")
    per_board = data.get("per_board")
    return {
        "current_board": current_board if isinstance(current_board, str) else None,
        "per_board": (
            {k: v for k, v in per_board.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(per_board, dict)
            else {}
        ),
    }


def _fields_from_query_string(query_string: str) -> dict[str, Any]:
    """Splits a stored (or incoming) urlencoded query string back into the
    same field shape items_page's own params take -- shared by every place
    that restores a remembered view (bare-nav cookie restore, and a
    board-change swapping in that new board's own last-remembered filters)."""
    parsed = parse_qs(query_string, keep_blank_values=True) if query_string else {}

    def values(name: str) -> list[str]:
        return parsed.get(name, [])

    def value(name: str, default: str = "") -> str:
        found = parsed.get(name)
        return found[0] if found else default

    return {
        "project": values("project"),
        "status": values("status"),
        "component": values("component"),
        "fixVersion": values("fixVersion"),
        "assignee": values("assignee"),
        "pattern": value("pattern"),
        "active": value("active", "1"),
        "board_scope": value("board_scope"),
        "swimlane": value("swimlane"),
        "show_released": value("show_released"),
        "sort": value("sort"),
        "dir": value("dir", "desc"),
    }


def _board_scope_section(query: dict[str, Any], boards: list[str]) -> str:
    """Items-page-only sidebar content -- Board/Scope/Swimlane; the natural
    home for whatever other view-level (not item-level) controls get added
    later (saved views, project switcher, etc.), which is exactly why this
    is its own section rather than folded into the filter chips. Nested in
    a <details> so it can fold away on its own independent of the whole
    sidebar's collapse (see page()'s #sidebar-collapsed checkbox).

    All three submit on change (the one deliberate bit of JavaScript in
    this whole page, everything else here is JS-free by design) -- unlike
    the checkbox filter fields, where you usually want to tick several
    values before applying, a single-value dropdown reads as "pick one and
    it takes effect immediately"; requiring a separate Apply click here was
    confusing (looked like changing it did nothing)."""
    board_value = query.get("board") or ""
    board_scope = query.get("board_scope") or ""
    swimlane_value = query.get("swimlane") or "none"
    swimlane_options_html = "".join(
        f'<option value="{mode}"{" selected" if mode == swimlane_value else ""}>{mode.capitalize()}</option>'
        for mode in SWIMLANE_MODES
    )
    # The synthetic "Modified Items" board (MODIFIED_BOARD_NAME) has no
    # real Active/Backlog split and no grouping axis -- Scope/Swimlane
    # would just be inert controls for it, so they're hidden entirely
    # rather than left selectable-but-meaningless.
    scope_and_swimlane_html = ""
    if board_value != MODIFIED_BOARD_NAME:
        scope_and_swimlane_html = f"""
      <div class="sidebar-section">
        <label for="sidebar-board-scope">Scope</label>
        <select name="board_scope" id="sidebar-board-scope" form="{ITEMS_FILTER_FORM_ID}" onchange="this.form.submit()">
          <option value="">(any scope)</option>
          <option value="active" {"selected" if board_scope == "active" else ""}>Active</option>
          <option value="backlog" {"selected" if board_scope == "backlog" else ""}>Backlog</option>
        </select>
      </div>
      <div class="sidebar-section">
        <label for="sidebar-swimlane">Swimlane</label>
        <select name="swimlane" id="sidebar-swimlane" form="{ITEMS_FILTER_FORM_ID}" onchange="this.form.submit()">{swimlane_options_html}</select>
      </div>
        """
    return f"""
    <details open>
      <summary>View</summary>
      <div class="sidebar-section">
        <label for="sidebar-board">Board</label>
        <select name="board" id="sidebar-board" form="{ITEMS_FILTER_FORM_ID}" onchange="this.form.submit()">{_options_html(boards, board_value)}</select>
      </div>
      {scope_and_swimlane_html}
    </details>
    """


def _find_board_entry(jira_dir: Path, project: str, board_name: str) -> dict[str, Any] | None:
    cache = load_boards(jira_dir, project)
    boards = (cache or {}).get("boards") if cache else None
    if not isinstance(boards, list):
        return None
    for board in boards:
        if isinstance(board, dict) and str(board.get("name") or "") == board_name:
            return board
    return None


def _patch_board_order(
    jira_dir: Path,
    project: str,
    board_name: str,
    key: str,
    *,
    target_scope: str,
    before_key: str | None,
) -> None:
    """Keeps the locally cached meta/<project>/boards.json honest right
    after a live drag-drop/scope-move write to Jira succeeds, instead of
    only on the next manual `jira-wb meta refresh --boards` -- the real
    Jira write already happened by the time this runs; this is pure local
    bookkeeping so the very next page load shows the move, not stale state.
    """
    cache = load_boards(jira_dir, project)
    boards = (cache or {}).get("boards") if cache else None
    if not isinstance(boards, list):
        return
    for board in boards:
        if not isinstance(board, dict) or str(board.get("name") or "") != board_name:
            continue
        backlog_keys = [k for k in (board.get("backlogKeys") or []) if k != key]
        board_keys = [k for k in (board.get("boardKeys") or []) if k != key]
        target_list = backlog_keys if target_scope == "backlog" else board_keys
        if before_key and before_key in target_list:
            target_list.insert(target_list.index(before_key), key)
        else:
            target_list.append(key)
        board["backlogKeys"] = backlog_keys
        board["boardKeys"] = board_keys
        write_json(boards_path(jira_dir, project), cache)
        from . import db as _db

        _db.recompute_board_membership(jira_dir)
        return


def _clear_all_url(query: dict[str, Any]) -> str:
    # Sort/dir are view state, not a filter -- kept so "start over on
    # filters" doesn't also scramble whatever column you had sorted.
    params = {key: query[key] for key in ("sort", "dir") if query.get(key)}
    return f"/?{urlencode(params, doseq=True)}"


def _filter_form(jira_dir: Path, config: WorkbenchConfig, query: dict[str, Any], allowed_version_names: set[str]) -> str:
    from .db import distinct_projects_for_board, field_counts

    # Straight SQL GROUP BY counts instead of materializing/JSON-decoding
    # every indexed item to count them in Python -- project/status/
    # component/assignee are all plain promoted columns with no per-request
    # re-resolution concern (unlike fixVersion, see below).
    project_counts = field_counts(jira_dir, "project")
    project_field = _checkbox_field("Project", "project", project_counts, query["project"], query)

    # Status/Component/Fix version/Assignee are all scoped to whichever
    # project(s) are currently selected -- no point offering a status,
    # component, version, or assignee that doesn't occur anywhere in the
    # selected project(s); it can't match anything. Unscoped (shows every
    # value across every synced project) when no project filter is set yet.
    selected_projects_lower = {value.strip().lower() for value in query["project"]}
    # A selected board narrows this the same way an explicit Project
    # checkbox would, even when no checkbox is actually ticked -- picking
    # "SAT board" (which only ever matches SAT issues) used to still show
    # every other synced project's statuses/components/assignees, because
    # this only ever looked at the Project checkboxes, never at the board.
    # That's especially easy to hit now that switching to a never-before-
    # seen board resets the Project checkboxes to empty (see items_page's
    # per-board "clean slate"), leaving them empty on every later visit to
    # that same board too, once the empty selection gets remembered in the
    # per-board cookie.
    board = str(query.get("board") or "")
    if selected_projects_lower:
        scoped_projects = [name for name, _count in project_counts if name.strip().lower() in selected_projects_lower]
    elif board:
        scoped_projects = sorted(distinct_projects_for_board(jira_dir, board))
    else:
        scoped_projects = None
    project_scope = {"projects": scoped_projects} if scoped_projects else {}
    status_field = _checkbox_field(
        "Status", "status", field_counts(jira_dir, "status", **project_scope), query["status"], query
    )
    component_field = _checkbox_field(
        "Component",
        "component",
        field_counts(jira_dir, "component", empty_bucket="_unassigned", **project_scope),
        query["component"],
        query,
    )
    # fixVersion can't be a raw GROUP BY on the stored column the way the
    # fields above are: it must stay re-resolved against the *current*
    # versions cache (a version can be renamed after this item's last
    # reindex without the issue itself changing -- see
    # resolve_fix_version_names), so a stale stored name is never trusted
    # for counting. Fetches the (project-scoped, so still small) enriched
    # item list instead and counts off that, same computation as before
    # this function moved everything else to SQL.
    fix_version_component_field = config.effective_component_field(config.default_project_key())
    fix_version_items = filtered_manifest_items(
        jira_dir, fix_version_component_field, field_filters={"project": scoped_projects} if scoped_projects else None
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
        for name, count in distinct_field_values(fix_version_items, "fixVersion")
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
        "Assignee", "assignee", field_counts(jira_dir, "assignee", **project_scope), query["assignee"], query
    )
    pattern = html.escape(query.get("pattern") or "")
    active_checked = "checked" if query.get("active", "1") == "1" else ""
    sort_value = html.escape(query.get("sort") or "")
    dir_value = html.escape(query.get("dir") or "desc")
    return f"""
    <form method="get" id="{ITEMS_FILTER_FORM_ID}" class="filters">
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
      <a href="{_clear_all_url(query)}" class="button-link" title="Remove every filter below (search, project, status, component, fix version, assignee, active-only, board, scope, swimlane)">Clear all filters</a>
      <a href="/" class="button-link" title="Discard the selections above and reload using the saved default filter">Reset to saved default filter</a>
    </form>
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
    current_dir = query.get("dir", "desc")
    # Descending first, matching the list's own default order (latest
    # first) -- clicking an unsorted column shouldn't feel like it jumped
    # to the "wrong end" of that default. Only the second click on the
    # *same* already-descending column flips to ascending.
    next_dir = "asc" if current_sort == field and current_dir == "desc" else "desc"
    # doseq=True so a list-valued field (project/status/component/...)
    # round-trips as repeated params (?status=A&status=B), not one
    # comma/str-joined value -- query's list fields come straight from
    # FastAPI's own Query(...)/Form(...) list parsing.
    params = {key: value for key, value in query.items() if key not in ("sort", "dir") and value}
    params["sort"] = field
    params["dir"] = next_dir
    indicator = (" ▼" if current_dir == "desc" else " ▲") if current_sort == field else ""
    return f'<th><a href="/?{urlencode(params, doseq=True)}">{html.escape(label)}{indicator}</a></th>'


def render_items_page(
    items: list[dict[str, Any]],
    total: int,
    query: dict[str, Any],
    boards: list[str],
    allowed_version_names: set[str],
    jira_dir: Path,
    config: WorkbenchConfig,
    *,
    flash: str | None = None,
    flash_kind: str | None = None,
    flash_href: str | None = None,
) -> str:
    # query["sort"] already holds the resolved item-dict field name (see
    # _sort_header, which is the only place that sets it) -- not the
    # display label, so this looks it up by value, never by key. Falls
    # back to "key" (not left unsorted) when no column has been explicitly
    # clicked, so the default view shows the most recently created issues
    # first (Jira keys increment over time) instead of arbitrary manifest
    # order -- combined with "dir" defaulting to "desc" everywhere else on
    # this page, that's latest-first out of the box.
    sort_field = query.get("sort") or "key"
    board_value = query.get("board") or ""
    # The synthetic "Modified Items" board (see MODIFIED_BOARD_NAME) has no
    # real Active/Backlog split and no meaningful grouping axis -- always a
    # flat list, regardless of whatever swimlane happens to still be
    # selected from a previous board.
    swimlane_mode = "none" if board_value == MODIFIED_BOARD_NAME else normalize_swimlane(query.get("swimlane"))
    items = sort_items_for_swimlane(items, swimlane_mode, sort_field=sort_field, reverse=query.get("dir") != "asc")

    # Every per-field option list (and read-only status, and status'
    # done-category classification) is per-project, not per-row -- fetched
    # once per distinct project actually *rendered* (not the full
    # unfiltered dataset -- see project_scoped_items_for below), and cached
    # for any repeat project across the visible rows.
    status_categories = observed_status_category_map(jira_dir)
    # Resolution names aren't project-scoped (Jira's Resolution field is
    # instance-wide, not per-project workflow config, unlike status/
    # priority) and aren't part of the manifest-item enrichment
    # _project_scoped_options relies on -- computed once here (not once per
    # row) straight from the observed_field_options local-scan the same
    # way status_categories above already is.
    # A leading "" entry (rendered as "(none)" by _pill_select_options_template_html's
    # own empty-label fallback) lets a status transition to Done submit with
    # no resolution, or clear one already set -- matching the old native
    # <select>'s explicit `<option value="">(none)</option>` (still present
    # for the Detail page's own _status_select_widget_html).
    resolution_options = ["", *observed_field_options(jira_dir, "resolution")]
    options_cache: dict[tuple[str, str], list[str]] = {}
    version_groups_cache: dict[str, list[tuple[str, list[str]]]] = {}
    done_statuses_cache: dict[str, list[str]] = {}
    read_only_cache: dict[str, bool] = {}
    component_field_cache: dict[str, str | None] = {}
    project_items_cache: dict[str, list[dict[str, Any]]] = {}

    def component_field_for(project: str) -> str | None:
        if project not in component_field_cache:
            component_field_cache[project] = config.effective_component_field(project)
        return component_field_cache[project]

    # Every pill-select's option list (Type/Priority/Status/Component/
    # Assignee/Fix Version -- all six editable fields now share this one
    # widget, see _pill_select_row_trigger_html) is identical for every row
    # sharing the same field+project -- registered into a shared <template>
    # (see _pill_select_options_template_html) the first time a given key
    # is needed, emitted once at the bottom of the page, and cloned into
    # each row's own menu lazily on first open (jiraWbPillPopulate) instead
    # of every row shipping its own full copy regardless of whether it's
    # ever opened. This is the fix for the Items list's actual page-weight
    # problem: on a real dataset these fields' repeated option markup was
    # measured at the large majority of each row's own HTML.
    pill_templates_by_key: dict[str, str] = {}
    pill_templates_html: list[str] = []

    def ensure_pill_template(
        key: str,
        options: list[str],
        icon_map: dict[str, tuple[str, str]] | None = None,
        default_icon: tuple[str, str] | None = None,
        *,
        empty_icon_html: str | None = None,
        icon_renderer: Callable[[str], str] | None = None,
        single: bool = True,
        groups: list[tuple[str, list[str]]] | None = None,
    ) -> str:
        if key not in pill_templates_by_key:
            template_id = f"t{len(pill_templates_by_key)}"
            pill_templates_by_key[key] = template_id
            pill_templates_html.append(
                _pill_select_options_template_html(
                    template_id,
                    options,
                    icon_map=icon_map,
                    default_icon=default_icon,
                    empty_icon_html=empty_icon_html,
                    icon_renderer=icon_renderer,
                    single=single,
                    groups=groups,
                )
            )
        return pill_templates_by_key[key]

    def project_scoped_items_for(project: str) -> list[dict[str, Any]]:
        # A project-scoped SQL fetch (see filtered_manifest_items) instead
        # of grouping the full unfiltered dataset in Python -- offers every
        # option that project's synced issues actually have (same
        # guarantee the old base_items grouping gave), just without ever
        # materializing the other projects' items to get there.
        if project not in project_items_cache:
            project_items_cache[project] = filtered_manifest_items(
                jira_dir, component_field_for(project), field_filters={"project": [project]}
            )
        return project_items_cache[project]

    def type_options_for(project: str) -> list[str]:
        key = ("type", project)
        if key not in options_cache:
            options_cache[key] = _project_scoped_options(project_scoped_items_for(project), "type")
        return options_cache[key]

    def priority_options_for(project: str) -> list[str]:
        key = ("priority", project)
        if key not in options_cache:
            options_cache[key] = _project_scoped_priority_options(project_scoped_items_for(project))
        return options_cache[key]

    def status_options_for(project: str) -> list[str]:
        key = ("status", project)
        if key not in options_cache:
            options_cache[key] = _project_scoped_options(project_scoped_items_for(project), "status")
        return options_cache[key]

    def done_statuses_for(project: str) -> list[str]:
        if project not in done_statuses_cache:
            done_statuses_cache[project] = [
                status for status in status_options_for(project) if status_categories.get(status) == "done"
            ]
        return done_statuses_cache[project]

    def assignee_options_for(project: str) -> list[str]:
        key = ("assignee", project)
        if key not in options_cache:
            options_cache[key] = [
                "(unassigned)",
                *_active_assignee_options(jira_dir, config, project, project_scoped_items_for(project)),
            ]
        return options_cache[key]

    def component_options_for(project: str) -> list[str]:
        key = ("component", project)
        if key not in options_cache:
            options_cache[key] = _active_component_options(
                jira_dir, project, component_field_for(project), project_scoped_items_for(project)
            )
        return options_cache[key]

    def _extended_with_observed(official: list[str], observed: set[str]) -> list[str]:
        # assignee/fixVersion's "official" option source (an explicitly
        # curated roster / the active-unreleased versions list) can miss a
        # specific item's own actual current value -- someone no longer on
        # the active roster, or a version that's since been released --
        # _icon_select_editor_html/_multi_select_editor_html used to patch
        # this in per-row ("if current not in options, prepend it"); the
        # shared template can't do that per-row, so it's done once here
        # instead, unioned across every item in the project (a strict
        # superset of anything any one row could have needed to show).
        known_lower = {value.strip().lower() for value in official}
        extra = sorted(value for value in observed if value.strip().lower() not in known_lower)
        return [*official, *extra]

    def assignee_template_options_for(project: str) -> list[str]:
        key = ("assigneeTemplate", project)
        if key not in options_cache:
            observed = {
                str(scoped_item.get("assignee") or "") or "(unassigned)"
                for scoped_item in project_scoped_items_for(project)
            }
            options_cache[key] = _extended_with_observed(assignee_options_for(project), observed)
        return options_cache[key]

    def fix_version_groups_for(project: str) -> list[tuple[str, list[str]]]:
        # Grouped into "Released"/"Unreleased" sections with headers,
        # matching Jira's own Fix Version dropdown look -- unlike
        # fix_version_options_for (the New Issue/Detail pages' flat list,
        # which hides released versions by default -- assigning new work to
        # an already-shipped version is unusual enough to want a deliberate
        # toggle there), this widget shows both groups straight away since
        # Jira's own picker does too. Archived versions are still always
        # excluded -- archiving is what actually removes a version from
        # Jira's picker for good.
        if project not in version_groups_cache:
            all_versions = [
                version for version in normalize_versions(load_versions(jira_dir, project)) if isinstance(version, dict)
            ]
            versions = [version for version in all_versions if not is_archived_version(version)]
            released_names = sorted({version_name(version).strip() for version in versions if version.get("released")} - {""})
            unreleased_names = sorted(
                {version_name(version).strip() for version in versions if not version.get("released")} - {""}
            )
            # A version this project's own issues actually carry but that's
            # missing from the refreshed versions cache entirely (deleted in
            # Jira after being assigned, or the cache is simply stale) --
            # same "extend with observed" safety net every other field's
            # template already uses, bucketed into Unreleased since there's
            # no released/archived signal available for it here. Excludes
            # anything that's actually a known ARCHIVED version, though --
            # regression: an issue can still carry an archived version as
            # its own current fixVersions value, and this fallback used to
            # reinstate it into the Unreleased group just because it wasn't
            # in the (already archived-filtered) known set, defeating the
            # "archived versions are always excluded" rule above for the
            # one case that rule exists to cover.
            archived_lower = {
                version_name(version).strip().lower() for version in all_versions if is_archived_version(version)
            }
            observed = {
                version
                for scoped_item in project_scoped_items_for(project)
                for version in (scoped_item.get("fixVersions") or [])
            }
            known_lower = {name.strip().lower() for name in (*released_names, *unreleased_names)}
            extra = sorted(
                value
                for value in observed
                if value.strip().lower() not in known_lower and value.strip().lower() not in archived_lower
            )
            groups: list[tuple[str, list[str]]] = []
            if released_names:
                groups.append(("Released", released_names))
            if unreleased_names or extra:
                groups.append(("Unreleased", [*unreleased_names, *extra]))
            version_groups_cache[project] = groups
        return version_groups_cache[project]

    def component_template_options_for(project: str) -> list[str]:
        key = ("componentTemplate", project)
        if key not in options_cache:
            # A project with no dedicated component_field uses Jira's native
            # (genuinely multi-valued) "components" field, comma-joined into
            # one display string per item (e.g. "Cloud, Spark") -- splitting
            # is required here so each individual component ends up as its
            # own observed option, not one unmatchable combined string that
            # would never equal any single template option's value.
            observed: set[str] = set()
            for scoped_item in project_scoped_items_for(project):
                observed.update(comma_parts(str(scoped_item.get("component") or "")))
            observed.discard("_unassigned")
            options_cache[key] = _extended_with_observed(component_options_for(project), observed)
        return options_cache[key]

    def project_is_read_only(project: str) -> bool:
        return read_only_cache.setdefault(project, is_project_read_only(jira_dir, project))

    # The row-widget forms all POST to /items/{key}/fields or /status,
    # whose success/error path defaults to redirecting back to the single-
    # item Detail page (right for the Detail page's own forms) -- an edit
    # made from a *list* row instead needs to land back on this exact list
    # view (same filters/sort/swimlane), so every row form here carries the
    # current URL along as a hidden "return_to" field for the route to
    # redirect back to instead.
    return_to_url = f"/?{urlencode(query, doseq=True)}"
    extra_hidden = f'<input type="hidden" name="return_to" value="{html.escape(return_to_url)}">'

    # Which currently-visible rows have a local shadow (an edit staged but
    # not yet pushed) -- a cheap directory glob (modified_issue_keys),
    # computed once here rather than once per row, both for the per-row
    # badge below and for the "Push N modified" button's visibility/count.
    modified_keys = modified_issue_keys(jira_dir)

    # When a specific board is selected with no explicit scope filter (and
    # no swimlane grouping active), the flat list becomes two collapsible
    # sections -- Active and Backlog -- matching Jira's own backlog view,
    # ordered by that board's real rank order (cached backlogKeys/
    # boardKeys, refreshed by `jira-wb meta refresh --boards`, extended
    # with a "boardKeys" -- the active-side rank order -- alongside the
    # pre-existing "backlogKeys") rather than whatever sort/filter order
    # this page would otherwise use. Deliberately doesn't combine with
    # swimlane grouping -- picking a swimlane already means "I want that
    # grouping instead", not stacked on top of this one.
    board_scope_value = query.get("board_scope") or ""
    sections_mode = (
        bool(board_value) and board_value != MODIFIED_BOARD_NAME and not board_scope_value and swimlane_mode == "none"
    )
    section_order: dict[str, list[str]] = {"active": [], "backlog": []}
    if sections_mode:
        board_entry = next((b for b in load_cached_boards(jira_dir) if str(b.get("name") or "") == board_value), None)
        if board_entry:
            section_order["backlog"] = [k for k in (board_entry.get("backlogKeys") or []) if isinstance(k, str)]
            section_order["active"] = [k for k in (board_entry.get("boardKeys") or []) if isinstance(k, str)]

    def item_scope(item: dict[str, Any]) -> str:
        # "backlog" only if this board's cache actually says so -- an item
        # with no boardStatus entry at all for this board (e.g. never
        # classified, or the board refresh predates it) defaults to
        # "active" instead of silently disappearing from both sections.
        return str(item.get("boardStatus", {}).get(board_value) or "active")

    if sections_mode:

        def rank_index(item: dict[str, Any]) -> int:
            order_list = section_order.get(item_scope(item)) or []
            try:
                return order_list.index(str(item.get("key") or ""))
            except ValueError:
                return len(order_list)

        # Active first, matching Jira's own backlog view layout (board/
        # sprint section above, backlog below). sorted() is stable, so
        # items missing from the cached order (e.g. very recently moved or
        # created, before the next board refresh) keep their existing
        # relative order, appended after ranked ones rather than jumping
        # around unpredictably.
        items = sorted(items, key=lambda item: (0 if item_scope(item) == "active" else 1, rank_index(item)))

    rows = []
    any_editable = False
    lane_seen: object = object()  # sentinel guaranteed to differ from any real lane label
    current_lane = lane_seen
    section_seen: object = object()
    current_section = section_seen
    new_issue_base_params: dict[str, str] = {"return_to": return_to_url}
    selected_projects_for_create = [value for value in query.get("project", []) if value]
    if len(selected_projects_for_create) == 1:
        new_issue_base_params["project"] = selected_projects_for_create[0]
    for item in items:
        if swimlane_mode != "none":
            lane = swimlane_label(item, swimlane_mode) or ""
            if lane != current_lane:
                current_lane = lane
                rows.append(
                    '<tr class="swimlane-header" onclick="jiraWbToggleSwimlane(this)">'
                    f'<td colspan="8">{html.escape(lane)}</td></tr>'
                )
        if sections_mode:
            section = item_scope(item)
            if section != current_section:
                current_section = section
                label = "Active" if section == "active" else "Backlog"
                section_new_issue_href = (
                    f"/items/new?{urlencode({**new_issue_base_params, 'board': board_value, 'target_scope': section})}"
                )
                rows.append(
                    f'<tr class="swimlane-header section-header" data-scope="{section}" '
                    'onclick="jiraWbToggleSection(this)" ondragover="jiraWbDragOver(event)" ondrop="jiraWbDrop(event)">'
                    f'<td colspan="8">{label} '
                    f'<a href="{section_new_issue_href}" class="new-issue-fab-small" '
                    f'title="Create a new Jira issue in {label}" onclick="event.stopPropagation()">+</a>'
                    "</td></tr>"
                )
        key = html.escape(str(item.get("key", "")))
        project = str(item.get("project") or "")
        item_type = str(item.get("type") or "")
        item_priority = str(item.get("priority") or "")
        if project and not project_is_read_only(project):
            any_editable = True
            action = f"/items/{key}/fields"
            type_cell = _pill_select_row_trigger_html(
                action,
                "type",
                [item_type],
                ensure_pill_template(f"type::{project}", type_options_for(project), TYPE_ICONS_SVG, DEFAULT_TYPE_ICON_SVG),
                single=True,
                pill_icon_html=lambda v: _icon_span(v, TYPE_ICONS_SVG, DEFAULT_TYPE_ICON_SVG),
                extra_hidden=extra_hidden,
                compact=True,
            )
            priority_cell = _pill_select_row_trigger_html(
                action,
                "priority",
                [item_priority],
                ensure_pill_template(
                    f"priority::{project}",
                    priority_options_for(project),
                    PRIORITY_ICONS_SVG,
                    DEFAULT_TYPE_ICON_SVG,
                    empty_icon_html=NO_PRIORITY_ICON_HTML,
                ),
                single=True,
                pill_icon_html=lambda v: _icon_span(v, PRIORITY_ICONS_SVG, DEFAULT_TYPE_ICON_SVG),
                extra_hidden=extra_hidden,
                compact=True,
            )
            status_cell = _status_pill_widget_html(
                f"/items/{key}/status",
                ensure_pill_template(f"status::{project}", status_options_for(project)),
                str(item.get("status") or ""),
                done_statuses_for(project),
                ensure_pill_template("resolution", resolution_options),
                extra_hidden=extra_hidden,
            )
            # "_unassigned" is sync.py's real on-disk sentinel for "no
            # component" on the item dict itself, not just a display
            # artifact -- must never leak into an editor as if it were a
            # real component name (matching the filter checkbox's own
            # "(unassigned)" convention elsewhere on this page).
            raw_component = str(item.get("component") or "")
            current_component = "" if raw_component == "_unassigned" else raw_component
            component_field = component_field_for(project)
            component_single = bool(component_field)
            component_current = [current_component] if component_single else comma_parts(current_component)
            component_cell = _pill_select_row_trigger_html(
                action,
                component_field or "components",
                component_current,
                ensure_pill_template(
                    f"component::{project}", component_template_options_for(project), single=component_single
                ),
                single=component_single,
                extra_hidden=extra_hidden,
            )
            assignee_name = str(item.get("assignee") or "") or "(unassigned)"
            assignee_cell = _pill_select_row_trigger_html(
                action,
                "assignee",
                [assignee_name],
                # The shared template can't show *this* row's real avatar
                # photo for every other row's own current assignee too --
                # it always renders initials-only (_avatar_span with no
                # photo_url); the pill below (per-row, always rendered)
                # still shows the real photo for the actual current one.
                ensure_pill_template(
                    f"assignee::{project}",
                    assignee_template_options_for(project),
                    icon_renderer=lambda value: _avatar_span(value, ""),
                ),
                single=True,
                pill_icon_html=lambda v: _avatar_span(v, str(item.get("assigneeAvatarUrl") or "")),
                extra_hidden=extra_hidden,
                compact=True,
            )
            fix_version_single = config.fix_version_single_select_project(project)
            fix_version_current = list(item.get("fixVersions") or [])
            if fix_version_single:
                fix_version_current = fix_version_current[:1]
            fix_version_groups = fix_version_groups_for(project)
            fix_version_cell = _pill_select_row_trigger_html(
                action,
                "fixVersions",
                fix_version_current,
                ensure_pill_template(
                    f"fixVersion::{project}",
                    [name for _, names in fix_version_groups for name in names],
                    single=fix_version_single,
                    groups=fix_version_groups,
                ),
                single=fix_version_single,
                extra_hidden=extra_hidden,
            )
            summary_cell = _text_editor_html(action, "summary", str(item.get("summary") or ""), extra_hidden=extra_hidden)
        else:
            type_cell = _type_icon_cell(item_type)
            priority_cell = _priority_icon_cell(item_priority)
            status_cell = html.escape(str(item.get("status", "")))
            component_cell = html.escape(display_component(item.get("component")))
            assignee_name = str(item.get("assignee") or "")
            assignee_cell = _avatar_span(assignee_name, str(item.get("assigneeAvatarUrl") or ""))
            fix_version_cell = html.escape(str(item.get("fixVersion", "")))
            summary_cell = html.escape(str(item.get("summary", "")))
        row_drag_attrs = (
            f' data-scope="{item_scope(item)}" data-key="{key}" data-project="{html.escape(project)}" '
            'draggable="true" ondragstart="jiraWbDragStart(event)" '
            'ondragover="jiraWbDragOver(event)" ondrop="jiraWbDrop(event)"'
            if sections_mode
            else ""
        )
        modified_badge_html = (
            ' <span class="modified-badge" title="Locally edited, not yet pushed to Jira">●</span>'
            if str(item.get("key") or "") in modified_keys
            else ""
        )
        rows.append(
            f"<tr{row_drag_attrs}>"
            f'<td class="pill-select-cell">{type_cell}</td>'
            f'<td><a href="/items/{key}">{key}</a>{modified_badge_html}</td>'
            f'<td class="pill-select-row-cell">{status_cell}</td>'
            f'<td class="pill-select-row-cell">{component_cell}</td>'
            f'<td class="row-field-cell">{summary_cell}</td>'
            f'<td class="pill-select-cell">{priority_cell}</td>'
            f'<td class="pill-select-cell">{assignee_cell}</td>'
            f'<td class="pill-select-row-cell">{fix_version_cell}</td>'
            "</tr>"
        )
    headers = "".join(
        _sort_header(label, query) for label in ("", "Key", "Status", "Component", "Summary", "Pr", "Assignee", "Fix version")
    )
    # return_to round-trips back through the New Issue form (a hidden
    # field there, same idea as the row-widgets' own return_to above) so
    # a created issue lands back on this exact list view, not the Detail
    # page -- see post_new_issue. In sections_mode, the two per-section "+"
    # buttons (built in the row loop above) already cover creation with an
    # unambiguous target scope, so this generic bottom FAB -- which has no
    # scope to hand off -- is suppressed rather than offering a third,
    # scope-less way to create that would reintroduce the exact ambiguity
    # this feature exists to remove.
    bottom_fab_html = ""
    if not sections_mode:
        new_issue_href = f"/items/new?{urlencode(new_issue_base_params)}"
        bottom_fab_html = f'<p><a href="{new_issue_href}" class="new-issue-fab" title="Create a new Jira issue">+</a></p>'
    pill_select_script = PILL_SELECT_SCRIPT + STATUS_PILL_SCRIPT if any_editable else ""
    swimlane_script = SWIMLANE_TOGGLE_SCRIPT if swimlane_mode != "none" else ""
    board_sections_script = BOARD_SECTIONS_SCRIPT if sections_mode else ""
    flash_banner = ""
    if flash:
        kind = flash_kind or "success"
        message_html = html.escape(flash)
        if flash_href:
            message_html = f'<a href="{html.escape(flash_href)}">{message_html}</a>'
        flash_banner = f'<p class="flash flash-{html.escape(kind)}">{message_html}</p>'
    # Counts every locally modified issue, not just whatever's currently
    # filtered/visible -- "is there anything anywhere that needs pushing"
    # rather than a count tied to today's filter. Links to a review page
    # (every modified issue's own diff, via the same render_diff the
    # Detail page's "Local diff" already uses) rather than pushing
    # directly -- a plain confirm() dialog doesn't actually show you what
    # you're about to push, only that you're about to push *something*.
    push_all_button_html = ""
    if modified_keys:
        review_href = f"/items/push-review?{urlencode({'return_to': return_to_url})}"
        push_all_button_html = (
            f'<a href="{review_href}" class="btn btn-primary" style="text-decoration: none;" '
            f'title="Review every locally modified issue before pushing">'
            f"Review &amp; push {len(modified_keys)} modified</a>"
        )
    body = f"""
        <header>
          <h1>Work items</h1>
          <p>{len(items)} of {total} synced items match the current filter.</p>
          {push_all_button_html}
          {flash_banner}
          {_filter_form(jira_dir, config, query, allowed_version_names)}
        </header>
        <table class="items-table">
          <colgroup>
            <col style="width: 36px;">
            <col style="width: 90px;">
            <col style="width: 130px;">
            <col style="width: 130px;">
            <col>
            <col style="width: 44px;">
            <col style="width: 48px;">
            <col style="width: 170px;">
          </colgroup>
          <thead><tr>{headers}</tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        {bottom_fab_html}
        {"".join(pill_templates_html)}
        {pill_select_script}
        {swimlane_script}
        {board_sections_script}
    """
    return page(
        body,
        title="Jira Workbench",
        sidebar_extra=_board_scope_section(query, boards),
        active_nav="items",
        form_wrapped=False,
    )


def _require_same_origin(request: Request) -> None:
    """No auth exists anywhere in this app -- these write routes have real
    external side effects (writing to Jira), unlike the one pre-existing
    state-changing POST (/save-filter, which only ever touches local
    config.toml), so a minimal same-origin check is worth the little it
    costs. Checked only when a browser actually sends Origin/Referer
    (normal form POSTs do) -- absent both, this lets the request through
    rather than blocking it, since this is defense-in-depth, not a real
    auth boundary, in a tool with no login system at all."""
    host = request.url.hostname
    for header_name in ("origin", "referer"):
        value = request.headers.get(header_name)
        if not value:
            continue
        origin_host = urlparse(value).hostname
        if origin_host and origin_host != host:
            raise HTTPException(status_code=403, detail="cross-origin write request rejected")


_MULTI_VALUE_ONLY_FIELD = "labels"
_SELECT_ONLY_FIELDS = {"fixVersions"}


def _current_multi_values(fields: dict[str, Any], field: str) -> list[str]:
    raw = fields.get(field)
    if isinstance(raw, list):
        return [display_name(item).strip() for item in raw if display_name(item).strip()]
    return []


def _project_scoped_options(project_scoped_items: list[dict[str, Any]], field: str) -> list[str]:
    """Options drawn only from issues in the same project as the one being
    edited -- distinct_field_values (and label_counts, for multi-valued
    fields) is normally used unfiltered for global listings, but offering
    every value that happens to exist on ANY synced project on a single
    issue's edit control is noisy at best (hundreds of another project's
    labels) and misleading at worst (an issue type or component that isn't
    even valid for this project)."""
    return [value for value, _ in distinct_field_values(project_scoped_items, field) if value != "(none)"]


def _active_assignee_options(
    jira_dir: Path, config: WorkbenchConfig, project: str, project_scoped_items: list[dict[str, Any]]
) -> list[str]:
    """Who you can actually ASSIGN an issue to -- deliberately NOT
    _project_scoped_options(items, "assignee"), which reflects whoever
    issues in this project have ever been assigned to, including people
    who've since left: a synced issue's assignee snapshot keeps whatever
    "active": true it had the last time that one issue happened to be
    re-fetched, so it can't be trusted to reflect someone's CURRENT status.
    load_assignees is a separate, explicitly-refreshed cache of Jira's own
    Administrator/Member project-access roster (jira-wb meta refresh
    --assignees) -- the same local-cache-first/explicit-refresh pattern
    boards/versions/components already use. Falls back to the old
    (unreliable) local observation only when that cache has never been
    populated yet, so a never-refreshed project still offers something
    rather than an empty dropdown.

    config.excluded_assignees is applied on top either way -- confirmed in
    practice that Jira's own project-access roster can itself be stale
    (someone who's stopped working on a project but was never actually
    removed from its Administrator/Member role), so no purely API-derived
    source is fully trustworthy; this is the manual, user-maintained
    override for exactly that gap.

    The FILTER checklist (_filter_form, elsewhere on this page)
    intentionally keeps using unrestricted local observation instead --
    filtering by an ex-member's old issues should still work."""
    cached = load_assignees(jira_dir, project)
    if cached is not None:
        names = {
            str(user.get("displayName") or "").strip()
            for user in cached.get("assignees", [])
            if isinstance(user, dict)
        }
    else:
        names = set(_project_scoped_options(project_scoped_items, "assignee"))
    names.discard("")
    excluded = config.excluded_assignees(project)
    return sorted(name for name in names if name.strip().lower() not in excluded)


def _active_component_options(
    jira_dir: Path, project: str, component_field: str | None, project_scoped_items: list[dict[str, Any]]
) -> list[str]:
    """Every real, current value this project's component field actually
    has in Jira -- deliberately NOT _project_scoped_options(items,
    "component"), which only ever lists values that have already been
    used on at least one locally synced issue. A component/option that
    was just added in Jira (jira-wb meta refresh --components, or added
    straight from the Jira UI, e.g. a team-managed project's own Settings
    > Fields page) has no issues yet, so local observation alone can never
    show it -- it would stay invisible in the edit dropdown until someone
    had already assigned it once, a chicken-and-egg gap. Prefers the
    refreshed metadata cache (custom field options, or the native
    components cache) and falls back to local observation only when
    neither cache has ever been populated for this project."""
    if component_field and component_field != "components":
        cache = load_component_field_options(jira_dir, project, component_field)
        options = normalize_components(cache.get("options")) if cache is not None else None
    else:
        cache = load_components(jira_dir, project)
        options = normalize_components(cache.get("components")) if cache is not None else None
    if options is not None:
        names = {component_name(option).strip() for option in options}
    else:
        names = set(_project_scoped_options(project_scoped_items, "component"))
    names.discard("")
    names.discard("_unassigned")
    return sorted(names)


def _project_scoped_priority_options(project_scoped_items: list[dict[str, Any]]) -> list[str]:
    values = _project_scoped_options(project_scoped_items, "priority")
    return sorted(values, key=lambda name: PRIORITY_RANK.get(name.strip().lower(), len(PRIORITY_RANK)))


def _tag_input_editor_html(
    action: str,
    field: str,
    options: list[str],
    current: list[str],
    *,
    form_field_name: str = "value",
    extra_hidden: str = "",
) -> str:
    # A comma-separated text input with a <datalist> for autocomplete,
    # not a checkbox list -- a project's labels/versions/components can
    # run into the dozens or hundreds, and a giant checkbox stack is both
    # visually heavy and awkward to scan on a web page (unlike the TUI,
    # where a filterable full-screen picker has room to work). The
    # <datalist> only matches against the *whole* input value, so it's
    # real help for the first tag and less so once you're several tags in
    # -- a known native-HTML limitation, not a bug -- but it's still zero
    # extra JavaScript and a broad improvement over an unfiltered
    # checkbox list for anything past a handful of options.
    datalist_id = f"{field}-datalist"
    datalist_html = "".join(f'<option value="{html.escape(option)}">' for option in options if option != "(none)")
    current_text = ", ".join(current)
    return f"""
    <form method="post" action="{action}">
      <input type="hidden" name="field" value="{html.escape(field)}">
      {extra_hidden}
      <input type="text" name="{form_field_name}" value="{html.escape(current_text)}" list="{datalist_id}"
        placeholder="Comma-separated" style="width: 100%;"
        onblur="if(this.value!==this.defaultValue)this.form.submit()">
      <datalist id="{datalist_id}">{datalist_html}</datalist>
    </form>
    """


def _multi_select_editor_html(
    action: str,
    field: str,
    options: list[str],
    current: list[str],
    *,
    extra_hidden: str = "",
) -> str:
    """A dropdown of checkboxes for a multi-valued field with a genuinely
    bounded option set (Fix Versions -- confirmed in practice to run to a
    few dozen per project at most, unlike Labels/Components which can run
    into the hundreds and stay on _tag_input_editor_html's free-text-plus-
    datalist instead, see that function's own docstring for why a
    checkbox stack doesn't scale there). Reuses the same .icon-select/
    jiraWbIconToggle open/close plumbing as the single-select icon
    dropdown, but with real checkboxes instead of click-to-select rows --
    each one submits the form immediately on change, matching every other
    edit widget's own auto-submit-on-change behavior, rather than adding
    a separate Apply step found nowhere else on this page. Checking a box
    only removed from the request's own "value" list when *unchecked*
    (standard HTML checkbox semantics), so the /items/{key}/fields route
    needs no changes -- it already accepts Form(value: list[str])."""
    current_lower = {value.strip().lower() for value in current}
    # "(none)" is a real, selectable placeholder in the single-select
    # widgets version_options()/etc. were originally built for -- here,
    # "nothing checked" already means "no fix version", so a literal
    # "(none)" checkbox would be redundant at best (and a contradictory
    # state at worst, if checked alongside a real value).
    display_options = [option for option in options if option != "(none)"]
    known_lower = {option.strip().lower() for option in display_options}
    display_options.extend(value for value in current if value.strip().lower() not in known_lower)
    trigger_label = html.escape(", ".join(current)) if current else "(none)"
    rows = []
    for option in display_options:
        checked = " checked" if option.strip().lower() in current_lower else ""
        escaped = html.escape(option)
        rows.append(
            f'<label class="icon-select-option checkbox-row"><input type="checkbox" name="value" '
            f'value="{escaped}"{checked} onchange="this.form.submit()"> {escaped}</label>'
        )
    return f"""
    <form method="post" action="{action}">
      <input type="hidden" name="field" value="{html.escape(field)}">
      {extra_hidden}
      <div class="icon-select">
        <button type="button" class="icon-select-trigger" onclick="jiraWbIconToggle(this)">
          <span>{trigger_label}</span>
        </button>
        <div class="icon-select-menu" role="listbox">{"".join(rows)}</div>
      </div>
    </form>
    """


def _tag_input_bare_html(name: str, options: list[str], current: list[str]) -> str:
    """Same comma-separated text-input-plus-<datalist> as
    _tag_input_editor_html, but with no <form>/auto-submit of its own --
    for the New Issue page, whose fields all live in one shared <form>
    submitted by a single Create button rather than saved field-by-field."""
    datalist_id = f"{name}-datalist"
    datalist_html = "".join(f'<option value="{html.escape(option)}">' for option in options if option != "(none)")
    current_text = ", ".join(current)
    return f"""
    <input type="text" name="{name}" value="{html.escape(current_text)}" list="{datalist_id}"
      placeholder="Comma-separated" style="width: 100%;">
    <datalist id="{datalist_id}">{datalist_html}</datalist>
    """


def _select_editor_html(
    action: str, field: str, options: list[str], current: str, *, extra_hidden: str = ""
) -> str:
    display_options = list(options)
    if current not in display_options:
        display_options = [current, *display_options]
    options_html = "".join(
        f'<option value="{html.escape(option)}"{" selected" if option == current else ""}>{html.escape(option) or "(none)"}</option>'
        for option in display_options
    )
    return f"""
    <form method="post" action="{action}">
      <input type="hidden" name="field" value="{html.escape(field)}">
      {extra_hidden}
      <select name="value" onchange="this.form.submit()">{options_html}</select>
    </form>
    """


def _icon_span(name: str, icon_map: dict[str, tuple[str, str]], default_icon: tuple[str, str]) -> str:
    svg, color = icon_map.get(name.strip().lower(), default_icon) if name else default_icon
    return f'<span style="color: {color}">{svg}</span>'


# Material Design's own "person" glyph -- matching this app's existing
# SVG-icon style (TYPE_ICONS_SVG/PRIORITY_ICONS_SVG) rather than an emoji,
# for the same "consistent icons, not emoji" reason those already are.
_UNASSIGNED_AVATAR_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>'


def _avatar_span(name: str, photo_url: str = "") -> str:
    """A small circle representing an assignee -- their real Jira avatar
    photo when a URL is known, otherwise initials on a per-user
    deterministic color (pill_color, the same hashing already used for
    label pills), or a plain grey circle with a generic person glyph for
    unassigned -- matching Jira's own avatar convention instead of this
    app's previous plain "(unassigned)"/name text with no icon at all."""
    if not name or name == "(unassigned)":
        return f'<span class="avatar-circle avatar-unassigned" title="Unassigned">{_UNASSIGNED_AVATAR_SVG}</span>'
    if photo_url:
        return f'<img class="avatar-circle" src="{html.escape(photo_url)}" alt="{html.escape(name)}" title="{html.escape(name)}">'
    initials = "".join(part[0] for part in name.split() if part)[:2].upper() or "?"
    return f'<span class="avatar-circle" style="background: {pill_color(name)}" title="{html.escape(name)}">{html.escape(initials)}</span>'


def _assignee_icon_renderer(current_name: str, current_photo_url: str) -> Callable[[str], str]:
    # icon_renderer's signature only takes the value being rendered, but a
    # real photo URL is only ever cheaply known (from the manifest/raw
    # issue) for the CURRENT assignee -- every other option row in the open
    # dropdown is just a name with no known Jira user object, so those
    # always fall back to initials.
    def render(value: str) -> str:
        return _avatar_span(value, current_photo_url if value == current_name else "")

    return render


# Shared by every page that renders at least one _icon_select_editor_html
# widget (Detail page, New Issue page -- the Items list moved to the
# unified PILL_SELECT_SCRIPT instead) -- kept as one constant rather than
# duplicated inline per page, and only actually emitted (see each page's
# own gating) when a widget that needs it is actually on screen.
ICON_SELECT_SCRIPT = """
<script>
function jiraWbIconToggle(btn) {
  var menu = btn.nextElementSibling;
  var wasOpen = menu.classList.contains('open');
  document.querySelectorAll('.icon-select-menu.open').forEach(function (m) { m.classList.remove('open'); });
  if (!wasOpen) menu.classList.add('open');
}
function jiraWbIconSelect(optionEl) {
  var form = optionEl.closest('form');
  form.querySelector('input[name="value"]').value = optionEl.getAttribute('data-value');
  form.submit();
}
function jiraWbIconSelectBare(optionEl) {
  var container = optionEl.closest('.icon-select');
  var hidden = container.querySelector('input[type="hidden"]');
  hidden.value = optionEl.getAttribute('data-value');
  container.querySelector('.icon-select-trigger').innerHTML = optionEl.innerHTML;
  container.querySelector('.icon-select-menu').classList.remove('open');
  if (container.dataset.autoSubmit === '1') hidden.closest('form').submit();
}
function jiraWbValidateRequired(form) {
  var missing = [];
  form.querySelectorAll('.icon-select input[type="hidden"][required]').forEach(function (input) {
    if (!input.value) missing.push(input.name);
  });
  if (missing.length) {
    alert('Please choose a value for: ' + missing.join(', '));
    return false;
  }
  return true;
}
document.addEventListener('click', function (e) {
  if (!e.target.closest('.icon-select')) {
    document.querySelectorAll('.icon-select-menu.open').forEach(function (m) { m.classList.remove('open'); });
  }
});
</script>
"""

# The Items list's one unified dropdown -- Type/Priority/Status/Component/
# Assignee/Fix Version all render through _pill_select_row_trigger_html now
# instead of four separate widget implementations (a native <select>, a
# custom icon-select listbox, a checkbox-list variant of that, and a text+
# datalist input). Every interactive change still auto-submits and reloads
# the whole page, exactly like every other widget in this app (see
# _text_editor_html's own docstring for why that's the deliberate baseline
# here) -- there's no client-side optimistic pill update to keep in sync,
# which is what keeps this script small despite covering both single- and
# multi-select in one set of functions.
PILL_SELECT_SCRIPT = """
<script>
function jiraWbPillCurrent(container) {
  try { return JSON.parse(container.getAttribute('data-current') || '[]'); } catch (e) { return []; }
}
function jiraWbPillPopulate(container) {
  // Lazily clones the shared <template> (see ensure_icon_template/
  // _pill_select_options_template_html) into this row's own menu, once --
  // this is the actual fix for the Items list's page-weight problem: the
  // option list (icons, avatars, every fix version) is never repeated per
  // row in the HTML the server sends, only cloned into the rows a user
  // actually opens.
  var menu = container.querySelector('.pill-select-menu');
  if (menu.dataset.populated) return menu;
  var key = container.dataset.optionsKey;
  var tpl = key && document.getElementById('tpl-' + key);
  var target = menu.querySelector('.pill-select-options');
  if (tpl) target.appendChild(tpl.content.cloneNode(true));
  menu.dataset.populated = '1';
  // Marks every currently-selected option -- one, for single-select, any
  // number for multi -- with .selected (a checkmark, see CSS) instead of a
  // native checkbox's checked state.
  var current = {};
  jiraWbPillCurrent(container).forEach(function (v) { current[v] = true; });
  target.querySelectorAll('.pill-select-option').forEach(function (el) {
    el.classList.toggle('selected', !!current[el.getAttribute('data-value')]);
  });
  return menu;
}
function jiraWbPillToggle(trigger) {
  var container = trigger.closest('.pill-select');
  var menu = jiraWbPillPopulate(container);
  var wasOpen = menu.classList.contains('open');
  document.querySelectorAll('.pill-select-menu.open').forEach(function (m) {
    if (m !== menu) m.classList.remove('open');
  });
  menu.classList.toggle('open', !wasOpen);
  if (!wasOpen) {
    var filterInput = menu.querySelector('.pill-select-filter');
    if (filterInput) {
      filterInput.value = '';
      jiraWbPillApplyFilter(menu, '');
      filterInput.focus();
    }
  }
}
function jiraWbPillFilter(input) {
  jiraWbPillApplyFilter(input.closest('.pill-select-menu'), input.value.trim().toLowerCase());
}
function jiraWbPillApplyFilter(menu, needle) {
  menu.querySelectorAll('.pill-select-option').forEach(function (opt) {
    var label = opt.getAttribute('data-label') || '';
    opt.style.display = (!needle || label.indexOf(needle) !== -1) ? '' : 'none';
  });
  // Fix Version's Released/Unreleased group headers (see
  // fix_version_groups_for) hide too once every option under them is
  // filtered out, so a search doesn't leave an empty-looking header behind.
  menu.querySelectorAll('.pill-select-group-header').forEach(function (header) {
    var sibling = header.nextElementSibling;
    var anyVisible = false;
    while (sibling && !sibling.classList.contains('pill-select-group-header')) {
      if (sibling.style.display !== 'none') { anyVisible = true; break; }
      sibling = sibling.nextElementSibling;
    }
    header.style.display = anyVisible ? '' : 'none';
  });
}
function jiraWbPillOptionClick(optionEl) {
  // Single-select: sets the one value and submits immediately.
  var container = optionEl.closest('.pill-select');
  var value = optionEl.getAttribute('data-value');
  var hiddenInput = container.closest('form').querySelector('input[name="' + container.dataset.valueField + '"]');
  if (hiddenInput) hiddenInput.value = value;
  container.setAttribute('data-current', JSON.stringify(value ? [value] : []));
  container.querySelector('.pill-select-menu').classList.remove('open');
  var hook = container.dataset.onSelect;
  if (hook && window[hook]) {
    window[hook](container, value);
  } else {
    container.closest('form').submit();
  }
}
function jiraWbPillSetMultiValues(container, values) {
  // Multi-select has no persistent native inputs for its current values
  // (no checkboxes anymore, see _pill_select_options_template_html) --
  // every change rebuilds one hidden input per value straight from the
  // tracked data-current list, right before submitting.
  var form = container.closest('form');
  form.querySelectorAll('input[data-pill-value="1"]').forEach(function (input) { input.remove(); });
  values.forEach(function (value) {
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = container.dataset.valueField;
    input.value = value;
    input.setAttribute('data-pill-value', '1');
    form.appendChild(input);
  });
  container.setAttribute('data-current', JSON.stringify(values));
}
function jiraWbPillMultiToggle(optionEl) {
  var container = optionEl.closest('.pill-select');
  var value = optionEl.getAttribute('data-value');
  var current = jiraWbPillCurrent(container);
  var idx = current.indexOf(value);
  var next = idx === -1 ? current.concat([value]) : current.slice(0, idx).concat(current.slice(idx + 1));
  jiraWbPillSetMultiValues(container, next);
  container.closest('form').submit();
}
function jiraWbPillRemove(removeBtn, value) {
  // A multi-select pill's own "x" -- removeBtn is a <span>, not a real
  // nested <button> (a <button> can never legally contain another one,
  // which every row's own pills otherwise would).
  var container = removeBtn.closest('.pill-select');
  var next = jiraWbPillCurrent(container).filter(function (v) { return v !== value; });
  jiraWbPillSetMultiValues(container, next);
  container.closest('form').submit();
}
document.addEventListener('click', function (e) {
  if (!e.target.closest('.pill-select')) {
    document.querySelectorAll('.pill-select-menu.open').forEach(function (m) { m.classList.remove('open'); });
  }
});
</script>
"""

# The Items list's status pill-select is the one field that needs a real
# on-select hook instead of PILL_SELECT_SCRIPT's default "just submit" --
# a transition into a Done-category status also needs a resolution, so
# rather than always showing a resolution picker nobody needs most of the
# time, this reveals one right at the moment of transition (mirroring
# Jira's own workflow transition screen), matching the previous native-
# <select>-based widget's own behavior exactly, just wired through
# data-on-select instead of an inline onchange.
STATUS_PILL_SCRIPT = """
<script>
function jiraWbStatusSelected(container, value) {
  var form = container.closest('form');
  var done = JSON.parse(container.dataset.done || '[]');
  var picker = form.querySelector('.resolution-picker');
  if (done.indexOf(value) !== -1) {
    picker.style.display = '';
    var resolutionTrigger = picker.querySelector('.pill-select-trigger');
    if (resolutionTrigger) resolutionTrigger.focus();
  } else {
    picker.style.display = 'none';
    var resolutionContainer = picker.querySelector('.pill-select');
    if (resolutionContainer) {
      resolutionContainer.setAttribute('data-current', '[]');
      var resolutionHidden = form.querySelector('input[name="resolution"]');
      if (resolutionHidden) resolutionHidden.value = '';
      var pills = resolutionContainer.querySelector('.pill-select-pills');
      if (pills) pills.innerHTML = '<span class="pill-select-empty">(none)</span>';
    }
    form.submit();
  }
}
</script>
"""

# Pushing to Jira is a real live round trip (potentially several issues,
# one Jira write apiece) inside a plain <form> POST -- the only feedback
# during that wait used to be the browser tab's own loading spinner, easy
# to mistake for the click not having registered at all. jiraWbPending
# swaps the just-clicked submit button for an inline spinner + "Pushing..."
# right away (before the browser navigates away for the POST), confirming
# the click landed; the button stays disabled so a slow request can't be
# double-submitted by an impatient second click.
PUSH_PENDING_SCRIPT = """
<script>
function jiraWbPending(form, label) {
  var btn = form.querySelector('button[type="submit"]');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-spinner"></span> ' + (label || 'Pushing…');
  }
  return true;
}
</script>
"""

# Only emitted on the Items list when swimlane grouping is active (see
# render_items_page's own gating). Each swimlane-header <tr> is a plain
# clickable row, not a native <details> -- <details> can't legally wrap a
# run of sibling <tr> elements inside a <table>, so collapsing is done by
# hiding every row between one header and the next (or the table's end).
SWIMLANE_TOGGLE_SCRIPT = """
<script>
function jiraWbToggleSwimlane(header) {
  header.classList.toggle('collapsed');
  var collapsed = header.classList.contains('collapsed');
  var row = header.nextElementSibling;
  while (row && !row.classList.contains('swimlane-header')) {
    row.style.display = collapsed ? 'none' : '';
    row = row.nextElementSibling;
  }
}
</script>
"""

# Only emitted on the Items list in "sections_mode" (a specific board
# selected, no explicit scope filter, no swimlane grouping -- see
# render_items_page). jiraWbToggleSection reuses the exact same collapse
# idiom as jiraWbToggleSwimlane (a section-header <tr>'s own class doubles
# as both, see the "swimlane-header section-header" row markup) -- the new
# part here is real drag-and-drop: dragging a row and dropping it (either
# on another row, to land directly before/after it, or on a section header,
# to land at that section's very start) both reorders it (Jira's Rank
# field) and, if the drop lands in the other section, moves it between
# Backlog and the board -- exactly like dragging a card in Jira's own
# backlog view. This is a real, deliberate step up in JavaScript (native
# HTML5 drag-and-drop, plus this app's first fetch()-driven write) --
# every other write in this app is a <form> POST + full-page redirect,
# but a full reload on every drag would defeat the point of dragging in
# the first place. Mouse/touch-drag only, no keyboard alternative -- an
# acknowledged accessibility gap in an otherwise keyboard-friendly app,
# accepted for this internal tool rather than blocking on it.
BOARD_SECTIONS_SCRIPT = """
<script>
var jiraWbDragKey = null;
function jiraWbToggleSection(header) {
  header.classList.toggle('collapsed');
  var collapsed = header.classList.contains('collapsed');
  var row = header.nextElementSibling;
  while (row && !row.classList.contains('section-header')) {
    row.style.display = collapsed ? 'none' : '';
    row = row.nextElementSibling;
  }
}
function jiraWbDragStart(event) {
  jiraWbDragKey = event.currentTarget.dataset.key;
  event.dataTransfer.effectAllowed = 'move';
}
function jiraWbDragOver(event) {
  event.preventDefault();
}
function jiraWbDrop(event) {
  event.preventDefault();
  var draggedKey = jiraWbDragKey;
  jiraWbDragKey = null;
  if (!draggedKey) return;
  var targetRow = event.currentTarget;
  var targetScope = targetRow.dataset.scope;
  if (!targetScope) return;
  var beforeKey = '';
  if (targetRow.dataset.key) {
    if (draggedKey === targetRow.dataset.key) return;
    var rect = targetRow.getBoundingClientRect();
    var dropAbove = (event.clientY - rect.top) < rect.height / 2;
    if (dropAbove) {
      beforeKey = targetRow.dataset.key;
    } else {
      var next = targetRow.nextElementSibling;
      beforeKey = (next && next.dataset.key) || '';
    }
  } else {
    // Dropped directly on a section header -- insert as that section's
    // first item.
    var first = targetRow.nextElementSibling;
    beforeKey = (first && first.dataset.key) || '';
  }
  var draggedRow = document.querySelector('tr[data-key="' + draggedKey + '"]');
  var project = (draggedRow && draggedRow.dataset.project) || '';
  var board = document.getElementById('sidebar-board').value;
  fetch('/items/' + draggedKey + '/board-position', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({project: project, board: board, target_scope: targetScope, before_key: beforeKey})
  }).then(function (r) { return r.json(); }).then(function (data) {
    if (!data.ok) alert('Could not move ' + draggedKey + ': ' + (data.error || 'unknown error'));
    window.location.reload();
  }).catch(function () {
    window.location.reload();
  });
}
</script>
"""


def _icon_select_editor_html(
    action: str,
    field: str,
    options: list[str],
    current: str,
    icon_map: dict[str, tuple[str, str]] | None = None,
    default_icon: tuple[str, str] | None = None,
    *,
    empty_icon_html: str | None = None,
    icon_renderer: Callable[[str], str] | None = None,
    show_trigger_label: bool = True,
    extra_hidden: str = "",
) -> str:
    # A native <option> can only ever hold plain text -- no browser will
    # render an <svg>/<span> inside one -- so getting the *exact* same
    # colored icon the Items list table uses to show up for every option
    # while the dropdown is open (not just the current selection) means
    # this can't be a real <select> at all. It's a small custom listbox
    # instead: a hidden input still carries the actual value posted to
    # /items/{key}/fields, so the server side needs no changes, but the
    # visible control is a button + absolutely-positioned option list
    # driven by the shared jiraWbIcon* functions (see render_item_detail_
    # page's <script> block, emitted once per page). This is a real,
    # deliberate step up in JavaScript from the rest of this app (which
    # otherwise sticks to inline onchange/onblur one-liners) -- the
    # trade-off explicitly chosen over either a native <select> (can't
    # show per-option icons) or emoji-in-option text (visually
    # inconsistent with the real icon set). Mouse/touch only for now --
    # unlike a native <select>, this doesn't get arrow-key option
    # navigation for free.
    #
    # icon_renderer lets a caller supply a completely different "icon"
    # source (e.g. a per-user avatar circle for Assignee) instead of the
    # fixed icon_map lookup Type/Priority use -- both still share this one
    # widget/JS rather than duplicating it. show_trigger_label=False drops
    # the text label from the *closed* trigger only (Priority: the icon
    # alone is enough once collapsed) -- every option row while the
    # dropdown is open still shows its own label, so nothing becomes
    # ambiguous when actually choosing.
    display_options = list(options)
    if current not in display_options:
        display_options = [current, *display_options]

    def icon_for(value: str) -> str:
        if icon_renderer is not None:
            return icon_renderer(value)
        # A genuinely unset value (e.g. priority, which some issue types
        # don't even have) is real Jira data, not a rendering bug -- an
        # explicit muted marker (matching _priority_icon_cell's own
        # convention) reads as "none" rather than looking like a generic/
        # broken default icon indistinguishable from a real value.
        if not value and empty_icon_html is not None:
            return empty_icon_html
        return _icon_span(value, icon_map, default_icon)

    def option_row(option: str) -> str:
        label = html.escape(option) or "(none)"
        selected_class = " selected" if option == current else ""
        return (
            f'<div class="icon-select-option{selected_class}" role="option" data-value="{html.escape(option)}" '
            f'onclick="jiraWbIconSelect(this)">{icon_for(option)}<span>{label}</span></div>'
        )

    trigger_label_html = f"<span>{html.escape(current) or '(none)'}</span>" if show_trigger_label else ""
    options_html = "".join(option_row(option) for option in display_options)
    return f"""
    <form method="post" action="{action}">
      <input type="hidden" name="field" value="{html.escape(field)}">
      <input type="hidden" name="value" value="{html.escape(current)}">
      {extra_hidden}
      <div class="icon-select">
        <button type="button" class="icon-select-trigger" onclick="jiraWbIconToggle(this)">
          {icon_for(current)}{trigger_label_html}
        </button>
        <div class="icon-select-menu" role="listbox">{options_html}</div>
      </div>
    </form>
    """


def _pill_html(value: str, icon_html: str = "", *, removable: bool = False, compact: bool = False) -> str:
    """compact=True renders the icon/avatar with no visible text label at
    all -- just a native `title` tooltip carrying it -- for the handful of
    row-trigger fields (Type, Priority, Assignee) narrow enough that a
    label would either get clipped or spill into the next column; every
    other pill (including these same fields' own dropdown *options*, via
    _pill_select_options_template_html, which never goes compact) still
    shows its label normally."""
    escaped = html.escape(value) or "(none)"
    remove_html = ""
    if removable:
        value_json = html.escape(json.dumps(value), quote=True)
        remove_html = (
            f'<span class="pill-remove" role="button" tabindex="0" '
            f'onclick="event.stopPropagation(); jiraWbPillRemove(this, {value_json})">&times;</span>'
        )
    if compact:
        return f'<span class="pill" title="{escaped}">{icon_html}{remove_html}</span>'
    return f'<span class="pill">{icon_html}<span>{escaped}</span>{remove_html}</span>'


def _pill_select_options_template_html(
    template_id: str,
    options: list[str],
    *,
    icon_map: dict[str, tuple[str, str]] | None = None,
    default_icon: tuple[str, str] | None = None,
    empty_icon_html: str | None = None,
    icon_renderer: Callable[[str], str] | None = None,
    single: bool,
    groups: list[tuple[str, list[str]]] | None = None,
) -> str:
    """The Items list's one shared dropdown option list -- rendered once
    per distinct (field, project) pair into an inert <template> (see
    ensure_icon_template), cloned into a row's own menu lazily on first
    open (jiraWbPillPopulate, PILL_SELECT_SCRIPT). Never bakes in a
    selected/checked state -- the same template is cloned into many rows
    with different current values, so that's applied per-row at open time
    instead, from that row's own data-current attribute.

    groups (Fix Version only, see fix_version_groups_for) renders section
    headers -- "Released"/"Unreleased" -- ahead of their own options,
    matching Jira's own Fix Version dropdown look, instead of one flat
    list; `options` is ignored when `groups` is given (still computed by
    the caller as the flattened equivalent, harmlessly unused here)."""

    def icon_for(value: str) -> str:
        if icon_renderer is not None:
            return icon_renderer(value)
        if not value and empty_icon_html is not None:
            return empty_icon_html
        if icon_map is None and default_icon is None:
            return ""
        return _icon_span(value, icon_map or {}, default_icon or ("", "currentColor"))

    def option_row(option: str) -> str:
        label = html.escape(option) or "(none)"
        escaped = html.escape(option)
        label_attr = html.escape(option.strip().lower())
        # No checkbox, either mode -- a checkbox suggested you had to hit
        # that exact target, when the whole row has always been the click
        # target; a plain row (hover highlight + a checkmark once selected,
        # see .pill-select-option.selected) reads as one clickable option
        # instead of a form control. Single-select sets the value
        # (jiraWbPillOptionClick); multi-select toggles membership
        # (jiraWbPillMultiToggle) -- both submit immediately, same
        # auto-submit-on-change convention the checkbox's own onchange used.
        handler = "jiraWbPillOptionClick" if single else "jiraWbPillMultiToggle"
        return (
            f'<div class="pill-select-option" role="option" data-value="{escaped}" data-label="{label_attr}" '
            f'onclick="{handler}(this)">{icon_for(option)}<span>{label}</span></div>'
        )

    if groups is not None:
        options_html = "".join(
            f'<div class="pill-select-group-header">{html.escape(label)}</div>'
            + "".join(option_row(option) for option in group_options)
            for label, group_options in groups
            if group_options
        )
    else:
        options_html = "".join(option_row(option) for option in options)
    return f'<template id="tpl-{html.escape(template_id)}">{options_html}</template>'


def _pill_select_div_html(
    current: list[str],
    template_id: str,
    *,
    single: bool,
    pill_icon_html: Callable[[str], str] | None = None,
    value_field_name: str = "value",
    on_select: str | None = None,
    placeholder: str = "(none)",
    container_attrs: str = "",
    compact: bool = False,
) -> str:
    """The actual `.pill-select` markup, with no enclosing <form> of its
    own -- see _pill_select_row_trigger_html for the normal case (one
    field, one form), and _status_pill_widget_html for the one case that
    needs two of these (status + a conditionally-revealed resolution)
    sharing a single <form> instead of two independent ones."""
    pills_html = (
        "".join(
            _pill_html(value, pill_icon_html(value) if pill_icon_html else "", removable=not single, compact=compact)
            for value in current
            if value
        )
        if any(current)
        else f'<span class="pill-select-empty">{html.escape(placeholder)}</span>'
    )
    hidden_value_input = (
        f'<input type="hidden" name="{html.escape(value_field_name)}" value="{html.escape(current[0] if current else "")}">'
        if single
        else ""
    )
    current_json = html.escape(json.dumps([value for value in current if value]), quote=True)
    on_select_attr = f' data-on-select="{html.escape(on_select)}"' if on_select else ""
    return f"""
      {hidden_value_input}
      <div class="pill-select" data-options-key="{html.escape(template_id)}" data-current="{current_json}"
           data-mode="{"single" if single else "multi"}" data-value-field="{html.escape(value_field_name)}"{on_select_attr}{container_attrs}>
        <div class="pill-select-trigger" role="button" tabindex="0" onclick="jiraWbPillToggle(this)">
          <span class="pill-select-pills">{pills_html}</span>
        </div>
        <div class="pill-select-menu" role="listbox">
          <input type="text" class="pill-select-filter" placeholder="Filter…" oninput="jiraWbPillFilter(this)"
                 onclick="event.stopPropagation()">
          <div class="pill-select-options"></div>
        </div>
      </div>
    """


def _pill_select_row_trigger_html(
    action: str,
    field: str | None,
    current: list[str],
    template_id: str,
    *,
    single: bool,
    pill_icon_html: Callable[[str], str] | None = None,
    value_field_name: str = "value",
    on_select: str | None = None,
    placeholder: str = "(none)",
    extra_hidden: str = "",
    compact: bool = False,
) -> str:
    """The per-row half of the Items list's unified dropdown, once its
    option list has moved into a shared _pill_select_options_template_html
    template -- current value(s) shown as pills (rendered here, using each
    value's own icon -- cheap, bounded by how many values THIS row
    actually has, not the full option list), a form posting the real
    value(s), and an empty menu populated lazily on first open (see
    PILL_SELECT_SCRIPT).

    single=False renders plain clickable option rows in the shared template
    (jiraWbPillMultiToggle rebuilds the posted "value" inputs from scratch
    on every toggle -- see jiraWbPillSetMultiValues, no native checkboxes)
    and each pill gets a removable "x". single=True renders one hidden
    input named value_field_name (not
    always literally "value" -- the status widget reuses this same
    component with name="status"/"resolution" for its own route/on_select
    hook) that a click updates before submitting. field=None skips the
    generic hidden "field" input entirely -- the status/resolution route,
    unlike /items/{key}/fields, has no use for a field/value pair at all.
    compact=True (Type/Priority/Assignee) drops the trigger's own visible
    text label in favor of a title tooltip -- see _pill_html -- these
    columns are narrow enough that a label either clips or spills into the
    next column.
    """
    field_hidden = f'<input type="hidden" name="field" value="{html.escape(field)}">' if field is not None else ""
    div_html = _pill_select_div_html(
        current,
        template_id,
        single=single,
        pill_icon_html=pill_icon_html,
        value_field_name=value_field_name,
        on_select=on_select,
        placeholder=placeholder,
        compact=compact,
    )
    return f"""
    <form method="post" action="{action}">
      {field_hidden}
      {extra_hidden}
      {div_html}
    </form>
    """


def _icon_select_bare_html(
    name: str,
    options: list[str],
    current: str,
    icon_map: dict[str, tuple[str, str]],
    default_icon: tuple[str, str],
    *,
    empty_icon_html: str | None = None,
    auto_submit: bool = False,
    required: bool = False,
) -> str:
    """Same custom listbox as _icon_select_editor_html, but with no <form>
    of its own -- for the New Issue page, where every field already lives
    together in one shared <form> (unlike the Detail/Items-list widgets,
    each of which independently POSTs to its own route), so the hidden
    input is named directly after the real field instead of going through
    a generic field/value pair. auto_submit reloads the whole page on
    selection (Type: picking a different type needs to reveal that type's
    own fields); without it, selecting an option just updates the trigger
    in place (jiraWbIconSelectBare in ICON_SELECT_SCRIPT), since nothing
    else on the page depends on it until the Create button is pressed.
    `required` is set as a plain HTML attribute for jiraWbValidateRequired
    to read -- the browser itself ignores `required` on a hidden input, so
    this is a JS-only marker, not real constraint validation."""
    display_options = list(options)
    if current not in display_options:
        display_options = [current, *display_options]

    def icon_for(value: str) -> str:
        if not value and empty_icon_html is not None:
            return empty_icon_html
        return _icon_span(value, icon_map, default_icon)

    def option_row(option: str) -> str:
        label = html.escape(option) or "(none)"
        selected_class = " selected" if option == current else ""
        return (
            f'<div class="icon-select-option{selected_class}" role="option" data-value="{html.escape(option)}" '
            f'onclick="jiraWbIconSelectBare(this)">{icon_for(option)}<span>{label}</span></div>'
        )

    trigger_label = html.escape(current) or "(none)"
    options_html = "".join(option_row(option) for option in display_options)
    required_attr = " required" if required else ""
    return f"""
    <div class="icon-select" data-auto-submit="{"1" if auto_submit else "0"}">
      <input type="hidden" name="{html.escape(name)}" value="{html.escape(current)}"{required_attr}>
      <button type="button" class="icon-select-trigger" onclick="jiraWbIconToggle(this)">
        {icon_for(current)}<span>{trigger_label}</span>
      </button>
      <div class="icon-select-menu" role="listbox">{options_html}</div>
    </div>
    """


def _text_editor_html(
    action: str, field: str, current: str, *, multiline: bool = False, extra_hidden: str = ""
) -> str:
    # No Save button -- click into the field, edit, click away, it's saved
    # (onblur submits the form), matching Jira's own inline-field-editing
    # behavior rather than a separate confirm step. This is a deliberate,
    # broader use of JavaScript than the rest of this app's pages (which
    # stick to zero or a couple of onchange="submit" selects) -- justified
    # here specifically because every field on this page is a real Jira
    # write the user explicitly asked to behave like Jira's own click-to-
    # edit-and-it's-saved UX, not a filter/display toggle.
    # this.defaultValue is the original (pre-edit) value for both <input>
    # and <textarea> natively -- comparing against it means clicking into a
    # field and back out without changing anything doesn't create a shadow
    # edit (set_field would otherwise mark the issue "changed" for a no-op).
    on_blur = 'onblur="if(this.value!==this.defaultValue)this.form.submit()"'
    if multiline:
        input_html = f'<textarea name="value" rows="6" style="width: 100%;" {on_blur}>{html.escape(current)}</textarea>'
    else:
        input_html = f'<input type="text" name="value" value="{html.escape(current)}" style="width: 100%;" {on_blur}>'
    return f"""
    <form method="post" action="{action}">
      <input type="hidden" name="field" value="{html.escape(field)}">
      {extra_hidden}
      {input_html}
    </form>
    """


def _field_editor_html(
    jira_dir: Path,
    config: WorkbenchConfig,
    key: str,
    field: str,
    project: str | None,
    issue: dict[str, Any],
    component_field: str | None,
    is_component_field_native: bool,
    project_scoped_items: list[dict[str, Any]],
) -> str:
    action = f"/items/{html.escape(key)}/fields"
    fields = as_dict(issue.get("fields"))
    is_multi_value = field == _MULTI_VALUE_ONLY_FIELD or (field == "components" and is_component_field_native)
    if is_multi_value:
        if field == "labels":
            options = [label for label, _ in label_counts(project_scoped_items)]
        else:
            options = [name for name, _ in component_counts(project_scoped_items) if name != "_unassigned"]
        current = _current_multi_values(fields, field)
        return _tag_input_editor_html(action, field, options, current)

    current_value = editable_field_value(issue, field)
    if field == "type":
        options = _project_scoped_options(project_scoped_items, "type")
        if options:
            return _icon_select_editor_html(action, field, options, current_value, TYPE_ICONS_SVG, DEFAULT_TYPE_ICON_SVG)
    elif field == "priority":
        options = _project_scoped_priority_options(project_scoped_items)
        if options:
            return _icon_select_editor_html(
                action,
                field,
                options,
                current_value,
                PRIORITY_ICONS_SVG,
                DEFAULT_TYPE_ICON_SVG,
                empty_icon_html=NO_PRIORITY_ICON_HTML,
            )
    elif field == "assignee":
        assignee_options = (
            _active_assignee_options(jira_dir, config, project, project_scoped_items)
            if project
            else _project_scoped_options(project_scoped_items, "assignee")
        )
        options = ["(unassigned)", *assignee_options]
        assignee_name = current_value or "(unassigned)"
        return _icon_select_editor_html(
            action,
            field,
            options,
            assignee_name,
            icon_renderer=_assignee_icon_renderer(assignee_name, avatar_url(fields.get("assignee"))),
            show_trigger_label=False,
        )
    elif field == component_field:
        # Both the native "components" field (single-select branch only
        # reached when there's no custom field configured for this
        # project) and a configured custom component field share the same
        # flat "component" key on an enriched manifest item.
        options = (
            _active_component_options(jira_dir, project, component_field, project_scoped_items)
            if project
            else _project_scoped_options(project_scoped_items, "component")
        )
        if options:
            return _select_editor_html(action, field, options, current_value)
    elif field == "parent":
        options = selectable_field_options(
            jira_dir,
            field,
            component_field,
            current_issue=issue,
            project=project,
            component=hierarchy_component(issue, component_field),
            version_filters_by_component=config.version_filters_by_component,
        )
        return _parent_field_editor_html(action, options, current_value)
    elif field in _SELECT_ONLY_FIELDS:
        options = selectable_field_options(
            jira_dir,
            field,
            component_field,
            current_issue=issue,
            project=project,
            component=hierarchy_component(issue, component_field),
            version_filters_by_component=config.version_filters_by_component,
        )
        if options:
            if field == "fixVersions":
                # A real (if rare) issue can carry more than one fix
                # version -- current_value above is editable_field_value's
                # own comma-joined string of ALL of them, which never
                # matches a single <option> and would silently collapse
                # a multi-valued issue down to just one on the next save.
                # _current_multi_values keeps them as a real list instead.
                return _multi_select_editor_html(action, field, options, _current_multi_values(fields, field))
            return _select_editor_html(action, field, options, current_value)
    return _text_editor_html(action, field, current_value, multiline=field == "description")


def _status_select_widget_html(
    action: str,
    status_options: list[str],
    current_status: str,
    done_statuses: list[str],
    resolution_options: list[str],
    *,
    extra_hidden: str = "",
) -> str:
    options_html = "".join(
        f'<option value="{html.escape(status)}"{" selected" if status == current_status else ""}>{html.escape(status)}</option>'
        for status in status_options
    )
    done_json = html.escape(json.dumps(done_statuses), quote=True)
    resolution_options_html = "".join(
        f'<option value="{html.escape(resolution)}">{html.escape(resolution)}</option>' for resolution in resolution_options
    )
    # Auto-submits on change like every other field -- but a transition
    # into a Done-category status also needs a resolution, so rather than
    # always showing a resolution select nobody needs most of the time,
    # this reveals one right at the moment of transition (mirroring Jira's
    # own workflow transition screen). Was a native prompt() (free-text,
    # no validation against real resolution names) -- now a real <select>
    # of this instance's own observed resolutions (same local-observation
    # pattern status_options/done_statuses above already use), matching
    # every other dropdown on this page rather than a one-off text box.
    # Picking "(none)" (an explicit, real option -- not just leaving the
    # select untouched) submits with a blank resolution, same as typing
    # nothing into the old prompt() and hitting OK.
    on_change = (
        "var done=JSON.parse(this.dataset.done);"
        "var picker=this.form.querySelector('.resolution-picker');"
        "if(done.indexOf(this.value)!==-1){"
        "picker.style.display='';"
        "picker.querySelector('select').focus();"
        "}else{"
        "picker.style.display='none';"
        "picker.querySelector('select').value='';"
        "this.form.submit();"
        "}"
    )
    return f"""
    <form method="post" action="{action}">
      {extra_hidden}
      <select name="status" data-done="{done_json}" onchange="{on_change}">{options_html}</select>
      <span class="resolution-picker" style="display: none;">
        <select name="resolution" onchange="this.form.submit()">
          <option value="" disabled selected>Resolution…</option>
          <option value="">(none)</option>
          {resolution_options_html}
        </select>
      </span>
    </form>
    """


def _status_pill_widget_html(
    action: str,
    status_template_id: str,
    current_status: str,
    done_statuses: list[str],
    resolution_template_id: str,
    *,
    extra_hidden: str = "",
) -> str:
    """The Items list's unified-pill-select version of
    _status_select_widget_html (still used by the Detail page, untouched)
    -- same behavior, auto-submits on a plain status change but reveals a
    resolution picker first for a transition into a Done-category status
    (see STATUS_PILL_SCRIPT's jiraWbStatusSelected) -- composed from two
    _pill_select_div_html instances sharing one <form> instead of two
    native <select>s, since status and resolution submit together."""
    done_json = html.escape(json.dumps(done_statuses), quote=True)
    status_div = _pill_select_div_html(
        [current_status] if current_status else [],
        status_template_id,
        single=True,
        value_field_name="status",
        on_select="jiraWbStatusSelected",
        container_attrs=f' data-done="{done_json}"',
    )
    resolution_div = _pill_select_div_html(
        [],
        resolution_template_id,
        single=True,
        value_field_name="resolution",
        placeholder="Resolution…",
    )
    return f"""
    <form method="post" action="{action}">
      {extra_hidden}
      {status_div}
      <span class="resolution-picker" style="display: none;">
        {resolution_div}
      </span>
    </form>
    """


def _status_form_html(
    jira_dir: Path,
    key: str,
    issue: dict[str, Any],
    project_scoped_items: list[dict[str, Any]],
    resolution_options: list[str],
) -> str:
    fields = as_dict(issue.get("fields"))
    current_status = display_name(fields.get("status"))
    status_options = _project_scoped_options(project_scoped_items, "status")
    if current_status and current_status not in status_options:
        status_options = [current_status, *status_options]
    categories = observed_status_category_map(jira_dir)
    done_statuses = [status for status in status_options if categories.get(status) == "done"]
    return _status_select_widget_html(
        f"/items/{html.escape(key)}/status", status_options, current_status, done_statuses, resolution_options
    )


def _comment_row_html(key: str, row: dict[str, str]) -> str:
    comment_id = html.escape(row["id"])
    action_base = f"/items/{html.escape(key)}/comments/{comment_id}"
    state = row["state"]
    if state == "pending-delete":
        delete_label, note = "Undo delete", " -- queued for deletion on next push"
    elif state == "local-new":
        delete_label, note = "Discard", " -- not yet pushed"
    elif state == "edited":
        delete_label, note = "Delete", " -- edited locally, not yet pushed"
    else:
        delete_label, note = "Delete", ""
    escaped_body = html.escape(row["body"])
    return f"""
    <div class="comment-row" style="border-bottom: 1px solid #8885; padding: 8px 0;">
      <p><strong>{html.escape(row["author"])}</strong> {html.escape(row["created"])}{note}</p>
      <form method="post" action="{action_base}/edit">
        <textarea name="body" rows="3" style="width: 100%;"
          onblur="if(this.value!==this.defaultValue)this.form.submit()">{escaped_body}</textarea>
      </form>
      <form method="post" action="{action_base}/delete" onsubmit="return confirm('{delete_label} this comment?')">
        <button type="submit">{delete_label}</button>
      </form>
    </div>
    """


# Standard-field display labels for the human-friendly push-review diff
# (_human_friendly_diff_html) -- a custom/component field not in this dict
# falls back to config.effective_component_field's own resolution (see
# that function) or, failing that, whatever load_field_names has cached,
# or the raw field id as a last resort.
_DIFF_FIELD_LABELS = {
    "summary": "Summary",
    "description": "Description",
    "type": "Type",
    "status": "Status",
    "priority": "Priority",
    "assignee": "Assignee",
    "reporter": "Reporter",
    "fixVersions": "Fix versions",
    "components": "Component",
    "labels": "Labels",
    "parent": "Parent",
}


def _diff_display_value(value: Any) -> str:
    """Turns either shape a field's value ever appears in -- Jira's own raw
    JSON (e.g. {"name": "High"}, [{"name": "3.4.0"}, ...]) or a shadow's
    already-encoded edit (view.py's encode_edit_value: plain strings,
    {"name": ...}/{"value": ...}/{"key": ...}, or lists of either) -- into
    one plain display string, the same "before" and "after" can be
    compared side by side without the reader needing to know which shape
    they're looking at."""
    if value is None:
        return "(none)"
    if isinstance(value, dict):
        for name_key in ("name", "value", "key", "displayName"):
            found = value.get(name_key)
            if found:
                return str(found)
        return "(none)"
    if isinstance(value, list):
        parts = [_diff_display_value(item) for item in value]
        parts = [part for part in parts if part and part != "(none)"]
        return ", ".join(parts) if parts else "(none)"
    text = str(value).strip()
    return text or "(none)"


def _diff_field_label(jira_dir: Path, config: WorkbenchConfig, project: str | None, field: str) -> str:
    if field in _DIFF_FIELD_LABELS:
        return _DIFF_FIELD_LABELS[field]
    if project and config.effective_component_field(project) == field:
        return "Component"
    cached_name = load_field_names(jira_dir).get(field)
    return cached_name or field


def _human_friendly_diff_html(jira_dir: Path, config: WorkbenchConfig, key: str) -> str:
    """The push-review page's own diff rendering -- deliberately separate
    from shadow.py's render_diff (still used as-is by the Detail page's
    "Local diff" and the `jira-wb shadow diff` CLI command, where a
    unified-diff-of-pretty-printed-JSON is exactly the right format for a
    terminal). A browser reader isn't served by "---/+++/@@" unified-diff
    syntax wrapped around raw JSON for what's usually a one-word field
    change -- this instead shows one row per changed field with plain
    Before/After display values (reusing _diff_display_value so both
    Jira's raw JSON shapes and a shadow's encoded-edit shapes render the
    same way), plus comments/resolution as plain sentences."""
    shadow = load_shadow(jira_dir, key)
    if shadow is None:
        return "<p>No local changes.</p>"
    issue = read_json(issue_path(jira_dir, key))
    if not isinstance(issue, dict):
        return "<p>Could not read the local issue file.</p>"
    project = issue_project_key(issue)

    sections = []
    fields = shadow.get("fields", {})
    if isinstance(fields, dict) and fields:
        rows = []
        for field in sorted(fields):
            label = html.escape(_diff_field_label(jira_dir, config, project, field))
            before = html.escape(_diff_display_value(field_value(issue, field)))
            after = html.escape(_diff_display_value(fields[field]))
            rows.append(
                f"<tr><th>{label}</th><td class=\"diff-before\">{before}</td>"
                f'<td class="diff-after">{after}</td></tr>'
            )
        sections.append(
            '<table class="diff-table"><thead><tr><th>Field</th><th>Before</th><th>After</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    # list_comments (not shadow["comments"] directly -- that only ever
    # holds this issue's *new* local comments, not edits/deletes against
    # already-synced ones) is what derives the real local-new/edited/
    # pending-delete/synced classification (see its own docstring); only
    # the non-"synced" rows represent something this push will actually
    # change.
    comment_state_labels = {"local-new": "New comment", "edited": "Edited comment", "pending-delete": "Comment queued for deletion"}
    comment_items = [
        f"<li>{html.escape(comment_state_labels[row['state']])}: {html.escape(row['body'])}</li>"
        for row in list_comments(jira_dir, key, shadow)
        if row.get("state") in comment_state_labels
    ]
    if comment_items:
        sections.append(f"<p><strong>Comments:</strong></p><ul>{''.join(comment_items)}</ul>")

    status_change = shadow.get("statusChange")
    if isinstance(status_change, dict):
        resolution = status_change.get("resolution")
        if resolution:
            sections.append(f"<p><strong>Resolution:</strong> {html.escape(str(resolution))}</p>")

    return "".join(sections) or "<p>No local changes.</p>"


def render_item_detail_page(
    jira_dir: Path,
    config: WorkbenchConfig,
    key: str,
    detail: dict[str, Any],
    comment_rows: list[dict[str, str]],
    shadow: dict[str, Any] | None,
    *,
    read_only: bool,
    flash: str | None = None,
    flash_kind: str | None = None,
) -> str:
    issue = detail["issue"]
    fields = as_dict(issue.get("fields"))
    escaped_key = html.escape(key)
    project = issue_project_key(issue)
    component_field = config.effective_component_field(project)
    is_component_field_native = not component_field
    component_editable_key = component_field or "components"

    try:
        project_scoped_items = [item for item in all_items(jira_dir, config) if item.get("project") == project]
    except ViewError:
        project_scoped_items = []

    parent = as_dict(fields.get("parent"))
    parent_key = html.escape(str(parent.get("key", "")))
    parent_link = f'<a href="/items/{parent_key}">{parent_key}</a>' if parent_key else ""

    editable_fields = editable_detail_fields(component_field) if not read_only else set()
    modified_fields = set(as_dict(shadow.get("fields")).keys()) if shadow else set()

    def display_value(field_name: str) -> str:
        value = fields.get(field_name)
        if isinstance(value, dict):
            value = value.get("displayName") or value.get("name") or value.get("value") or value
        if isinstance(value, list):
            value = ", ".join(str(as_dict(v).get("name", v)) for v in value)
        return html.escape(str(value)) if value else ""

    def cell(field_name: str, plain_html: str = "") -> str:
        if field_name in editable_fields:
            return _field_editor_html(
                jira_dir,
                config,
                key,
                field_name,
                project,
                issue,
                component_field,
                is_component_field_native,
                project_scoped_items,
            )
        return plain_html or display_value(field_name)

    def modified_badge(field_key: str | None) -> str:
        # The border-left accent on a modified field's whole container is
        # easy to miss in a dense grid -- this small dot sits right next
        # to the field's own label, so "this differs from Jira" is
        # obvious without having to scan every field for a colored edge.
        if field_key not in modified_fields:
            return ""
        return ' <span class="modified-badge" title="Locally edited, not yet pushed to Jira">●</span>'

    summary_html = cell("summary")
    summary_class = "detail-summary modified" if "summary" in modified_fields else "detail-summary"

    # Editable case renders _icon_select_editor_html (a custom widget --
    # its own icon is built in). Read-only fallback keeps the same
    # icon-beside-text look via _type_icon_cell/_priority_icon_cell (the
    # exact icons the Items list table uses) so both states look
    # consistent with each other and with that table.
    type_field_html = cell(
        "type",
        f'<div style="display:flex;align-items:center;gap:6px;">'
        f'{_type_icon_cell(display_name(fields.get("issuetype")))}{display_value("issuetype")}</div>',
    )
    priority_field_html = cell(
        "priority",
        f'<div style="display:flex;align-items:center;gap:6px;">'
        f'{_priority_icon_cell(display_name(fields.get("priority")))}{display_value("priority")}</div>',
    )
    assignee_field_html = cell(
        "assignee", _avatar_span(display_name(fields.get("assignee")), avatar_url(fields.get("assignee")))
    )

    # Compact grid -- the single-value fields Jira itself shows in a dense
    # "Details" panel rather than one full-width row apiece, which just
    # made the page scroll forever for no reason. Labels gets its own
    # full-width row below since a tag list can run long. field_key is
    # None for read-only-always fields (Reporter) that can never appear in
    # a shadow's own "fields", so they never need the "modified" class.
    grid_fields = [
        ("Type", "type", type_field_html),
        (
            "Status",
            "status",
            _status_form_html(jira_dir, key, issue, project_scoped_items, observed_field_options(jira_dir, "resolution"))
            if "status" in editable_fields
            else display_value("status"),
        ),
        ("Priority", "priority", priority_field_html),
        (
            "Component",
            component_editable_key,
            cell(component_editable_key, html.escape(hierarchy_component(issue, component_field))),
        ),
        ("Assignee", "assignee", assignee_field_html),
        ("Reporter", None, display_value("reporter")),
        ("Fix versions", "fixVersions", cell("fixVersions")),
        ("Parent", "parent", cell("parent", parent_link)),
    ]
    grid_html = "".join(
        f'<div class="detail-field{" modified" if field_key in modified_fields else ""}">'
        f"<label>{html.escape(label)}{modified_badge(field_key)}</label>{value}</div>"
        for label, field_key, value in grid_fields
    )
    labels_class = "detail-field-full modified" if "labels" in modified_fields else "detail-field-full"
    labels_html = f'<div class="{labels_class}"><label>Labels{modified_badge("labels")}</label>{cell("labels")}</div>'

    description_value = display_name(fields.get("description")) if "description" not in editable_fields else ""
    description_html = (
        _field_editor_html(
            jira_dir,
            config,
            key,
            "description",
            project,
            issue,
            component_field,
            is_component_field_native,
            project_scoped_items,
        )
        if "description" in editable_fields
        else f"<pre>{html.escape(description_value)}</pre>"
    )
    description_class = "detail-description modified" if "description" in modified_fields else "detail-description"

    comment_html = "".join(_comment_row_html(key, row) for row in comment_rows) or "<p>No comments.</p>"
    add_comment_form = f"""
    <form method="post" action="/items/{escaped_key}/comments">
      <textarea name="body" rows="3" style="width: 100%;" placeholder="Add a comment"></textarea>
      <button type="submit">Add comment</button>
    </form>
    """

    flash_banner = ""
    if flash:
        kind = flash_kind or "success"
        flash_banner = f'<p class="flash flash-{html.escape(kind)}">{html.escape(flash)}</p>'

    # Revert/Commit only make sense when there's actually a local shadow to
    # act on -- showing them unconditionally invited clicking "Push to
    # Jira" on an issue with nothing to push. "Commit" (not "Push to
    # Jira") to read as a compact, git-like pair with Revert; the fuller
    # description moves into the tooltip instead of the button label.
    revert_push = ""
    if shadow is not None:
        diff_text = html.escape(render_diff(jira_dir, key))
        revert_push = f"""
        <div class="local-changes-bar">
          <details class="local-diff">
            <summary>Local diff</summary>
            <pre>{diff_text}</pre>
          </details>
          <form method="post" action="/items/{escaped_key}/revert"
            onsubmit="return confirm('Discard all local unpushed changes for {escaped_key}?')">
            <button type="submit" class="btn btn-danger" title="Discard all local unpushed changes for {escaped_key}">Revert</button>
          </form>
          <form method="post" action="/items/{escaped_key}/push"
            onsubmit="return confirm('Push local changes for {escaped_key} to Jira now?') && jiraWbPending(this)">
            <button type="submit" class="btn btn-primary" title="Push local changes for {escaped_key} to Jira">Commit</button>
          </form>
        </div>
        """

    read_only_notice = (
        '<p><em>This project is read-only -- field/status edits are disabled here; comments and push are still available.</em></p>'
        if read_only
        else ""
    )

    # Only emitted when at least one icon-select widget is actually on the
    # page (Type/Priority are editable) -- no point shipping JS a
    # read-only view of this page will never call.
    icon_select_script = ICON_SELECT_SCRIPT if ("type" in editable_fields or "priority" in editable_fields) else ""
    push_pending_script = PUSH_PENDING_SCRIPT if shadow is not None else ""

    body = f"""
    <header>
      <p><a href="/">Back to items</a></p>
      <h1>{escaped_key}</h1>
      {flash_banner}
      {read_only_notice}
      {revert_push}
    </header>
    <div class="{summary_class}">{summary_html}{modified_badge("summary")}</div>
    <div class="detail-fields">{grid_html}</div>
    {labels_html}
    <h2>Description{modified_badge("description")}</h2>
    <div class="{description_class}">{description_html}</div>
    <h2>Comments</h2>
    {comment_html}
    {add_comment_form}
    {icon_select_script}
    {push_pending_script}
    """
    return page(body, title=f"{key} - Jira Workbench", active_nav="items", form_wrapped=False)


# --- Issue creation (Phase 5) -- mirrors tui/screens/issue_create.py's
# IssueCreateScreen: Type must be picked before any type-specific field
# is even shown (Jira's own create-screen config decides which fields
# apply per type), Status is fixed to "To Do" and never sent (Jira has no
# create-time status field), Summary/Description/Type are required. Since
# the web GUI has no per-request cache the way the TUI's app-level
# `issue_type_fields_cache` does, issue_createmeta is fetched live on
# every GET/POST to this page -- an acceptable cost, since creating an
# issue already requires a live round trip to Jira regardless.

ISSUE_CREATE_FIELD_JIRA_KEYS = {
    "version": "fixVersions",
    "priority": "priority",
    "labels": "labels",
    "assignee": "assignee",
    "reporter": "reporter",
    "parent": "parent",
}


def _issue_create_visible_fields(create_fields: dict[str, Any], component_field: str) -> list[str]:
    visible = ["component"] if component_field in create_fields else []
    visible.extend(key for key, jira_key in ISSUE_CREATE_FIELD_JIRA_KEYS.items() if jira_key in create_fields)
    return visible


def _issue_create_field_required(create_fields: dict[str, Any], jira_key: str) -> bool:
    # Jira's own createmeta marks a field required per issue type -- e.g.
    # a project can require Priority or a custom "component" field on one
    # issue type and not another. Type/Summary/Description are hardcoded
    # required regardless of this (mirrors the TUI's own IssueCreateScreen,
    # which never even offers a way to leave those blank).
    meta = create_fields.get(jira_key)
    return bool(isinstance(meta, dict) and meta.get("required"))


def _new_issue_select(name: str, options: list[str], current: str, *, required: bool = False) -> str:
    display_options = list(options)
    if current not in display_options:
        display_options = [current, *display_options]
    if required and not current:
        # A required field must not default to a real selected value the
        # user never actually chose -- drop the blank "current" entry just
        # added above and show an explicit unselected placeholder instead,
        # so the browser's own <select required> validation has an actual
        # empty state to catch on submit.
        display_options = [option for option in display_options if option]
    options_html = "".join(
        f'<option value="{html.escape(option)}"{" selected" if option == current else ""}>{html.escape(option)}</option>'
        for option in display_options
    )
    if required and not current:
        options_html = '<option value="" disabled selected>(choose one)</option>' + options_html
    return f'<select name="{name}"{" required" if required else ""}>{options_html}</select>'


def _parent_option_tags(options: list[str], current: str) -> str:
    # parent_options() returns "Type: KEY summary" labels -- shown here
    # with the type's emoji instead of the word (a native <option> can't
    # render arbitrary markup, only plain text, but an emoji character
    # works fine) -- and the select submits the bare key (what
    # build_create_fields/encode_edit_value expect), not the full label,
    # unlike every other field where the option's value and display text
    # are the same string.
    option_html = [f'<option value="(none)"{" selected" if current in ("", "(none)") else ""}>(none)</option>']
    seen_keys = set()
    for option in options:
        if option == "(none)":
            continue
        key = issue_key_from_text(option) or option.split(" ", 1)[0]
        seen_keys.add(key)
        type_part, _, rest = option.partition(":")
        emoji = TYPE_EMOJI.get(type_part.strip().lower(), DEFAULT_TYPE_EMOJI)
        label = f"{emoji} {rest.strip()}".strip() or option
        selected = " selected" if key == current else ""
        option_html.append(f'<option value="{html.escape(key)}"{selected}>{html.escape(label)}</option>')
    # The issue's actual current parent might not be among the selectable
    # candidates (e.g. it was never independently synced as its own
    # top-level issue) -- rather than silently falling back to "(none)"
    # and misrepresenting the real value, show it plainly, same safety net
    # _select_editor_html gives every other single-value field.
    if current and current not in seen_keys and current != "(none)":
        option_html.append(f'<option value="{html.escape(current)}" selected>{html.escape(current)}</option>')
    return "".join(option_html)


def _parent_select(options: list[str], current: str) -> str:
    """Bare <select> (no wrapping <form>) for the New Issue page, where
    every field lives in one shared form submitted by a single button."""
    return f'<select name="parent">{_parent_option_tags(options, current)}</select>'


def _parent_field_editor_html(action: str, options: list[str], current: str) -> str:
    """Detail page version -- its own independent auto-submitting form,
    matching every other single-value field editor on that page."""
    return f"""
    <form method="post" action="{action}">
      <input type="hidden" name="field" value="parent">
      <select name="value" onchange="this.form.submit()">{_parent_option_tags(options, current)}</select>
    </form>
    """


def _strip_sentinel(value: str, sentinel: str) -> str:
    stripped = value.strip()
    return "" if stripped == sentinel else stripped


def _list_url_matches_item(jira_dir: Path, config: WorkbenchConfig, list_url: str, key: str) -> bool:
    """Whether `key` would actually show up if you navigated to `list_url`
    (the Items list URL a New Issue form's return_to points back at) --
    re-applies the exact same field_filters/pattern/active/board/board_scope
    filter_items already uses for a normal page load. Used right after
    creating an issue to decide whether the redirect back to the list needs
    a plain "created" flash or a "created, but you can't see it from here"
    warning -- a filter that excludes the type/project/component you just
    picked is a completely ordinary thing to have active, not a bug, so
    this doesn't try to change the filter, just flags the mismatch."""
    params = parse_qs(urlparse(list_url).query)

    def first(name: str, default: str = "") -> str:
        values = params.get(name)
        return values[0] if values else default

    field_filters = {
        field: values
        for field, values in (
            ("project", params.get("project", [])),
            ("status", params.get("status", [])),
            ("component", params.get("component", [])),
            ("fixVersion", params.get("fixVersion", [])),
            ("assignee", params.get("assignee", [])),
        )
        if values
    }
    try:
        base = all_items(jira_dir, config)
    except ViewError:
        return True
    filtered = filter_items(
        base,
        field_filters=field_filters,
        pattern=first("pattern") or None,
        active=first("active") == "1",
        board=first("board") or None,
        board_scope=first("board_scope") or None,
    )
    return any(item.get("key") == key for item in filtered)


def render_issue_create_unavailable_page(project: str, reason: str) -> str:
    body = f"""
    <header><p><a href="/">Back to items</a></p><h1>New issue in {html.escape(project)}</h1></header>
    <p class="flash flash-error">Cannot create an issue here: {html.escape(reason)}</p>
    """
    return page(body, title="New issue - Jira Workbench", active_nav="items")


def _issue_create_field_label(label: str, *, required: bool) -> str:
    marker = ' <span style="color: #dc2626" title="Required">*</span>' if required else ""
    return f"{html.escape(label)}{marker}"


def render_issue_create_page(
    jira_dir: Path,
    config: WorkbenchConfig,
    project: str,
    type_fields: dict[str, dict[str, Any]],
    component_field: str,
    query: dict[str, Any],
    default_reporter: str,
    *,
    flash: str | None = None,
    flash_kind: str | None = None,
) -> str:
    def value(field_name: str) -> str:
        return str(query.get(field_name) or "")

    selected_type = value("type")
    create_fields = type_fields.get(selected_type, {})
    visible = _issue_create_visible_fields(create_fields, component_field) if selected_type else []

    _project_scoped_items_cache: list[dict[str, Any]] | None = None

    def project_scoped_items() -> list[dict[str, Any]]:
        # Manifest-based (all_items), not observed_field_options' own
        # local_issues() full-directory-of-every-project scan -- an
        # assignee/etc list for "New issue in SAT" must only ever offer
        # SAT's own people, matching the same per-project scoping the
        # Detail page and Items list already apply to their own editors.
        # Cached (called once each for Component/Priority/Assignee/Labels
        # below) so a single page render loads and enriches the manifest
        # only once, not up to four times.
        nonlocal _project_scoped_items_cache
        if _project_scoped_items_cache is None:
            try:
                base = all_items(jira_dir, config)
            except ViewError:
                base = []
            project_lower = project.strip().lower()
            _project_scoped_items_cache = [
                item for item in base if str(item.get("project") or "").strip().lower() == project_lower
            ]
        return _project_scoped_items_cache

    # Type/Priority reuse the same icon-select widget the Detail page and
    # Items list already use (jiraWbIconSelectBare -- the <form>-less
    # variant, since every field here shares one page-level form). Type
    # always auto-submits the whole form on selection to reveal that
    # type's own fields, matching its previous plain-<select> behavior;
    # Priority has no such dependency, so picking one just updates the
    # trigger in place until Create is actually clicked.
    type_field_html = _icon_select_bare_html(
        "type", sorted(type_fields), selected_type, TYPE_ICONS_SVG, DEFAULT_TYPE_ICON_SVG, auto_submit=True, required=True
    )

    grid_fields = [
        ("Type", type_field_html, True),
        ("Status", "To Do <em>(fixed -- new issues always start at the workflow's initial status)</em>", False),
    ]

    if "component" in visible:
        options = _active_component_options(jira_dir, project, component_field, project_scoped_items())
        component_required = _issue_create_field_required(create_fields, component_field)
        current = value("component")
        if not component_required:
            options = ["(none)", *options]
            current = current or "(none)"
        grid_fields.append(("Component", _new_issue_select("component", options, current, required=component_required), component_required))
    if "version" in visible:
        options = version_options(
            jira_dir,
            project=project,
            component=value("component") or None,
            version_filters_by_component=config.version_filters_by_component,
        )
        version_required = _issue_create_field_required(create_fields, "fixVersions")
        current = value("version")
        if not version_required:
            current = current or "(none)"
        grid_fields.append(("Fix version", _new_issue_select("version", options, current, required=version_required), version_required))
    if "priority" in visible:
        options = _project_scoped_priority_options(project_scoped_items())
        priority_required = _issue_create_field_required(create_fields, "priority")
        grid_fields.append(
            (
                "Priority",
                _icon_select_bare_html(
                    "priority",
                    options,
                    value("priority"),
                    PRIORITY_ICONS_SVG,
                    DEFAULT_TYPE_ICON_SVG,
                    empty_icon_html=NO_PRIORITY_ICON_HTML,
                    required=priority_required,
                ),
                priority_required,
            )
        )
    if "assignee" in visible:
        options = ["(unassigned)", *_active_assignee_options(jira_dir, config, project, project_scoped_items())]
        assignee_required = _issue_create_field_required(create_fields, "assignee")
        current = value("assignee")
        if not assignee_required:
            current = current or "(unassigned)"
        grid_fields.append(("Assignee", _new_issue_select("assignee", options, current, required=assignee_required), assignee_required))
    if "reporter" in visible:
        options = observed_field_options(jira_dir, "reporter")
        reporter_options = (
            [default_reporter, *(o for o in options if o != default_reporter)] if default_reporter else options
        )
        reporter_required = _issue_create_field_required(create_fields, "reporter")
        grid_fields.append(
            (
                "Reporter",
                _new_issue_select(
                    "reporter", reporter_options, value("reporter") or default_reporter, required=reporter_required
                ),
                reporter_required,
            )
        )
    if "parent" in visible:
        grid_fields.append(("Parent", _parent_select(parent_options(jira_dir), value("parent") or "(none)"), False))

    grid_html = "".join(
        f'<div class="detail-field"><label>{_issue_create_field_label(label, required=required)}</label>{cell}</div>'
        for label, cell, required in grid_fields
    )

    labels_html = ""
    if "labels" in visible:
        # label_counts (not label_options' own local_issues() scan of
        # every project) is the manifest-based, multi-value-aware
        # counterpart _project_scoped_options can't cover on its own.
        options = [label for label, _ in label_counts(project_scoped_items())]
        labels_html = (
            '<div class="detail-field-full"><label>Labels</label>'
            f'{_tag_input_bare_html("labels", options, comma_parts(value("labels")))}</div>'
        )

    flash_banner = ""
    if flash:
        kind = flash_kind or "success"
        flash_banner = f'<p class="flash flash-{html.escape(kind)}">{html.escape(flash)}</p>'

    epic_field = f'<input type="hidden" name="epic" value="{html.escape(value("epic"))}">' if value("epic") else ""
    # Carries the Items list URL this New Issue form was opened from (see
    # render_items_page's new_issue_href) through every Type-change reload
    # and the final Create submit, so post_new_issue can send the user
    # back to that exact list view instead of always landing on the new
    # issue's own Detail page.
    return_to_field = (
        f'<input type="hidden" name="return_to" value="{html.escape(value("return_to"))}">' if value("return_to") else ""
    )
    # board/target_scope carry the section you clicked "+" from (see
    # render_items_page's per-section new-issue links) through every
    # Type-change reload and the final Create submit -- post_new_issue uses
    # them to move the newly created issue into that exact section right
    # after creating it, so "create from Backlog" actually lands in
    # Backlog instead of wherever Jira's own default happens to put it.
    board_value = value("board")
    target_scope_value = value("target_scope")
    board_scope_fields = ""
    scope_notice = ""
    if board_value and target_scope_value:
        board_scope_fields = (
            f'<input type="hidden" name="board" value="{html.escape(board_value)}">'
            f'<input type="hidden" name="target_scope" value="{html.escape(target_scope_value)}">'
        )
        scope_label = "Active" if target_scope_value == "active" else "Backlog"
        scope_notice = (
            f'<p><em>Will be created in: {html.escape(scope_label)} of {html.escape(board_value)}</em></p>'
        )

    body = f"""
    <header>
      <p><a href="/">Back to items</a></p>
      <h1>New issue in {html.escape(project)}</h1>
      {scope_notice}
      {flash_banner}
    </header>
    <div class="detail-summary">
      <input type="text" name="summary" value="{html.escape(value("summary"))}" placeholder="Summary" required style="width: 100%;">
    </div>
    <div class="detail-fields">{grid_html}</div>
    {labels_html}
    <h2>Description</h2>
    <div class="detail-description">
      <textarea name="description" rows="6" required style="width: 100%;">{html.escape(value("description"))}</textarea>
    </div>
    {epic_field}
    {return_to_field}
    {board_scope_fields}
    <input type="hidden" name="project" value="{html.escape(project)}">
    <button type="submit" formmethod="post" formaction="/items/new" onclick="return jiraWbValidateRequired(this.form)"
      title="Selecting a Type reloads the form to show that type's own fields; this button actually creates the issue">
      Create issue
    </button>
    {ICON_SELECT_SCRIPT}
    """
    return page(body, title="New issue - Jira Workbench", active_nav="items")


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


def _versions_rows(versions: list[dict[str, Any]], project: str) -> str:
    head = "<th>Name</th><th>ID</th><th>Released</th><th>Archived</th><th>Actions</th>"
    body_rows = []
    for version in versions:
        identifier = html.escape(version_identifier(version))
        name = html.escape(str(version.get("name", "")))
        project_field = f'<input type="hidden" name="project" value="{html.escape(project)}">'
        identifier_field = f'<input type="hidden" name="identifier" value="{identifier}">'
        actions = f"""
        <form method="post" action="/meta/versions/rename" style="display:inline-block">
          {project_field}{identifier_field}
          <input type="text" name="new_name" placeholder="New name" size="14">
          <button type="submit">Rename</button>
        </form>
        <form method="post" action="/meta/versions/release" style="display:inline-block">
          {project_field}{identifier_field}
          <button type="submit">Release</button>
        </form>
        <form method="post" action="/meta/versions/archive" style="display:inline-block">
          {project_field}{identifier_field}
          <button type="submit">Archive</button>
        </form>
        <form method="post" action="/meta/versions/delete" style="display:inline-block"
          onsubmit="return confirm('Delete version {name}? This cannot be undone.')">
          {project_field}{identifier_field}
          <button type="submit">Delete</button>
        </form>
        """
        body_rows.append(
            "<tr>"
            f"<td>{name}</td>"
            f"<td>{identifier}</td>"
            f"<td>{html.escape(str(version.get('released', '')))}</td>"
            f"<td>{html.escape(str(version.get('archived', '')))}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _add_version_form(project: str) -> str:
    return f"""
    <form method="post" action="/meta/versions/add" class="filters">
      <input type="hidden" name="project" value="{html.escape(project)}">
      <input type="text" name="name" placeholder="New version name">
      <button type="submit">Add version</button>
    </form>
    """


def _add_component_form(project: str, component_field: str | None) -> str:
    if component_field and component_field != "components":
        return f"""
        <form method="post" action="/meta/components/add-option" class="filters">
          <input type="hidden" name="project" value="{html.escape(project)}">
          <input type="text" name="name" placeholder="New component option name">
          <input type="text" name="context_id" placeholder="Context id (if required)">
          <button type="submit">Add option</button>
        </form>
        """
    return f"""
    <form method="post" action="/meta/components/add" class="filters">
      <input type="hidden" name="project" value="{html.escape(project)}">
      <input type="text" name="name" placeholder="New component name">
      <button type="submit">Add component</button>
    </form>
    """


def _boards_rows_with_actions(boards: list[dict[str, Any]], project: str) -> str:
    head = (
        '<th>Name</th><th>ID</th><th>Type</th><th>Kind</th><th>Active in picker</th>'
        '<th>Supported</th><th>JQL / reason</th><th>Actions</th>'
    )
    body_rows = []
    for board in boards:
        kind = str(board.get("kind") or "jira")
        identifier = str(board.get("name")) if kind == "local" else str(board.get("id") or "")
        active = bool(board.get("active", True))
        reason = board.get("unsupportedReason")
        jql = board.get("jql")
        supported = "-" if kind == "local" else ("No" if reason else "Yes")
        if reason and jql:
            detail = f"{reason} (jql: {jql})"
        elif reason:
            detail = str(reason)
        else:
            detail = str(jql or "")
        toggle_label = "Disable" if active else "Enable"
        toggle_form = f"""
        <form method="post" action="/meta/boards/toggle-active" style="display:inline-block">
          <input type="hidden" name="project" value="{html.escape(project)}">
          <input type="hidden" name="kind" value="{html.escape(kind)}">
          <input type="hidden" name="identifier" value="{html.escape(identifier)}">
          <input type="hidden" name="active" value="{"0" if active else "1"}">
          <button type="submit">{toggle_label}</button>
        </form>
        """
        delete_form = ""
        if kind == "local":
            delete_form = f"""
            <form method="post" action="/meta/boards/delete" style="display:inline-block"
              onsubmit="return confirm('Delete local board {html.escape(identifier)}?')">
              <input type="hidden" name="project" value="{html.escape(project)}">
              <input type="hidden" name="name" value="{html.escape(identifier)}">
              <button type="submit">Delete</button>
            </form>
            """
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(str(board.get('name', '')))}</td>"
            f"<td>{html.escape(str(board.get('id', '')))}</td>"
            f"<td>{html.escape(str(board.get('type', '')))}</td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td>{'Yes' if active else 'No'}</td>"
            f"<td>{supported}</td>"
            f"<td><code>{html.escape(detail)}</code></td>"
            f"<td>{toggle_form}{delete_form}</td>"
            "</tr>"
        )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _add_local_board_form(project: str) -> str:
    return f"""
    <form method="post" action="/meta/boards/add" class="filters">
      <input type="hidden" name="project" value="{html.escape(project)}">
      <input type="text" name="name" placeholder="Board name" required>
      <input type="text" name="board_project" placeholder="Project filter (comma-separated)" value="{html.escape(project)}">
      <input type="text" name="status" placeholder="Status filter (comma-separated)">
      <input type="text" name="component" placeholder="Component filter (comma-separated)">
      <input type="text" name="assignee" placeholder="Assignee filter (comma-separated)">
      <input type="text" name="fix_version" placeholder="Fix version filter (comma-separated)">
      <input type="text" name="pattern" placeholder="Text pattern (regex)">
      <button type="submit">Add local board</button>
    </form>
    """


def _labels_rows(counts: list[tuple[str, int]], project: str) -> str:
    head = "<th>Label</th><th>Count</th><th>Actions</th>"
    body_rows = []
    for label, count in counts:
        escaped_label = html.escape(label)
        project_field = f'<input type="hidden" name="project" value="{html.escape(project)}">'
        label_field = f'<input type="hidden" name="label" value="{escaped_label}">'
        actions = f"""
        <form method="post" action="/meta/labels/rename" style="display:inline-block">
          {project_field}{label_field}
          <input type="text" name="new_name" placeholder="New name" size="14">
          <button type="submit">Rename</button>
        </form>
        <form method="post" action="/meta/labels/delete" style="display:inline-block"
          onsubmit="return confirm('Delete label {escaped_label} from {count} issue(s) and push them now?')">
          {project_field}{label_field}
          <button type="submit">Delete</button>
        </form>
        """
        body_rows.append(f"<tr><td>{escaped_label}</td><td>{count}</td><td>{actions}</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _label_bulk_edit_flash(result: LabelBulkEditResult, action_desc: str) -> tuple[str, str]:
    if not result.affected_keys:
        if result.skipped_read_only_keys:
            return "no issues to update -- all matches are in read-only projects", "error"
        return "no issues currently have that label", "error"
    if result.push_error:
        return (
            f"{action_desc} locally on {len(result.affected_keys)} issue(s), but could not push: {result.push_error}",
            "error",
        )
    push = result.push_result
    summary = (
        f"{action_desc} and pushed {push.pushed} issue(s)"
        if push is not None
        else f"{action_desc} on {len(result.affected_keys)} issue(s)"
    )
    if push is not None and (push.blocked or push.failed):
        summary += f" (blocked={push.blocked} failed={push.failed})"
        return summary, "error"
    return summary, "success"


META_SECTIONS: tuple[tuple[str, str], ...] = (
    ("versions", "Versions"),
    ("components", "Components"),
    ("boards", "Boards"),
    ("labels", "Labels"),
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
    section: str,
    project: str,
    projects: list[str],
    table_html: str,
    q: str | None,
    empty_message: str,
    *,
    write_forms_html: str = "",
    extra_filter_html: str = "",
    flash: str | None = None,
    flash_kind: str | None = None,
) -> str:
    title = dict(META_SECTIONS).get(section, section.title())
    q_value = html.escape(q or "")
    flash_banner = ""
    if flash:
        kind = flash_kind or "success"
        flash_banner = f'<p class="flash flash-{html.escape(kind)}">{html.escape(flash)}</p>'
    body = f"""
    <header>
      <h1>{html.escape(project)} &middot; {html.escape(title)}</h1>
      {flash_banner}
      <form method="get" class="filters">
        <input type="hidden" name="project" value="{html.escape(project)}">
        <input name="q" value="{q_value}" placeholder="Filter by name">
        {extra_filter_html}
        <button type="submit">Filter</button>
      </form>
      {write_forms_html}
    </header>
    {table_html or f"<p>{html.escape(empty_message)}</p>"}
    """
    return page(
        body,
        title=f"{title} - Jira Workbench",
        sidebar_extra=_meta_sidebar(projects, project, section),
        active_nav="meta",
        form_wrapped=False,
    )


def create_app(jira_dir: Path, config: WorkbenchConfig, config_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Jira Workbench API")

    # Same lifetime/intent as the TUI's own app.issue_type_fields_cache
    # (tui/app.py): one issue_createmeta call per project for as long as
    # this process runs, not one per request. The New Issue page's Type
    # field auto-submits the whole form on every change to reveal that
    # type's own fields -- without this, each of those reloads (and the
    # final POST) re-fetched the full create-metadata live from Jira,
    # which is what made picking a Type feel sluggish.
    type_fields_cache: dict[str, dict[str, dict[str, object]]] = {}

    def cached_type_fields(project: str, client: Any) -> dict[str, dict[str, object]]:
        if project not in type_fields_cache:
            type_fields_cache[project] = fetch_issue_type_fields(client, project)
        return type_fields_cache[project]

    # Same idea, for the Agile "Rank" custom field's id -- instance-wide
    # (not per-project), and the board-position route needs it on every
    # drag-drop that reorders anything, so this avoids a live
    # fetch_all_field_names() round trip per drag. "unfetched" (not None)
    # is the not-yet-cached sentinel, since None is itself a valid, real
    # result (no Rank field on this instance).
    _rank_field_id_state: dict[str, str | None] = {}

    def cached_rank_field_id(client: Any) -> str | None:
        if "value" not in _rank_field_id_state:
            _rank_field_id_state["value"] = fetch_rank_field_id(client)
        return _rank_field_id_state["value"]

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
        response: Response,
        project: list[str] | None = Query(None),
        status: list[str] | None = Query(None),
        component: list[str] | None = Query(None),
        fixVersion: list[str] | None = Query(None),  # noqa: N803
        assignee: list[str] | None = Query(None),
        pattern: str | None = None,
        active: str | None = None,
        board: str | None = None,
        board_scope: str | None = None,
        swimlane: str | None = None,
        show_released: str | None = None,
        sort: str | None = None,
        dir: str | None = None,  # noqa: A002 -- matches the query param name
        flash: str | None = None,
        flash_kind: str | None = None,
        flash_href: str | None = None,
    ) -> str:
        view_cookie = _load_view_cookie(request)
        current_board = view_cookie["current_board"]
        per_board = view_cookie["per_board"]

        def _apply_remembered(fields: dict[str, Any], *, keep_board: str | None) -> None:
            nonlocal project, status, component, fixVersion, assignee, pattern
            nonlocal active, board, board_scope, swimlane, show_released, sort, dir
            project = fields["project"]
            status = fields["status"]
            component = fields["component"]
            fixVersion = fields["fixVersion"]
            assignee = fields["assignee"]
            pattern = fields["pattern"]
            active = fields["active"]
            board_scope = fields["board_scope"]
            swimlane = fields["swimlane"]
            show_released = fields["show_released"]
            sort = fields["sort"]
            dir = fields["dir"]
            if keep_board is not None:
                board = keep_board

        # A bare "/" (no query string at all -- a fresh nav click, not a
        # filter-form submission) restores whichever board was last active
        # and that board's own remembered filters, so a plain reload
        # doesn't snap back to the config.toml default the moment you've
        # changed a filter/sort/swimlane. config.toml's saved [view]
        # defaults -- also what every other interface (CLI/TUI) reads --
        # only apply on a genuinely first-ever visit (no cookie at all yet).
        if not request.query_params:
            remembered_qs = per_board.get(current_board or "") if current_board is not None else None
            if remembered_qs is not None:
                _apply_remembered(_fields_from_query_string(remembered_qs), keep_board=current_board)
            else:
                project = list(config.view_project or ())
                status = list(config.view_status or ())
                component = list(config.view_component or ())
                fixVersion = list(config.view_fix_version or ())
                assignee = list(config.view_assignee or ())
                pattern = config.view_filter
                active = "1" if config.view_active is None or config.view_active else "0"
                board = config.view_board
                board_scope = config.view_board_scope
                swimlane = config.view_swimlane
        # Once any filter param is present the request is explicit and
        # wins outright, including an intentionally-cleared (empty) field
        # -- *except* when the board itself just changed (only detected
        # when this request actually names a board, however blank -- a
        # partial/synthetic request that omits "board" entirely has
        # nothing to compare, so it's left alone): the other fields it
        # carries belong to whatever board was previously selected, not
        # this new one, so they're discarded in favor of this board's own
        # last-remembered state (or a clean slate, if this board has never
        # been visited before) rather than bleeding across boards.
        elif board is not None and current_board is not None and board != current_board:
            remembered_qs = per_board.get(board)
            if remembered_qs is not None:
                _apply_remembered(_fields_from_query_string(remembered_qs), keep_board=board)
            else:
                new_board = board
                project = status = component = fixVersion = assignee = []
                pattern = None
                active = "1"
                board_scope = None
                swimlane = None
                show_released = None
                sort = None
                dir = "desc"
                board = new_board

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
        if not (jira_dir / "manifest.json").exists():
            return page("<h1>Jira Workbench</h1><p>No manifest found. Run <code>jira-wb sync</code>.</p>")
        component_field = config.effective_component_field(config.default_project_key())
        from .db import count_items, reindex_items

        if count_items(jira_dir) == 0:
            # index.db exists (bootstrapped) but has never actually been
            # reindexed -- e.g. a fresh index.db created by opening it
            # once, with no `jira-wb sync`/`db reindex` run since. Every
            # SQL-only helper below (filtered_manifest_items, field_counts,
            # count_items itself) trusts the index completely with no
            # per-item file fallback (unlike load_manifest_items), so left
            # unchecked this would silently render "0 of 0" instead of the
            # real, already-synced data manifest.json says exists.
            # Self-heals here instead: one slow request rebuilds it, every
            # request after this one hits the normal fast path.
            reindex_items(jira_dir, component_field)
        # filtered_manifest_items pushes the SQL-native fields (not
        # fixVersion -- see its own docstring) plus board membership down
        # into a WHERE clause as a safe-superset narrowing (db.list_items);
        # filter_items below still runs the exact same, unchanged filtering
        # it always has, just over a much smaller candidate set instead of
        # every synced item.
        sql_field_filters = {
            field: values
            for field, values in field_filters.items()
            if field in {"project", "status", "component", "assignee"}
        }
        if board == MODIFIED_BOARD_NAME:
            # Not a real board -- bypasses matches_board entirely in favor
            # of filter_items' own modified_only/modified_keys, which
            # nothing in this app's web GUI used until now -- and isn't a
            # board list_items' json_each check could match against either.
            narrowed = filtered_manifest_items(jira_dir, component_field, field_filters=sql_field_filters)
            filtered = filter_items(
                narrowed,
                field_filters=field_filters,
                pattern=pattern,
                active=active_only,
                modified_only=True,
                modified_keys=modified_issue_keys(jira_dir),
            )
        else:
            narrowed = filtered_manifest_items(
                jira_dir, component_field, field_filters=sql_field_filters, board=board or None
            )
            filtered = filter_items(
                narrowed,
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
            "swimlane": normalize_swimlane(swimlane),
            "show_released": "1" if show_released_only else "",
            "sort": sort or "",
            "dir": dir or "desc",
        }
        board_key = query["board"]
        per_board[board_key] = urlencode({k: v for k, v in query.items() if k != "board"}, doseq=True)
        cookie_payload = json.dumps({"current_board": board_key, "per_board": per_board})
        response.set_cookie(LAST_VIEW_COOKIE, cookie_payload, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
        boards = [
            MODIFIED_BOARD_NAME,
            *sorted(
                {
                    str(b.get("name"))
                    for b in load_cached_boards(jira_dir)
                    if b.get("name") and b.get("active", True)
                }
            ),
        ]
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
        return render_items_page(
            filtered,
            count_items(jira_dir),
            query,
            boards,
            allowed_version_names,
            jira_dir,
            config,
            flash=flash,
            flash_kind=flash_kind,
            flash_href=flash_href,
        )

    @app.get("/goto")
    def goto_item(goto_key: str = "") -> RedirectResponse:
        # The sidebar's own quick-jump box (every page) -- deliberately a
        # real navigation (redirect to /items/{KEY}), not a filter change,
        # since the whole point is reaching an issue even when it doesn't
        # match whatever's currently filtered/shown.
        normalized = goto_key.strip().upper()
        if not normalized:
            return RedirectResponse(url="/", status_code=303)
        if not ISSUE_KEY_PATTERN.match(normalized):
            query_string = urlencode(
                {"flash": f"'{goto_key.strip()}' doesn't look like a Jira issue key (e.g. PROJ-123)", "flash_kind": "error"}
            )
            return RedirectResponse(url=f"/?{query_string}", status_code=303)
        if item_detail_data(jira_dir, normalized) is None:
            query_string = urlencode({"flash": f"{normalized} is not synced locally", "flash_kind": "error"})
            return RedirectResponse(url=f"/?{query_string}", status_code=303)
        return RedirectResponse(url=f"/items/{normalized}", status_code=303)

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
        swimlane: str = Form(""),
        show_released: str = Form(""),
        sort: str = Form(""),
        dir: str = Form("desc"),  # noqa: A002 -- matches the form field name
    ) -> RedirectResponse:
        if config_path is None:
            raise HTTPException(status_code=400, detail="no config file location known")
        normalized_swimlane = normalize_swimlane(swimlane)
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
                    # "none" is swimlane's own default (same effect as the
                    # key being absent at all), so it's not worth persisting
                    # explicitly -- matches board/board_scope's own None-
                    # clears-the-key convention above.
                    "swimlane": normalized_swimlane if normalized_swimlane != "none" else None,
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
                "swimlane": normalized_swimlane,
                "show_released": show_released,
                "sort": sort,
                "dir": dir,
            },
            doseq=True,
        )
        return RedirectResponse(url=f"/?{query_string}", status_code=303)

    # Registered before "/items/{key}" -- Starlette matches routes in
    # registration order, so "/items/new" would otherwise be swallowed by
    # the "{key}" path parameter (matching key="new") before ever reaching
    # this route.
    @app.get("/items/new", response_class=HTMLResponse)
    def new_issue_page(
        project: str | None = None,
        type: str | None = Query(None),  # noqa: A002
        summary: str | None = None,
        description: str | None = None,
        component: str | None = None,
        version: str | None = None,
        priority: str | None = None,
        labels: list[str] = Query([]),  # noqa: B008
        assignee: str | None = None,
        reporter: str | None = None,
        parent: str | None = None,
        epic: str | None = None,
        return_to: str | None = None,
        board: str | None = None,
        target_scope: str | None = None,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> str:
        resolved_project = project or config.default_project_key()
        if not resolved_project:
            raise HTTPException(status_code=400, detail="no project configured -- pass ?project=KEY")
        if is_project_read_only(jira_dir, resolved_project):
            return render_issue_create_unavailable_page(resolved_project, f"project {resolved_project} is read-only")
        try:
            _, client = api_client_from_config(
                resolved_project, config.jira_url, config.jira_email, config.jira_api_token
            )
            type_fields = cached_type_fields(resolved_project, client)
        except (MetadataError, IssueError) as exc:
            return render_issue_create_unavailable_page(resolved_project, str(exc))
        if not type_fields:
            return render_issue_create_unavailable_page(
                resolved_project, f"no creatable issue types found for project {resolved_project}"
            )
        component_field = config.effective_component_field(resolved_project) or "components"

        query: dict[str, Any] = {
            "project": resolved_project,
            "type": type or "",
            "summary": summary or "",
            "description": description or "",
            "component": component or "",
            "version": version or "",
            "priority": priority or "",
            "labels": ",".join(v for v in labels if v.strip()),
            "assignee": assignee or "",
            "reporter": reporter or "",
            "parent": parent or "",
            "epic": epic or "",
            "return_to": return_to or "",
            "board": board or "",
            "target_scope": target_scope or "",
        }
        # Epic-based defaults (mirrors IssueCreateScreen's epic_item
        # prefill) -- only applied to a fresh, untouched form (no type
        # chosen yet), so reloading after picking a Type never clobbers
        # values the user may have already edited.
        if epic and not type:
            epic_item = next((item for item in all_items(jira_dir, config) if item.get("key") == epic), None)
            if epic_item:
                query["component"] = query["component"] or str(epic_item.get("component") or "")
                query["version"] = query["version"] or str(epic_item.get("fixVersion") or "")
                query["priority"] = query["priority"] or str(epic_item.get("priority") or "")
                query["assignee"] = query["assignee"] or str(epic_item.get("assignee") or "")
                query["parent"] = query["parent"] or epic

        default_reporter = ""
        if any("reporter" in fields for fields in type_fields.values()):
            try:
                current_user = fetch_current_user(client)
                default_reporter = str(current_user.get("displayName") or current_user.get("emailAddress") or "")
            except IssueError:
                default_reporter = ""

        return render_issue_create_page(
            jira_dir,
            config,
            resolved_project,
            type_fields,
            component_field,
            query,
            default_reporter,
            flash=flash,
            flash_kind=flash_kind,
        )

    @app.post("/items/new", dependencies=[Depends(_require_same_origin)])
    def post_new_issue(
        project: str = Form(...),
        type: str = Form(""),  # noqa: A002
        summary: str = Form(""),
        description: str = Form(""),
        component: str = Form(""),
        version: str = Form(""),
        priority: str = Form(""),
        labels: str = Form(""),
        assignee: str = Form(""),
        reporter: str = Form(""),
        parent: str = Form(""),
        epic: str = Form(""),
        return_to: str = Form(""),
        board: str = Form(""),
        target_scope: str = Form(""),
    ) -> RedirectResponse:
        def back_to_form(message: str) -> RedirectResponse:
            params = {
                "project": project,
                "type": type,
                "summary": summary,
                "description": description,
                "component": component,
                "version": version,
                "priority": priority,
                "labels": labels,
                "assignee": assignee,
                "reporter": reporter,
                "parent": parent,
                "epic": epic,
                "return_to": return_to,
                "board": board,
                "target_scope": target_scope,
                "flash": message,
                "flash_kind": "error",
            }
            query_string = urlencode({k: v for k, v in params.items() if v}, doseq=True)
            return RedirectResponse(url=f"/items/new?{query_string}", status_code=303)

        if not type:
            return back_to_form("Issue type is required")
        if not summary.strip():
            return back_to_form("Summary is required")
        if not description.strip():
            return back_to_form("Description is required")
        if is_project_read_only(jira_dir, project):
            return back_to_form(f"project {project} is read-only")

        component_field = config.effective_component_field(project) or "components"
        component_value = _strip_sentinel(component, "(none)")
        version_value = _strip_sentinel(version, "(none)")
        priority_value = _strip_sentinel(priority, "(none)")
        parent_value = _strip_sentinel(parent, "(none)")
        assignee_value = _strip_sentinel(assignee, "(unassigned)")
        label_values = comma_parts(labels)

        try:
            _, client = api_client_from_config(project, config.jira_url, config.jira_email, config.jira_api_token)
            type_fields = cached_type_fields(project, client)
            create_fields = type_fields.get(type, {})
        except (MetadataError, IssueError) as exc:
            return back_to_form(str(exc))

        # Client-side (HTML5 required + jiraWbValidateRequired) already
        # steers the user away from this, but it's just a UX nicety, not a
        # security boundary -- Jira's own createmeta is the authority on
        # what's actually required per issue type, checked here too so a
        # crafted/stale request can't slip a genuinely required field
        # through blank and hit create_issue's much less friendly error.
        for jira_key, submitted_value, label in (
            (component_field, component_value, "Component"),
            ("fixVersions", version_value, "Fix version"),
            ("priority", priority_value, "Priority"),
            ("assignee", assignee_value, "Assignee"),
            ("reporter", reporter.strip(), "Reporter"),
        ):
            if _issue_create_field_required(create_fields, jira_key) and not submitted_value:
                return back_to_form(f"{label} is required")

        try:
            fields = build_create_fields(
                project=project,
                issue_type=type,
                summary=summary.strip(),
                description=description.strip(),
                component_field=component_field,
                component=component_value or None,
                parent=parent_value or None,
                priority=priority_value or None,
                fix_version=version_value or None,
                labels=label_values or None,
                assignee=user_payload(jira_dir, assignee_value) if assignee_value else None,
                create_fields=create_fields,
                reporter=user_payload(jira_dir, reporter) if reporter.strip() else None,
            )
            key = create_issue(client, fields)
        except (MetadataError, IssueError, ShadowError) as exc:
            return back_to_form(str(exc))

        try:
            refresh_local_issue_after_push(jira_dir, key, client, component_field)
        except ShadowError as exc:
            query_string = urlencode({"flash": f"created but local refresh failed: {exc}", "flash_kind": "error"})
            return RedirectResponse(url=f"/items/{key}?{query_string}", status_code=303)

        # A plain issue_create doesn't control which section (Backlog vs
        # the board) a new issue lands in -- Jira decides that on its own,
        # which is exactly the "why did it land on the active board"
        # confusion this feature exists to fix. When the "+" was clicked
        # from a specific section, explicitly move the new issue there
        # right away, then refresh this project's whole board cache (not
        # just this one key) so the Items list reflects it immediately
        # rather than waiting for the next manual `meta refresh --boards`.
        # The issue itself is already created either way -- a failed move
        # here is surfaced as a warning below, not treated as the create
        # itself having failed.
        scope_move_error: str | None = None
        if board and target_scope:
            board_entry = _find_board_entry(jira_dir, project, board)
            if board_entry is None:
                scope_move_error = f"created, but board {board!r} was not found to move it into {target_scope}"
            else:
                board_id = board_entry.get("id")
                try:
                    if target_scope == "backlog":
                        move_issue_to_backlog(client, key)
                    elif board_id is not None:
                        move_issue_to_board(client, board_id, key)
                    refresh_boards_api(jira_dir, project, client, component_field)
                except Exception as exc:  # noqa: BLE001 -- surfaced as a warning, not raised
                    scope_move_error = f"created, but moving it to {target_scope} failed: {exc}"

        # Land back on the list you opened "New issue" from, not the new
        # issue's own Detail page -- but a filter that doesn't happen to
        # match what you just created (wrong project/status/component
        # selected, etc.) would otherwise make it look like creation
        # silently failed, so that case (and a failed scope_move above)
        # gets a clickable warning banner pointing at the issue instead of
        # a plain success message.
        if scope_move_error:
            params = {"flash": f"{key} {scope_move_error}", "flash_kind": "warning", "flash_href": f"/items/{key}"}
        elif return_to and not _list_url_matches_item(jira_dir, config, return_to, key):
            params = {
                "flash": f"{key} created, but it doesn't match your current filters",
                "flash_kind": "warning",
                "flash_href": f"/items/{key}",
            }
        else:
            params = {"flash": f"{key} created", "flash_kind": "success"}

        if return_to:
            separator = "&" if "?" in return_to else "?"
            return RedirectResponse(url=f"{return_to}{separator}{urlencode(params)}", status_code=303)
        return RedirectResponse(url=f"/items/{key}?{urlencode(params)}", status_code=303)

    # Registered before "/items/{key}" -- Starlette matches routes in
    # registration order, so "/items/push-review" would otherwise be
    # swallowed by the "{key}" path parameter (matching key="push-review")
    # before ever reaching this route, same reason "/items/new" is too.
    @app.get("/items/push-review", response_class=HTMLResponse)
    def push_review_page(return_to: str | None = None) -> str:
        # The Items list's "Review & push N modified" link lands here
        # first -- every locally modified issue's own diff, reviewed
        # before the actual push happens on this page's own confirm.
        # _human_friendly_diff_html (not shadow.py's render_diff, which
        # stays as-is for the Detail page's "Local diff" and the CLI) --
        # a unified diff of pretty-printed JSON is right for a terminal,
        # not a browser reader looking at a one-word field change.
        return_to_value = return_to or "/"
        keys = sorted(modified_issue_keys(jira_dir))
        sections = []
        for key in keys:
            # The key alone ("SAT-142") doesn't say what the issue is
            # about -- the summary is what actually lets you recognize it
            # at a glance in a list of several.
            detail = item_detail_data(jira_dir, key)
            summary_text = (
                str(as_dict(detail["issue"].get("fields")).get("summary") or "") if detail is not None else ""
            )
            label = f"{key} — {summary_text}" if summary_text else key
            diff_html = _human_friendly_diff_html(jira_dir, config, key)
            sections.append(f'<details class="local-diff" open><summary>{html.escape(label)}</summary>{diff_html}</details>')
        push_form = ""
        if keys:
            push_form = f"""
            <form method="post" action="/items/push-all"
              onsubmit="return confirm('Push {len(keys)} issue(s) to Jira now?') && jiraWbPending(this, 'Pushing {len(keys)} issue(s)…')">
              <input type="hidden" name="return_to" value="{html.escape(return_to_value)}">
              <button type="submit" class="btn btn-primary">Push {len(keys)} to Jira</button>
            </form>
            """
        body = f"""
        <header>
          <p><a href="{html.escape(return_to_value)}">Back to items</a></p>
          <h1>Review changes before pushing</h1>
          <p>{len(keys)} locally modified issue(s) will be pushed to Jira.</p>
        </header>
        {"".join(sections) or "<p>Nothing to push.</p>"}
        {push_form}
        {PUSH_PENDING_SCRIPT if keys else ""}
        """
        return page(body, title="Review changes - Jira Workbench", active_nav="items", form_wrapped=False)

    @app.get("/items/{key}", response_class=HTMLResponse)
    def item_page(key: str, flash: str | None = None, flash_kind: str | None = None) -> str:
        detail = item_detail_data(jira_dir, key)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"work item {key} is not synced locally")
        project = issue_project_key(detail["issue"])
        read_only = is_project_read_only(jira_dir, project)
        shadow = load_shadow(jira_dir, key)
        comment_rows = list_comments(jira_dir, key, shadow)
        return render_item_detail_page(
            jira_dir,
            config,
            key,
            detail,
            comment_rows,
            shadow,
            read_only=read_only,
            flash=flash,
            flash_kind=flash_kind,
        )

    def _redirect_after_edit(key: str, *, flash: str, flash_kind: str = "success", return_to: str = "") -> RedirectResponse:
        # return_to carries the Items *list* page's full current URL
        # (filters/sort/swimlane and all) when a field/status edit came
        # from an inline row widget there -- without it, every edit route
        # below defaults to the single-item Detail page, which is correct
        # when the edit came from Detail itself but wrongly yanks the user
        # off the list into "view mode" after a list-row edit.
        query_string = urlencode({"flash": flash, "flash_kind": flash_kind})
        target = return_to or f"/items/{key}"
        separator = "&" if "?" in target else "?"
        return RedirectResponse(url=f"{target}{separator}{query_string}", status_code=303)

    def _redirect_to_item(key: str, *, flash: str, flash_kind: str = "success") -> RedirectResponse:
        return _redirect_after_edit(key, flash=flash, flash_kind=flash_kind)

    def _require_synced(key: str) -> dict[str, Any]:
        detail = item_detail_data(jira_dir, key)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"work item {key} is not synced locally")
        return detail

    @app.post("/items/{key}/fields", dependencies=[Depends(_require_same_origin)])
    def post_item_field(
        key: str, field: str = Form(...), value: list[str] = Form([]), return_to: str = Form("")  # noqa: B008
    ) -> RedirectResponse:
        detail = _require_synced(key)
        project = issue_project_key(detail["issue"])
        component_field = config.effective_component_field(project)
        raw_value = ",".join(v for v in value if v.strip())
        encoded = encode_edit_value(field, raw_value, component_field=component_field)
        try:
            set_field(jira_dir, key, field, encoded)
        except ShadowError as exc:
            return _redirect_after_edit(key, flash=str(exc), flash_kind="error", return_to=return_to)
        return _redirect_after_edit(key, flash=f"Saved {field}", return_to=return_to)

    @app.post("/items/{key}/status", dependencies=[Depends(_require_same_origin)])
    def post_item_status(
        key: str, status: str = Form(...), resolution: str = Form(""), return_to: str = Form("")
    ) -> RedirectResponse:
        _require_synced(key)
        categories = observed_status_category_map(jira_dir)
        effective_resolution = resolution.strip() if categories.get(status) == "done" else ""
        try:
            set_field(jira_dir, key, "status", status)
            # Always called (not only when a resolution is given) so moving
            # to a non-done status explicitly clears any resolution left
            # over from a previous done-status change instead of letting it
            # linger in the shadow -- a small deliberate improvement over
            # the TUI's own _edit_status, which only calls this when
            # resolution is truthy.
            set_status_change(jira_dir, key, resolution=effective_resolution or None)
        except ShadowError as exc:
            return _redirect_after_edit(key, flash=str(exc), flash_kind="error", return_to=return_to)
        return _redirect_after_edit(key, flash=f"Status changed to {status}", return_to=return_to)

    @app.post("/items/{key}/board-position", dependencies=[Depends(_require_same_origin)])
    def post_item_board_position(
        key: str,
        project: str = Form(...),
        board: str = Form(...),
        target_scope: str = Form(...),
        before_key: str = Form(""),
    ) -> dict[str, Any]:
        # Returns JSON, not a redirect -- this is the drag-and-drop handler's
        # own fetch() target (see BOARD_SECTIONS_SCRIPT), the first
        # non-form-POST write route in this app. The scoped "New issue" flow
        # (post_new_issue) calls the same underlying metadata.py functions
        # directly rather than this route, since it already has a live
        # client open from creating the issue.
        _require_synced(key)
        board_entry = _find_board_entry(jira_dir, project, board)
        if board_entry is None:
            return {"ok": False, "error": f"board {board!r} not found for project {project}"}
        board_id = board_entry.get("id")
        current_scope = "backlog" if key in (board_entry.get("backlogKeys") or []) else "active"

        try:
            _, client = api_client_from_config(project, config.jira_url, config.jira_email, config.jira_api_token)
            if target_scope != current_scope:
                if target_scope == "backlog":
                    move_issue_to_backlog(client, key)
                else:
                    move_issue_to_board(client, board_id, key)
            if before_key:
                rank_field_id = cached_rank_field_id(client)
                if rank_field_id:
                    rank_issue_before(client, key, before_key, rank_field_id)
            _patch_board_order(jira_dir, project, board, key, target_scope=target_scope, before_key=before_key or None)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the caller as JSON, not raised
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    @app.post("/items/{key}/comments", dependencies=[Depends(_require_same_origin)])
    def post_item_comment(key: str, body: str = Form(...)) -> RedirectResponse:
        _require_synced(key)
        if not body.strip():
            return _redirect_to_item(key, flash="Comment body cannot be empty", flash_kind="error")
        try:
            add_comment(jira_dir, key, body.strip())
        except ShadowError as exc:
            return _redirect_to_item(key, flash=str(exc), flash_kind="error")
        return _redirect_to_item(key, flash="Comment added")

    @app.post("/items/{key}/comments/{comment_id}/edit", dependencies=[Depends(_require_same_origin)])
    def post_item_comment_edit(key: str, comment_id: str, body: str = Form(...)) -> RedirectResponse:
        _require_synced(key)
        try:
            edit_comment(jira_dir, key, comment_id, body)
        except ShadowError as exc:
            return _redirect_to_item(key, flash=str(exc), flash_kind="error")
        return _redirect_to_item(key, flash="Comment updated")

    @app.post("/items/{key}/comments/{comment_id}/delete", dependencies=[Depends(_require_same_origin)])
    def post_item_comment_delete(key: str, comment_id: str) -> RedirectResponse:
        _require_synced(key)
        shadow = load_shadow(jira_dir, key)
        rows = {row["id"]: row for row in list_comments(jira_dir, key, shadow)}
        row = rows.get(comment_id)
        state = row["state"] if row else ("local-new" if is_local_comment_id(comment_id) else "synced")
        try:
            if state == "pending-delete":
                undelete_comment(jira_dir, key, comment_id)
                flash = "Delete undone"
            elif state == "local-new":
                remove_local_comment(jira_dir, key, comment_id)
                flash = "Comment discarded"
            else:
                delete_comment(jira_dir, key, comment_id)
                flash = "Comment queued for deletion"
        except ShadowError as exc:
            return _redirect_to_item(key, flash=str(exc), flash_kind="error")
        return _redirect_to_item(key, flash=flash)

    @app.post("/items/{key}/revert", dependencies=[Depends(_require_same_origin)])
    def post_item_revert(key: str) -> RedirectResponse:
        _require_synced(key)
        delete_shadow(jira_dir, key)
        return _redirect_to_item(key, flash="Local changes reverted")

    @app.post("/items/{key}/push", dependencies=[Depends(_require_same_origin)])
    def post_item_push(key: str) -> RedirectResponse:
        detail = _require_synced(key)
        project = issue_project_key(detail["issue"])
        try:
            _, client = api_client_from_config(project, config.jira_url, config.jira_email, config.jira_api_token)
        except MetadataError as exc:
            return _redirect_to_item(key, flash=str(exc), flash_kind="error")
        component_field = config.effective_component_field(project) or "components"
        # push_shadows([key]), not push_key directly -- a strict superset of
        # safety (still touches only this one key when it has no parent
        # dependency on another unpushed shadow) that the TUI's own
        # action_push doesn't bother with today.
        result = push_shadows(jira_dir, [key], client, component_field=component_field)
        if result.failed:
            error = result.errors[0] if result.errors else "push failed"
            return _redirect_to_item(key, flash=error, flash_kind="error")
        if result.blocked:
            return _redirect_to_item(key, flash=f"{key}: push blocked (unmet dependency)", flash_kind="error")
        if result.pushed:
            return _redirect_to_item(key, flash=f"{key}: pushed to Jira")
        return _redirect_to_item(key, flash=f"{key}: nothing to push")

    @app.post("/items/push-all", dependencies=[Depends(_require_same_origin)])
    def post_push_all_modified(return_to: str = Form("")) -> RedirectResponse:
        # The Items list's own "Push N modified" button (see
        # render_items_page) -- every locally modified issue, not just
        # whatever the current filter happens to show, grouped by project
        # since api_client_from_config/push_shadows are project-scoped
        # (one live Jira client per project, unlike a single-item push
        # which already knows its one project).
        target = return_to or "/"
        separator = "&" if "?" in target else "?"

        def redirect(message: str, kind: str) -> RedirectResponse:
            query_string = urlencode({"flash": message, "flash_kind": kind})
            return RedirectResponse(url=f"{target}{separator}{query_string}", status_code=303)

        keys_by_project: dict[str, list[str]] = {}
        for key in sorted(modified_issue_keys(jira_dir)):
            detail = item_detail_data(jira_dir, key)
            if detail is None:
                continue
            project = issue_project_key(detail["issue"])
            if project:
                keys_by_project.setdefault(project, []).append(key)

        if not keys_by_project:
            return redirect("Nothing to push", "success")

        pushed = blocked = failed = 0
        errors: list[str] = []
        for project, keys in keys_by_project.items():
            try:
                _, client = api_client_from_config(project, config.jira_url, config.jira_email, config.jira_api_token)
            except MetadataError as exc:
                failed += len(keys)
                errors.append(str(exc))
                continue
            component_field = config.effective_component_field(project) or "components"
            result = push_shadows(jira_dir, keys, client, component_field=component_field)
            pushed += result.pushed
            blocked += result.blocked
            failed += result.failed
            errors.extend(result.errors)

        if failed:
            detail_suffix = f": {errors[0]}" if errors else ""
            return redirect(f"{pushed} pushed, {failed} failed{detail_suffix}", "error")
        if blocked:
            return redirect(f"{pushed} pushed, {blocked} blocked (unmet dependency)", "error")
        return redirect(f"{pushed} pushed to Jira", "success")

    def _configured_projects() -> list[str]:
        return [p.key for p in config.resolved_projects()]

    def _redirect_to_meta_section(
        section: str, project: str, *, flash: str, flash_kind: str = "success", q: str | None = None
    ) -> RedirectResponse:
        params = {"project": project, "flash": flash, "flash_kind": flash_kind}
        if q:
            params["q"] = q
        return RedirectResponse(url=f"/meta/{section}?{urlencode(params)}", status_code=303)

    @app.get("/meta", response_class=HTMLResponse)
    def meta_page() -> str:
        return render_meta_projects_page(_configured_projects())

    @app.get("/meta/versions", response_class=HTMLResponse)
    def meta_versions_page(
        project: str | None = None,
        q: str | None = None,
        show_archived: str | None = None,
        flash: str | None = None,
        flash_kind: str | None = None,
    ) -> str:
        projects = _configured_projects()
        if not project:
            return render_meta_projects_page(projects, section="versions")
        try:
            versions = meta_versions_data(jira_dir, config, project)
        except MetadataError:
            versions = []
        show_archived_on = show_archived == "1"
        if not show_archived_on:
            # Archived versions are the ones you're least likely to still
            # want to act on (rename/release/delete) -- an active project
            # can accumulate dozens of them over time, crowding out the
            # handful of current versions this page is normally opened
            # for. Hidden by default, one checkbox away when you do need
            # to find one.
            versions = [version for version in versions if not version.get("archived")]
        filtered = _filter_by_name(versions, q)
        table = _versions_rows(filtered, project) if filtered else ""
        empty_message = "No versions match that filter." if q else "No versions loaded for this project."
        return render_meta_section_page(
            "versions",
            project,
            projects,
            table,
            q,
            empty_message,
            write_forms_html=_add_version_form(project),
            extra_filter_html=f"""
            <label class="checkbox-row" title="Also list versions Jira has archived">
              <input type="checkbox" name="show_archived" value="1" {"checked" if show_archived_on else ""}
                onchange="this.form.submit()"> Show archived versions
            </label>
            """,
            flash=flash,
            flash_kind=flash_kind,
        )

    @app.post("/meta/versions/add", dependencies=[Depends(_require_same_origin)])
    def post_meta_version_add(project: str = Form(...), name: str = Form(...)) -> RedirectResponse:
        try:
            message = add_meta_version(
                jira_dir, name, project, config.jira_url, config.jira_email, config.jira_api_token
            )
            flash_kind = "success"
        except MetadataError as exc:
            message, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("versions", project, flash=message.splitlines()[0], flash_kind=flash_kind)

    @app.post("/meta/versions/rename", dependencies=[Depends(_require_same_origin)])
    def post_meta_version_rename(
        project: str = Form(...), identifier: str = Form(...), new_name: str = Form(...)
    ) -> RedirectResponse:
        try:
            message = rename_meta_version(
                jira_dir, identifier, new_name, project, config.jira_url, config.jira_email, config.jira_api_token
            )
            flash_kind = "success"
        except MetadataError as exc:
            message, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("versions", project, flash=message.splitlines()[0], flash_kind=flash_kind)

    @app.post("/meta/versions/release", dependencies=[Depends(_require_same_origin)])
    def post_meta_version_release(project: str = Form(...), identifier: str = Form(...)) -> RedirectResponse:
        try:
            message = release_meta_version(
                jira_dir, identifier, None, project, config.jira_url, config.jira_email, config.jira_api_token
            )
            flash_kind = "success"
        except MetadataError as exc:
            message, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("versions", project, flash=message.splitlines()[0], flash_kind=flash_kind)

    @app.post("/meta/versions/archive", dependencies=[Depends(_require_same_origin)])
    def post_meta_version_archive(project: str = Form(...), identifier: str = Form(...)) -> RedirectResponse:
        try:
            message = archive_meta_version(
                jira_dir, identifier, project, config.jira_url, config.jira_email, config.jira_api_token
            )
            flash_kind = "success"
        except MetadataError as exc:
            message, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("versions", project, flash=message.splitlines()[0], flash_kind=flash_kind)

    @app.post("/meta/versions/delete", dependencies=[Depends(_require_same_origin)])
    def post_meta_version_delete(project: str = Form(...), identifier: str = Form(...)) -> RedirectResponse:
        try:
            message = delete_meta_version(
                jira_dir, identifier, None, project, config.jira_url, config.jira_email, config.jira_api_token
            )
            flash_kind = "success"
        except MetadataError as exc:
            message, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("versions", project, flash=message.splitlines()[0], flash_kind=flash_kind)

    @app.get("/meta/components", response_class=HTMLResponse)
    def meta_components_page(
        project: str | None = None, q: str | None = None, flash: str | None = None, flash_kind: str | None = None
    ) -> str:
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
        component_field = config.effective_component_field(project)
        return render_meta_section_page(
            "components",
            project,
            projects,
            table,
            q,
            empty_message,
            write_forms_html=_add_component_form(project, component_field),
            flash=flash,
            flash_kind=flash_kind,
        )

    @app.post("/meta/components/add", dependencies=[Depends(_require_same_origin)])
    def post_meta_component_add(project: str = Form(...), name: str = Form(...)) -> RedirectResponse:
        try:
            message = add_meta_component(
                jira_dir, name, project, config.jira_url, config.jira_email, config.jira_api_token
            )
            flash_kind = "success"
        except MetadataError as exc:
            message, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("components", project, flash=message.splitlines()[0], flash_kind=flash_kind)

    @app.post("/meta/components/add-option", dependencies=[Depends(_require_same_origin)])
    def post_meta_component_field_option_add(
        project: str = Form(...), name: str = Form(...), context_id: str = Form("")
    ) -> RedirectResponse:
        component_field = config.effective_component_field(project)
        try:
            message = add_meta_component_field_option(
                jira_dir,
                name,
                project,
                component_field,
                context_id or None,
                config.jira_url,
                config.jira_email,
                config.jira_api_token,
            )
            flash_kind = "success"
        except MetadataError as exc:
            message, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("components", project, flash=message.splitlines()[0], flash_kind=flash_kind)

    @app.get("/meta/boards", response_class=HTMLResponse)
    def meta_boards_page(
        project: str | None = None, q: str | None = None, flash: str | None = None, flash_kind: str | None = None
    ) -> str:
        projects = _configured_projects()
        if not project:
            return render_meta_projects_page(projects, section="boards")
        try:
            boards = meta_boards_data(jira_dir, config, project)
        except MetadataError:
            boards = []
        filtered = _filter_by_name(boards, q)
        table = _boards_rows_with_actions(filtered, project) if filtered else ""
        empty_message = "No boards match that filter." if q else "No boards loaded for this project."
        return render_meta_section_page(
            "boards",
            project,
            projects,
            table,
            q,
            empty_message,
            write_forms_html=_add_local_board_form(project),
            flash=flash,
            flash_kind=flash_kind,
        )

    @app.post("/meta/boards/add", dependencies=[Depends(_require_same_origin)])
    def post_meta_board_add(
        project: str = Form(...),
        name: str = Form(...),
        board_project: str = Form(""),
        status: str = Form(""),
        component: str = Form(""),
        assignee: str = Form(""),
        fix_version: str = Form(""),
        pattern: str = Form(""),
    ) -> RedirectResponse:
        raw_filters = {
            "project": board_project,
            "status": status,
            "component": component,
            "assignee": assignee,
            "fixVersion": fix_version,
        }
        field_filters = {
            field: [v.strip() for v in value.split(",") if v.strip()] for field, value in raw_filters.items() if value.strip()
        }
        try:
            add_local_board(jira_dir, name, field_filters, pattern.strip() or None)
            flash, flash_kind = f"created local board {name}", "success"
        except MetadataError as exc:
            flash, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("boards", project, flash=flash, flash_kind=flash_kind)

    @app.post("/meta/boards/delete", dependencies=[Depends(_require_same_origin)])
    def post_meta_board_delete(project: str = Form(...), name: str = Form(...)) -> RedirectResponse:
        try:
            delete_local_board(jira_dir, name)
            flash, flash_kind = f"deleted local board {name}", "success"
        except MetadataError as exc:
            flash, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("boards", project, flash=flash, flash_kind=flash_kind)

    @app.post("/meta/boards/toggle-active", dependencies=[Depends(_require_same_origin)])
    def post_meta_board_toggle_active(
        project: str = Form(...), kind: str = Form(...), identifier: str = Form(...), active: str = Form(...)
    ) -> RedirectResponse:
        try:
            set_board_active(jira_dir, kind, identifier, active == "1")
            verb = "enabled" if active == "1" else "disabled"
            flash, flash_kind = f"{verb} board {identifier}", "success"
        except MetadataError as exc:
            flash, flash_kind = str(exc), "error"
        return _redirect_to_meta_section("boards", project, flash=flash, flash_kind=flash_kind)

    @app.get("/meta/labels", response_class=HTMLResponse)
    def meta_labels_page(
        project: str | None = None, q: str | None = None, flash: str | None = None, flash_kind: str | None = None
    ) -> str:
        projects = _configured_projects()
        if not project:
            return render_meta_projects_page(projects, section="labels")
        component_field = config.effective_component_field(project)
        items = [item for item in load_manifest_items(jira_dir, component_field) if item.get("project") == project]
        counts = label_counts(items)
        filtered = [(label, count) for label, count in counts if not q or q.strip().lower() in label.lower()]
        table = _labels_rows(filtered, project) if filtered else ""
        empty_message = "No labels match that filter." if q else "No labels found for this project."
        note = (
            "<p><em>Jira has no standalone label registry -- apply a new label to an issue "
            "(Detail or New issue page) and it will appear here.</em></p>"
        )
        return render_meta_section_page(
            "labels", project, projects, table, q, empty_message, write_forms_html=note, flash=flash, flash_kind=flash_kind
        )

    @app.post("/meta/labels/rename", dependencies=[Depends(_require_same_origin)])
    def post_meta_label_rename(project: str = Form(...), label: str = Form(...), new_name: str = Form(...)) -> RedirectResponse:
        if not new_name.strip():
            return _redirect_to_meta_section("labels", project, flash="new label name is required", flash_kind="error")
        component_field = config.effective_component_field(project)
        try:
            result = bulk_edit_label(
                jira_dir,
                label,
                new_name.strip(),
                project,
                config.jira_url,
                config.jira_email,
                config.jira_api_token,
                component_field,
            )
        except ShadowError as exc:
            return _redirect_to_meta_section("labels", project, flash=str(exc), flash_kind="error")
        flash, flash_kind = _label_bulk_edit_flash(result, f"renamed '{label}' to '{new_name.strip()}'")
        return _redirect_to_meta_section("labels", project, flash=flash, flash_kind=flash_kind)

    @app.post("/meta/labels/delete", dependencies=[Depends(_require_same_origin)])
    def post_meta_label_delete(project: str = Form(...), label: str = Form(...)) -> RedirectResponse:
        component_field = config.effective_component_field(project)
        try:
            result = bulk_edit_label(
                jira_dir, label, None, project, config.jira_url, config.jira_email, config.jira_api_token, component_field
            )
        except ShadowError as exc:
            return _redirect_to_meta_section("labels", project, flash=str(exc), flash_kind="error")
        flash, flash_kind = _label_bulk_edit_flash(result, f"deleted '{label}'")
        return _redirect_to_meta_section("labels", project, flash=flash, flash_kind=flash_kind)

    return app


def serve(
    jira_dir: Path, host: str, port: int, config: WorkbenchConfig | None = None, config_path: Path | None = None
) -> None:
    app = create_app(jira_dir, config if config is not None else WorkbenchConfig(), config_path)
    print(f"Serving Jira Workbench at http://{host}:{port}")
    uvicorn.run(app, host=host, port=int(port), log_level="warning")
