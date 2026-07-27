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


@dataclass(frozen=True)
class SyncResult:
    work_item_count: int
    changed_count: int
    skipped_count: int
    version_count: int = 0


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


def issue_path_sort_key(path: Path) -> tuple[str, int, str]:
    return issue_key_sort_key(path.parent.name)


def find_existing_issue(components_dir: Path, key: str) -> Path | None:
    if not components_dir.exists():
        return None
    for path in components_dir.glob(f"*/{key}/issue.json"):
        return path
    return None


def fetch_work_item_index(project: str, client: JiraSyncClient) -> list[dict[str, Any]]:
    payload = client.enhanced_jql_get_list_of_tickets(
        f"project={project} ORDER BY key",
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
        "type": issue_type.get("name") if isinstance(issue_type, dict) else None,
        "updated": updated_at(issue) if isinstance(issue, dict) else None,
        "path": directory.relative_to(base_dir).as_posix(),
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
        progress("[1/5] Refreshing project metadata...")
    from .metadata import refresh_versions_api

    version_count = 0
    try:
        version_cache = refresh_versions_api(jira_dir, config.project, client)
        versions = version_cache.get("versions")
        version_count = len(versions) if isinstance(versions, list) else 0
    except Exception as exc:
        if progress:
            progress(f"metadata refresh skipped: {exc}")

    if progress:
        progress("[2/5] Refreshing project index...")
    work_items = fetch_work_item_index(config.project, client)
    write_json(jira_dir / "project.json", work_items)

    if progress:
        progress("[3/5] Syncing changed issues...")
    changed = 0
    skipped = 0
    total = len(work_items)
    for index, work_item in enumerate(work_items, start=1):
        key = work_item.get("key")
        if not isinstance(key, str) or not ISSUE_KEY_PATTERN.fullmatch(key):
            continue

        if progress:
            progress(
                f"[3/5] Syncing changed issues... {index}/{total} {key} "
                f"changed={changed} unchanged={skipped}"
            )

        existing = find_existing_issue(components_dir, key)
        index_updated = updated_at(work_item)
        if existing is not None and index_updated is not None:
            existing_issue = read_json(existing)
            if isinstance(existing_issue, dict) and updated_at(existing_issue) == index_updated:
                skipped += 1
                continue

        issue = normalize_issue(client.get_issue(key, fields="*all"), key)

        if existing is not None:
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
        progress("[4/5] Building manifest...")
    build_manifest(jira_dir)
    if progress:
        progress("[5/5] Done.")
    return SyncResult(
        work_item_count=len(work_items),
        changed_count=changed,
        skipped_count=skipped,
        version_count=version_count,
    )
