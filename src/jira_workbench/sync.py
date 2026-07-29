from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class JiraSyncClient(Protocol):
    def enhanced_jql_get_list_of_tickets(
        self,
        jql: str,
        fields: str | list[str] = "*all",
        limit: int | None = None,
        expand: str | None = None,
    ) -> list[dict[str, Any]]:
        pass

    def get_issue(
        self,
        issue_id_or_key: str,
        fields: str | list | tuple | set | None = None,
        properties: str | None = None,
        update_history: bool = True,
        expand: str | None = None,
    ) -> Any:
        pass

    def issue_get_comments(self, issue_id: str) -> Any:
        pass

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        pass

    def resource_url(self, resource: str, api_root: str = "rest/api", api_version: str | int = "latest") -> str:
        pass


class SyncError(RuntimeError):
    pass


ISSUE_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*-\d+")

Progress = Callable[[str], None]


@dataclass(frozen=True)
class SyncConfig:
    project: str
    jira_dir: Path
    component_field: str = "components"
    force: bool = False
    # None means unbounded (sync everything, today's default). Set to bound
    # how far back Done issues are pulled in -- a project with thousands of
    # ancient closed tickets doesn't need to keep re-fetching all of them on
    # every sync. Currently-open issues are never excluded regardless of age;
    # only issues already in Jira's Done status category are subject to this.
    # Issues that age out (or that a newly-added cutoff excludes) are simply
    # left alone on disk, not deleted -- see build_index_jql.
    history_months: int | None = None


@dataclass(frozen=True)
class SyncResult:
    work_item_count: int
    changed_count: int
    skipped_count: int
    version_count: int = 0
    backfilled_parent_count: int = 0


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_work_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("values", "issues"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def normalize_issue(payload: Any, key: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, list):
        shape = f"list with {len(payload)} items"
    else:
        shape = type(payload).__name__
    raise SyncError(
        f"Jira work item {key} returned unexpected JSON from the Jira API ({shape}). "
        "Expected a single issue object."
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def updated_at(issue: dict[str, Any]) -> str | None:
    fields = issue.get("fields")
    if isinstance(fields, dict):
        value = fields.get("updated")
        if isinstance(value, str):
            return value
    value = issue.get("updated")
    return value if isinstance(value, str) else None


def component_slug(issue: dict[str, Any], field_name: str) -> str:
    fields = issue.get("fields")
    value: Any = None
    if isinstance(fields, dict):
        field_value = fields.get(field_name)
        if isinstance(field_value, list):
            parts = [field_value_name(item) for item in field_value]
            value = "--".join(part for part in parts if part)
        elif isinstance(field_value, dict):
            value = field_value_name(field_value)
        elif isinstance(field_value, str):
            value = field_value
    if not value:
        value = "_unassigned"
    slug = re.sub(r"[^a-z0-9._-]", "-", str(value).lower())
    if not slug or set(slug) <= {"."}:
        return "_unassigned"
    return slug


def field_value_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("value", "name", "displayName", "key"):
            item = value.get(key)
            if isinstance(item, str):
                return item
    if isinstance(value, str):
        return value
    return ""


def issue_key_sort_key(key: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)-(\d+)", key)
    if match:
        return (match.group(1).upper(), int(match.group(2)), "")
    return (key.upper(), -1, key)


def issue_project_key(issue: dict[str, Any]) -> str | None:
    """The Jira project an issue belongs to, e.g. "SAT" for issue "SAT-123".

    Prefers the issue's own synced fields.project.key (present since a sync
    fetches fields="*all"); falls back to the issue key's own letter prefix
    if that field is ever missing. Lives here (the lowest layer both
    shadow.py and view.py already depend on) rather than in either -- pure,
    operates on an already-loaded issue dict, no I/O.
    """
    fields = issue.get("fields")
    if isinstance(fields, dict):
        project = fields.get("project")
        if isinstance(project, dict):
            key = project.get("key")
            if isinstance(key, str) and key:
                return key
    key = issue.get("key")
    if isinstance(key, str):
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)-\d+", key)
        if match:
            return match.group(1)
    return None


def status_category_key(status: Any) -> str | None:
    """Jira's real status classification (statusCategory.key: "new" /
    "indeterminate" / "done"), not the display name -- a project's
    workflow can name its statuses anything ("Solved", "Verified",
    "Shipped"), but every one of them still belongs to one of these three
    fixed system categories. Returns None if `status` isn't a full status
    dict (e.g. it's been shadow-overwritten to a bare name, or predates
    this app syncing statusCategory at all)."""
    if not isinstance(status, dict):
        return None
    category = status.get("statusCategory")
    if not isinstance(category, dict):
        return None
    key = category.get("key")
    return key if isinstance(key, str) and key else None


# Last-resort fallback only, for a status whose real statusCategory isn't
# known yet (a manifest/issue.json synced before status_category_key
# existed, or -- for a bare shadow-overwritten status name -- one that's
# never been observed anywhere else in the locally synced instance
# either). A custom workflow status not covered by these literal English
# words is exactly the bug this whole mechanism exists to avoid; this is
# only a stopgap until the next sync repopulates the real category.
FALLBACK_DONE_STATUS_NAMES = {"close", "closed", "done", "resolved"}


def issue_path_sort_key(path: Path) -> tuple[str, int, str]:
    return issue_key_sort_key(path.parent.name)


def find_existing_issue(components_dir: Path, key: str) -> Path | None:
    if not components_dir.exists():
        return None
    for path in components_dir.glob(f"*/{key}/issue.json"):
        return path
    return None


def build_index_jql(project: str, history_months: int | None = None) -> str:
    """The JQL used to list a project's issues for syncing.

    With no cutoff, every issue in the project is listed (today's default).
    With a cutoff, currently-open issues are still listed regardless of age
    -- only issues Jira already considers Done are excluded, and only once
    they've been in that status for longer than the cutoff. This uses
    statuscategorychangedate (how long an issue has sat in its current
    status), the same field [view].hide_done_after_days already keys off of,
    rather than `updated` -- an unrelated bulk edit (e.g. relabeling) bumps
    `updated` without meaning the issue is any less stale.
    """
    if history_months is None:
        return f"project={project} ORDER BY key"
    days = history_months * 30
    return f"project={project} AND (statusCategory != Done OR statuscategorychangedate >= -{days}d) ORDER BY key"


def fetch_work_item_index(
    project: str, client: JiraSyncClient, *, history_months: int | None = None
) -> list[dict[str, Any]]:
    payload = client.enhanced_jql_get_list_of_tickets(
        build_index_jql(project, history_months),
        fields=["issuetype", "key", "updated"],
    )
    return normalize_work_items(payload)


def issue_attachments(issue: dict[str, Any]) -> Any:
    fields = issue.get("fields")
    if isinstance(fields, dict) and isinstance(fields.get("attachment"), list):
        return fields["attachment"]
    return []


def fetch_issue_comments(client: JiraSyncClient, key: str) -> Any:
    payload = client.issue_get_comments(key)
    if not isinstance(payload, dict):
        return payload
    comments = list(payload.get("comments") or [])
    total = payload.get("total")
    max_results = payload.get("maxResults")
    start_at = payload.get("startAt")
    if isinstance(total, int) and isinstance(max_results, int) and isinstance(start_at, int) and max_results > 0:
        next_start = start_at + max_results
        comment_url = f"{client.resource_url('issue')}/{key}/comment"
        while next_start < total:
            page = client.get(comment_url, params={"startAt": next_start, "maxResults": max_results})
            page_comments = page.get("comments") if isinstance(page, dict) else None
            if not page_comments:
                break
            comments.extend(page_comments)
            next_start += len(page_comments)
    return {**payload, "comments": comments}


def issue_summary(component: str, key: str, issue_path: Path, base_dir: Path) -> dict[str, str | None]:
    issue = read_json(issue_path)
    fields = issue.get("fields") if isinstance(issue, dict) else {}
    if not isinstance(fields, dict):
        fields = {}
    status = fields.get("status")
    issue_type = fields.get("issuetype")
    priority = fields.get("priority")
    assignee = fields.get("assignee")
    fix_versions = fields.get("fixVersions")
    parent = fields.get("parent")
    parent_fields = parent.get("fields") if isinstance(parent, dict) else {}
    if not isinstance(parent_fields, dict):
        parent_fields = {}
    directory = issue_path.parent
    return {
        "component": component,
        "epic": parent.get("key") if isinstance(parent, dict) else None,
        "epicSummary": parent_fields.get("summary") if isinstance(parent_fields.get("summary"), str) else None,
        "fixVersion": field_value_name(fix_versions[0]) if isinstance(fix_versions, list) and fix_versions else None,
        "key": key,
        "priority": priority.get("name") if isinstance(priority, dict) else None,
        "assignee": assignee.get("displayName") if isinstance(assignee, dict) else None,
        "summary": fields.get("summary") if isinstance(fields.get("summary"), str) else None,
        "status": status.get("name") if isinstance(status, dict) else None,
        "statusCategory": status_category_key(status),
        "type": issue_type.get("name") if isinstance(issue_type, dict) else None,
        "updated": updated_at(issue) if isinstance(issue, dict) else None,
        "path": directory.relative_to(base_dir).as_posix(),
        "project": issue_project_key(issue) if isinstance(issue, dict) else None,
    }


def build_manifest(jira_dir: Path) -> dict[str, Any]:
    components_dir = jira_dir / "components"
    items: list[dict[str, Any]] = []
    if components_dir.exists():
        for issue_path in sorted(components_dir.glob("*/*/issue.json"), key=issue_path_sort_key):
            issue_dir = issue_path.parent
            component = issue_dir.parent.name
            key = issue_dir.name
            items.append(issue_summary(component, key, issue_path, jira_dir))

    components = []
    for component in sorted({item["component"] for item in items if item.get("component")}):
        work_items = sorted(
            (item["key"] for item in items if item["component"] == component),
            key=issue_key_sort_key,
        )
        components.append({"component": component, "count": len(work_items), "workItems": work_items})

    manifest = {
        "generatedAt": utc_now(),
        "workItemCount": len(items),
        "components": components,
        "workItems": items,
    }
    write_json(jira_dir / "manifest.json", manifest)
    return manifest


def backfill_missing_parents(
    jira_dir: Path,
    project: str,
    client: JiraSyncClient,
    component_field: str,
    *,
    progress: Progress | None = None,
) -> int:
    """Ensure every locally synced issue's parent is itself synced, even
    one a history_months cutoff would otherwise exclude -- any parent
    (Epic, Story, Task, whatever `fields.parent` points at -- this isn't
    Epic-specific) closed over a year ago can still have a child that's
    open right now (or was itself touched more recently), and a child
    whose own parent was never fetched is exactly what forces the index's
    swimlane header to fall back to a denormalized (and possibly very
    long) cached summary instead of a real, resolvable issue.

    Loops until a pass finds nothing new to backfill, so a multi-level gap
    (e.g. a subtask's parent Story, and that Story's own parent Epic, both
    missing) gets fully resolved, not just one hop of it. Only backfills
    same-project parents: a parent that lives in a different project needs
    that project configured and synced too, which this can't do on its
    own. Each pass re-reads every locally cached issue (same full-directory
    walk build_manifest already does every sync, so no new order of
    magnitude of I/O), so this also reconciles gaps left over from before
    this existed, not just ones from this run.
    """
    components_dir = jira_dir / "components"
    if not components_dir.exists():
        return 0

    backfilled = 0
    unfetchable: set[str] = set()
    while True:
        missing_keys: dict[str, None] = {}
        for issue_path in components_dir.glob("*/*/issue.json"):
            issue = read_json(issue_path)
            if not isinstance(issue, dict):
                continue
            fields = issue.get("fields")
            parent = fields.get("parent") if isinstance(fields, dict) else None
            parent_key = parent.get("key") if isinstance(parent, dict) else None
            if not isinstance(parent_key, str) or not parent_key or parent_key in unfetchable:
                continue
            if issue_project_key({"key": parent_key}) != project:
                continue
            if find_existing_issue(components_dir, parent_key) is not None:
                continue
            missing_keys[parent_key] = None

        if not missing_keys:
            break

        total = len(missing_keys)
        progressed = False
        for index, key in enumerate(missing_keys, start=1):
            if progress:
                progress(f"[4/6] Backfilling parents excluded by the history cutoff... {index}/{total} {key}")
            try:
                issue = normalize_issue(client.get_issue(key, fields="*all"), key)
            except Exception as exc:
                unfetchable.add(key)
                if progress:
                    progress(f"could not backfill parent {key}: {exc}")
                continue
            component = component_slug(issue, component_field)
            dest = components_dir / component / key
            dest.mkdir(parents=True, exist_ok=True)
            write_json(dest / "issue.json", issue)
            write_json(dest / "comments.json", fetch_issue_comments(client, key))
            write_json(dest / "attachments.json", issue_attachments(issue))
            write_json(dest / "sync.json", {"key": key, "component": component, "syncedAt": utc_now()})
            backfilled += 1
            progressed = True

        if not progressed:
            break
    return backfilled


def sync_project(
    config: SyncConfig,
    client: JiraSyncClient,
    *,
    progress: Progress | None = print,
) -> SyncResult:
    jira_dir = config.jira_dir
    components_dir = jira_dir / "components"
    components_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress("[1/6] Refreshing project metadata...")
    from .issue import fetch_all_field_names
    from .metadata import refresh_versions_api, remember_field_names

    version_count = 0
    try:
        version_cache = refresh_versions_api(jira_dir, config.project, client)
        versions = version_cache.get("versions")
        version_count = len(versions) if isinstance(versions, list) else 0
    except Exception as exc:
        if progress:
            progress(f"metadata refresh skipped: {exc}")

    try:
        # Populated here (not just opportunistically from the TUI's Detail
        # screen) so a CLI-only workflow -- `jira-wb shadow set` / `shadow
        # report`, no TUI involved -- still shows friendly custom field
        # names instead of raw ids like "customfield_10082".
        remember_field_names(jira_dir, fetch_all_field_names(client))
    except Exception as exc:
        if progress:
            progress(f"field name refresh skipped: {exc}")

    if progress:
        progress("[2/6] Refreshing project index...")
    work_items = fetch_work_item_index(config.project, client, history_months=config.history_months)
    write_json(jira_dir / "project.json", work_items)

    if progress:
        progress("[3/6] Syncing changed issues...")
    changed = 0
    skipped = 0
    total = len(work_items)
    for index, work_item in enumerate(work_items, start=1):
        key = work_item.get("key")
        if not isinstance(key, str) or not ISSUE_KEY_PATTERN.fullmatch(key):
            continue

        if progress:
            progress(
                f"[3/6] Syncing changed issues... {index}/{total} {key} "
                f"changed={changed} unchanged={skipped}"
            )

        existing = find_existing_issue(components_dir, key)
        index_updated = updated_at(work_item)
        if not config.force and existing is not None and index_updated is not None:
            existing_issue = read_json(existing)
            if isinstance(existing_issue, dict) and updated_at(existing_issue) == index_updated:
                skipped += 1
                continue

        issue = normalize_issue(client.get_issue(key, fields="*all"), key)

        if not config.force and existing is not None:
            existing_issue = read_json(existing)
            if isinstance(existing_issue, dict) and updated_at(existing_issue) == updated_at(issue):
                skipped += 1
                continue

        component = component_slug(issue, config.component_field)
        dest = components_dir / component / key
        old_dest = existing.parent if existing is not None else None
        shadow = None
        if old_dest is not None:
            shadow_path = old_dest / "shadow.json"
            if shadow_path.exists():
                shadow = read_json(shadow_path)

        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        write_json(dest / "issue.json", issue)
        write_json(dest / "comments.json", fetch_issue_comments(client, key))
        write_json(dest / "attachments.json", issue_attachments(issue))
        write_json(dest / "sync.json", {"key": key, "component": component, "syncedAt": utc_now()})
        if shadow is not None:
            write_json(dest / "shadow.json", shadow)

        if old_dest is not None and old_dest != dest and old_dest.exists():
            shutil.rmtree(old_dest)
        changed += 1

    if progress:
        progress("[4/6] Backfilling parents excluded by the history cutoff...")
    backfilled = backfill_missing_parents(jira_dir, config.project, client, config.component_field, progress=progress)

    if progress:
        progress("[5/6] Building manifest...")
    build_manifest(jira_dir)
    if progress:
        progress("[6/6] Done.")
    return SyncResult(
        work_item_count=len(work_items),
        changed_count=changed,
        skipped_count=skipped,
        version_count=version_count,
        backfilled_parent_count=backfilled,
    )
