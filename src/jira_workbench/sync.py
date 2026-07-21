from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class JsonRunner(Protocol):
    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        pass


class SyncError(RuntimeError):
    pass


Progress = Callable[[str], None]


@dataclass(frozen=True)
class SyncConfig:
    project: str
    component_field: str
    jira_dir: Path


@dataclass(frozen=True)
class SyncResult:
    work_item_count: int
    changed_count: int
    skipped_count: int


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
        f"Jira work item {key} returned unexpected JSON from acli view ({shape}). "
        f"Check: acli jira workitem view {key} --fields '*all' --json"
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
        if isinstance(field_value, dict):
            value = field_value.get("value")
        elif isinstance(field_value, str):
            value = field_value
    if not value:
        value = "_unassigned"
    slug = re.sub(r"[^a-z0-9._-]", "-", str(value).lower())
    return slug or "_unassigned"


def find_existing_issue(components_dir: Path, key: str) -> Path | None:
    if not components_dir.exists():
        return None
    for path in components_dir.glob(f"*/{key}/issue.json"):
        return path
    return None


def issue_summary(component: str, key: str, issue_path: Path, base_dir: Path) -> dict[str, str | None]:
    issue = read_json(issue_path)
    fields = issue.get("fields") if isinstance(issue, dict) else {}
    if not isinstance(fields, dict):
        fields = {}
    status = fields.get("status")
    issue_type = fields.get("issuetype")
    directory = issue_path.parent
    return {
        "component": component,
        "key": key,
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
        for issue_path in sorted(components_dir.glob("*/*/issue.json")):
            issue_dir = issue_path.parent
            component = issue_dir.parent.name
            key = issue_dir.name
            items.append(issue_summary(component, key, issue_path, jira_dir))

    components = []
    for component in sorted({item["component"] for item in items if item.get("component")}):
        work_items = [item["key"] for item in items if item["component"] == component]
        components.append({"component": component, "count": len(work_items), "workItems": work_items})

    manifest = {
        "generatedAt": utc_now(),
        "workItemCount": len(items),
        "components": components,
        "workItems": items,
    }
    write_json(jira_dir / "manifest.json", manifest)
    return manifest


def sync_project(config: SyncConfig, runner: JsonRunner, *, progress: Progress | None = print) -> SyncResult:
    jira_dir = config.jira_dir
    components_dir = jira_dir / "components"
    components_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress("[1/4] Refreshing project index...")
    index_payload = runner.json(
        [
            "jira",
            "workitem",
            "search",
            "--jql",
            f"project={config.project} ORDER BY key",
            "--fields",
            "issuetype,key",
            "--paginate",
            "--json",
        ]
    )
    work_items = normalize_work_items(index_payload)
    write_json(jira_dir / "project.json", work_items)

    if progress:
        progress("[2/4] Syncing changed issues...")
    changed = 0
    skipped = 0
    for work_item in work_items:
        key = work_item.get("key")
        if not isinstance(key, str) or not key:
            continue

        existing = find_existing_issue(components_dir, key)
        issue = normalize_issue(
            runner.json(["jira", "workitem", "view", key, "--fields", "*all", "--json"]),
            key,
        )

        if existing is not None:
            existing_issue = read_json(existing)
            if isinstance(existing_issue, dict) and updated_at(existing_issue) == updated_at(issue):
                skipped += 1
                continue

        component = component_slug(issue, config.component_field)
        dest = components_dir / component / key
        old_dest = existing.parent if existing is not None else None

        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        write_json(dest / "issue.json", issue)
        write_json(
            dest / "comments.json",
            runner.json(
                ["jira", "workitem", "comment", "list", "--key", key, "--paginate", "--json"],
                allow_failure=True,
            ),
        )
        write_json(
            dest / "attachments.json",
            runner.json(
                ["jira", "workitem", "attachment", "list", "--key", key, "--json"],
                allow_failure=True,
            ),
        )
        write_json(dest / "sync.json", {"key": key, "component": component, "syncedAt": utc_now()})

        if old_dest is not None and old_dest != dest and old_dest.exists():
            shutil.rmtree(old_dest)
        changed += 1

    if progress:
        progress("[3/4] Building manifest...")
    build_manifest(jira_dir)
    if progress:
        progress("[4/4] Done.")
    return SyncResult(work_item_count=len(work_items), changed_count=changed, skipped_count=skipped)
