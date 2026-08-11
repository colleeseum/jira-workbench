from __future__ import annotations

import difflib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .metadata import is_project_read_only
from .sync import (
    build_manifest,
    component_slug,
    find_existing_issue,
    issue_key_sort_key,
    issue_project_key,
    read_json,
    updated_at,
    utc_now,
    write_json,
)


API_PUSH_FIELDS = {
    "assignee",
    "description",
    "duedate",
    "fixVersions",
    "labels",
    "parent",
    "priority",
    "summary",
    "type",
}
TRANSITION_PUSH_FIELDS = {"status"}


class ShadowError(RuntimeError):
    pass


class JiraClient(Protocol):
    def get_issue(self, issue_id_or_key: str, fields: str | list | tuple | set | None = None, **kwargs: Any) -> Any:
        pass

    def get_all_resolutions(self) -> Any:
        pass

    def issue_transition(self, issue_key: str, status: str) -> Any:
        pass

    def issue_update(
        self,
        issue_key: str,
        fields: str | dict[str, Any],
        update: dict[Any, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        pass

    def issue_add_comment(self, issue_key: str, comment: str, visibility: dict[str, Any] | None = None) -> Any:
        pass

    def issue_edit_comment(
        self,
        issue_key: str,
        comment_id: str,
        comment: str,
        visibility: dict[str, Any] | None = None,
        notify_users: bool = True,
    ) -> Any:
        pass

    def update_issue_field(self, key: str, fields: dict[str, Any], notify_users: bool = True) -> Any:
        pass

    def resource_url(self, resource: str, api_root: str = "rest/api", api_version: str | int = "latest") -> str:
        pass

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        pass


Progress = Callable[[str], None]


@dataclass(frozen=True)
class PushResult:
    pushed: int
    skipped: int
    blocked: int
    failed: int = 0
    errors: tuple[str, ...] = ()


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_dir(jira_dir: Path, key: str, *, component_hint: str | None = None) -> Path:
    issue_path = find_existing_issue(jira_dir / "components", key, component_hint=component_hint)
    if issue_path is None:
        raise ShadowError(f"work item {key} is not synced locally")
    return issue_path.parent


def issue_path(jira_dir: Path, key: str) -> Path:
    return issue_dir(jira_dir, key) / "issue.json"


def shadow_path(jira_dir: Path, key: str, *, component_hint: str | None = None) -> Path:
    return issue_dir(jira_dir, key, component_hint=component_hint) / "shadow.json"


def load_shadow(jira_dir: Path, key: str, *, component_hint: str | None = None) -> dict[str, Any] | None:
    # component_hint (a manifest item's own already-known "component") lets
    # a caller looping over many items skip find_existing_issue's directory
    # glob -- see its own docstring. Every enriched item already carries
    # this, and with_local_index_fields is exactly that hot loop.
    path = shadow_path(jira_dir, key, component_hint=component_hint)
    if not path.exists():
        return None
    value = read_json(path)
    if not isinstance(value, dict):
        raise ShadowError(f"shadow file for {key} is not a JSON object")
    return value


def new_shadow(jira_dir: Path, key: str) -> dict[str, Any]:
    issue = read_json(issue_path(jira_dir, key))
    if not isinstance(issue, dict):
        raise ShadowError(f"local issue file for {key} is not a JSON object")
    return {
        "key": key,
        "baseUpdated": updated_at(issue),
        "state": "working",
        "fields": {},
        "comments": [],
        "createdAt": now(),
        "updatedAt": now(),
    }


def save_shadow(jira_dir: Path, key: str, shadow: dict[str, Any]) -> None:
    shadow["updatedAt"] = now()
    write_json(shadow_path(jira_dir, key), shadow)
    from . import db as _db  # deferred: avoid a module-level import cycle (db -> view -> shadow)

    _db.set_item_has_shadow(jira_dir, key, True)


def delete_shadow(jira_dir: Path, key: str) -> None:
    path = shadow_path(jira_dir, key)
    if path.exists():
        path.unlink()
    from . import db as _db  # deferred: avoid a module-level import cycle (db -> view -> shadow)

    _db.set_item_has_shadow(jira_dir, key, False)


def ensure_shadow(jira_dir: Path, key: str) -> dict[str, Any]:
    return load_shadow(jira_dir, key) or new_shadow(jira_dir, key)


def raise_if_project_read_only(jira_dir: Path, key: str) -> None:
    """Blocks field/status edits for an issue whose project is marked
    read-only in the local project registry (see metadata.py's
    write_project_registry/is_project_read_only) -- the one enforcement
    point shared by every field-editing caller across the CLI and TUI.
    Comments are unaffected: add_comment/edit_comment/delete_comment/
    remove_local_comment are separate functions and never call this.
    """
    issue = read_json(issue_path(jira_dir, key))
    if not isinstance(issue, dict):
        return
    project = issue_project_key(issue)
    if is_project_read_only(jira_dir, project):
        raise ShadowError(f"{key}: project {project} is read-only (comments are still allowed)")


def set_field(jira_dir: Path, key: str, field: str, value: Any) -> dict[str, Any]:
    raise_if_project_read_only(jira_dir, key)
    shadow = ensure_shadow(jira_dir, key)
    fields = shadow.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ShadowError(f"shadow fields for {key} are not a JSON object")
    fields[field] = value
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def unset_field(jira_dir: Path, key: str, field: str) -> dict[str, Any]:
    raise_if_project_read_only(jira_dir, key)
    shadow = ensure_shadow(jira_dir, key)
    fields = shadow.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ShadowError(f"shadow fields for {key} are not a JSON object")
    fields.pop(field, None)
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def add_comment(jira_dir: Path, key: str, body: str) -> dict[str, Any]:
    shadow = ensure_shadow(jira_dir, key)
    comments = shadow.setdefault("comments", [])
    if not isinstance(comments, list):
        raise ShadowError(f"shadow comments for {key} are not a JSON array")
    comments.append(
        {
            "id": f"local-{uuid4().hex}",
            "body": body,
            "createdAt": now(),
            "state": "working",
        }
    )
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def is_local_comment_id(comment_id: str) -> bool:
    return comment_id.startswith("local-")


def edit_comment(jira_dir: Path, key: str, comment_id: str, body: str) -> dict[str, Any]:
    """Edit a comment's body.

    A local-only (unpushed) comment is mutated in place. An already-synced
    remote comment is instead recorded in commentEdits, keyed by its remote
    id, and applied via issue_edit_comment on the next push.
    """
    shadow = ensure_shadow(jira_dir, key)
    if is_local_comment_id(comment_id):
        comments = shadow.setdefault("comments", [])
        for comment in comments:
            if isinstance(comment, dict) and comment.get("id") == comment_id:
                comment["body"] = body
                break
    else:
        edits = shadow.setdefault("commentEdits", {})
        if not isinstance(edits, dict):
            raise ShadowError(f"shadow commentEdits for {key} are not a JSON object")
        edits[comment_id] = body
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def remove_local_comment(jira_dir: Path, key: str, comment_id: str) -> dict[str, Any]:
    """Discard a local-only (unpushed) comment entirely -- nothing to push, so nothing to mark."""
    shadow = ensure_shadow(jira_dir, key)
    comments = shadow.setdefault("comments", [])
    if not isinstance(comments, list):
        raise ShadowError(f"shadow comments for {key} are not a JSON array")
    shadow["comments"] = [c for c in comments if not (isinstance(c, dict) and c.get("id") == comment_id)]
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def delete_comment(jira_dir: Path, key: str, comment_id: str) -> dict[str, Any]:
    """Mark an already-synced remote comment for deletion on the next push.

    Clears any pending edit for the same comment -- a queued delete wins.
    """
    shadow = ensure_shadow(jira_dir, key)
    deletes = shadow.setdefault("commentDeletes", [])
    if not isinstance(deletes, list):
        raise ShadowError(f"shadow commentDeletes for {key} are not a JSON array")
    if comment_id not in deletes:
        deletes.append(comment_id)
    edits = shadow.get("commentEdits")
    if isinstance(edits, dict):
        edits.pop(comment_id, None)
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def undelete_comment(jira_dir: Path, key: str, comment_id: str) -> dict[str, Any]:
    """Undo a pending delete-on-push mark for a remote comment."""
    shadow = ensure_shadow(jira_dir, key)
    deletes = shadow.get("commentDeletes")
    if isinstance(deletes, list) and comment_id in deletes:
        deletes.remove(comment_id)
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def delete_remote_comment(client: JiraClient, key: str, comment_id: str) -> None:
    """Delete a comment on Jira directly -- atlassian-python-api has no wrapper for this endpoint."""
    url = f"{client.resource_url('issue')}/{key}/comment/{comment_id}"
    client.delete(url)


def set_status_change(
    jira_dir: Path,
    key: str,
    *,
    resolution: str | None = None,
) -> dict[str, Any]:
    raise_if_project_read_only(jira_dir, key)
    shadow = ensure_shadow(jira_dir, key)
    if resolution:
        shadow["statusChange"] = {"resolution": resolution}
    else:
        shadow.pop("statusChange", None)
    shadow["state"] = "working"
    save_shadow(jira_dir, key, shadow)
    return shadow


def commit_shadow(jira_dir: Path, key: str) -> dict[str, Any]:
    shadow = load_shadow(jira_dir, key)
    if shadow is None:
        raise ShadowError(f"work item {key} has no local shadow changes")
    shadow["state"] = "committed"
    shadow["committedAt"] = now()
    for comment in shadow.get("comments", []):
        if isinstance(comment, dict) and comment.get("state") == "working":
            comment["state"] = "committed"
    save_shadow(jira_dir, key, shadow)
    return shadow


def all_shadow_keys(jira_dir: Path) -> list[str]:
    components = jira_dir / "components"
    if not components.exists():
        return []
    return sorted((path.parent.name for path in components.glob("*/*/shadow.json")), key=issue_key_sort_key)


def shadow_status(jira_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for key in all_shadow_keys(jira_dir):
        shadow = load_shadow(jira_dir, key)
        if shadow is None:
            continue
        fields = shadow.get("fields", {})
        comments = shadow.get("comments", [])
        rows.append(
            {
                "key": key,
                "state": shadow.get("state", "working"),
                "fields": len(fields) if isinstance(fields, dict) else 0,
                "comments": len(comments) if isinstance(comments, list) else 0,
                "baseUpdated": shadow.get("baseUpdated"),
            }
        )
    return rows


def field_value(issue: dict[str, Any], field: str) -> Any:
    fields = issue.get("fields", {})
    if not isinstance(fields, dict):
        return None
    if field == "type":
        field = "issuetype"
    value = fields.get(field)
    if isinstance(value, dict) and "name" in value:
        return value["name"]
    return value


def render_diff(jira_dir: Path, key: str) -> str:
    shadow = load_shadow(jira_dir, key)
    if shadow is None:
        return f"{key}: no local shadow changes"
    issue = read_json(issue_path(jira_dir, key))
    if not isinstance(issue, dict):
        raise ShadowError(f"local issue file for {key} is not a JSON object")

    lines = [f"{key} ({shadow.get('state', 'working')})"]
    fields = shadow.get("fields", {})
    if isinstance(fields, dict):
        for field in sorted(fields):
            before = json.dumps(field_value(issue, field), indent=2, sort_keys=True).splitlines()
            after = json.dumps(fields[field], indent=2, sort_keys=True).splitlines()
            lines.append(f"\nfield: {field}")
            lines.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"remote/{field}",
                    tofile=f"shadow/{field}",
                    lineterm="",
                )
            )

    comments = shadow.get("comments", [])
    if isinstance(comments, list) and comments:
        lines.append("\ncomments:")
        for comment in comments:
            if isinstance(comment, dict):
                state = comment.get("state", "working")
                lines.append(f"+ [{state}] {comment.get('body', '')}")
    status_change = shadow.get("statusChange", {})
    if isinstance(status_change, dict) and status_change:
        lines.append("\nstatus change:")
        resolution = status_change.get("resolution")
        if resolution:
            lines.append(f"+ resolution: {resolution}")
    return "\n".join(lines)


def unsupported_fields(shadow: dict[str, Any]) -> list[str]:
    fields = shadow.get("fields", {})
    if not isinstance(fields, dict):
        return []
    return sorted(
        field
        for field in fields
        if field not in API_PUSH_FIELDS and field not in TRANSITION_PUSH_FIELDS and not field.startswith("customfield_")
    )


def remote_updated(client: JiraClient, key: str) -> str | None:
    try:
        payload = client.get_issue(key, fields="updated")
    except Exception as exc:
        raise ShadowError(f"could not read remote updated value for {key}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ShadowError(f"could not read remote updated value for {key}")
    return updated_at(payload)


def resolution_payload(client: JiraClient, resolution: str) -> dict[str, str]:
    normalized = resolution.strip().lower()
    try:
        for item in client.get_all_resolutions():
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            item_name = str(item.get("name") or "")
            if normalized in {item_id.lower(), item_name.lower()}:
                return {"id": item_id} if item_id else {"name": item_name}
    except Exception:
        pass
    return {"name": resolution}


def plain_text_adf(value: str) -> dict[str, Any]:
    paragraphs = value.splitlines() or [""]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line}] if line else [],
            }
            for line in paragraphs
        ],
    }


def user_payload(jira_dir: Path, value: Any) -> Any:
    text = str(value).strip()
    if not text or text == "(unassigned)":
        return None
    for path in (jira_dir / "components").glob("*/*/issue.json"):
        issue = read_json(path)
        fields = issue.get("fields") if isinstance(issue, dict) else {}
        if not isinstance(fields, dict):
            continue
        for field in ("assignee", "reporter"):
            user = fields.get(field)
            if not isinstance(user, dict):
                continue
            candidates = {
                str(user.get("accountId") or ""),
                str(user.get("displayName") or ""),
                str(user.get("emailAddress") or ""),
                str(user.get("name") or ""),
            }
            if text in candidates:
                account_id = str(user.get("accountId") or "")
                if account_id:
                    return {"accountId": account_id}
    return {"accountId": text}


def api_field_name(field: str) -> str:
    return "issuetype" if field == "type" else field


def api_field_value(jira_dir: Path, field: str, value: Any) -> Any:
    if field == "type" and isinstance(value, str):
        return {"name": value}
    if field == "assignee":
        return user_payload(jira_dir, value)
    return value


def _field_identity(value: Any) -> Any:
    """Normalized, hashable identity for a Jira field value, used to decide
    whether two snapshots of the same field genuinely differ. Prefers a
    stable identifier (id/key/accountId) over a display name -- e.g. a fix
    version renamed between the shadow's base and now is the same version,
    not a conflicting edit, the same reasoning as resolve_fix_version_names
    in view.py."""
    if isinstance(value, dict):
        for id_key in ("id", "key", "accountId", "name", "value"):
            if value.get(id_key) is not None:
                return (id_key, value[id_key])
        return json.dumps(value, sort_keys=True) if value else None
    if isinstance(value, list):
        return frozenset(_field_identity(item) for item in value)
    return value


def conflicting_fields(jira_dir: Path, key: str, shadow: dict[str, Any], remote_issue: dict[str, Any]) -> list[str]:
    """Which of the shadow's own edited fields also changed remotely since
    the shadow's base snapshot -- a genuine edit-vs-edit conflict, as opposed
    to the issue's overall `updated` timestamp moving for an unrelated reason
    (someone else's comment, a linked dependency, an unrelated field, etc.).
    Status/resolution is transition-based, not a plain field diff, and is
    excluded here; new comment additions are always safe to push regardless
    (Jira comments are additive) and are likewise not covered by this check.
    """
    base_issue = read_json(issue_path(jira_dir, key))
    base_fields = base_issue.get("fields") if isinstance(base_issue, dict) else None
    remote_fields = remote_issue.get("fields") if isinstance(remote_issue, dict) else None
    shadow_fields = shadow.get("fields")
    if not isinstance(shadow_fields, dict) or not isinstance(base_fields, dict) or not isinstance(remote_fields, dict):
        return list(shadow_fields) if isinstance(shadow_fields, dict) else []
    conflicts = []
    for field in shadow_fields:
        if field == "status":
            continue
        api_name = api_field_name(field)
        if _field_identity(base_fields.get(api_name)) != _field_identity(remote_fields.get(api_name)):
            conflicts.append(field)
    return conflicts


def update_field_summary(field: str, value: Any) -> str:
    if field == "fixVersions" and isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("id")
                if name:
                    names.append(str(name))
            elif item:
                names.append(str(item))
        return f"fixVersions={', '.join(names) if names else '(none)'}"
    if field == "parent" and isinstance(value, dict):
        parent = value.get("key") or value.get("id")
        return f"parent={parent}" if parent else "parent=(unknown)"
    if field == "issuetype" and isinstance(value, dict):
        issue_type = value.get("name") or value.get("id")
        return f"type={issue_type}" if issue_type else "type=(unknown)"
    if field == "priority" and isinstance(value, dict):
        priority = value.get("name") or value.get("id")
        return f"priority={priority}" if priority else "priority=(unknown)"
    if field == "assignee" and isinstance(value, dict):
        assignee = value.get("displayName") or value.get("accountId")
        return f"assignee={assignee}" if assignee else "assignee=(unassigned)"
    return f"{field}={json.dumps(value, sort_keys=True)}"


def update_fields_summary(fields: dict[str, Any]) -> str:
    return "; ".join(update_field_summary(field, value) for field, value in sorted(fields.items()))


def jira_update_error(key: str, fields: dict[str, Any], exc: Exception) -> ShadowError:
    names = ", ".join(sorted(fields)) or "(none)"
    values = update_fields_summary(fields)
    detail = f" ({values})" if values else ""
    hint = ""
    if "fixVersions" in fields:
        hint = "; verify the fixVersion exists in Jira and the value matches exactly"
    return ShadowError(f"{key}: Jira update failed for fields [{names}]{detail}: {exc}{hint}")


def shadow_parent_key(shadow: dict[str, Any]) -> str | None:
    fields = shadow.get("fields")
    if not isinstance(fields, dict):
        return None
    parent = fields.get("parent")
    if not isinstance(parent, dict):
        return None
    key = parent.get("key")
    return key if isinstance(key, str) and key else None


def optional_shadow(jira_dir: Path, key: str) -> dict[str, Any] | None:
    issue_path = find_existing_issue(jira_dir / "components", key)
    if issue_path is None:
        return None
    path = issue_path.parent / "shadow.json"
    if not path.exists():
        return None
    value = read_json(path)
    if not isinstance(value, dict):
        raise ShadowError(f"shadow file for {key} is not a JSON object")
    return value


def local_issue_type(jira_dir: Path, key: str) -> str | None:
    issue_path = find_existing_issue(jira_dir / "components", key)
    if issue_path is None:
        return None
    issue = read_json(issue_path)
    fields = issue.get("fields") if isinstance(issue, dict) else {}
    if not isinstance(fields, dict):
        return None
    issue_type = fields.get("issuetype")
    if isinstance(issue_type, dict):
        name = issue_type.get("name")
        return name if isinstance(name, str) and name else None
    return None


def push_dependencies(jira_dir: Path, keys: list[str]) -> dict[str, set[str]]:
    selected = set(keys)
    dependencies: dict[str, set[str]] = {key: set() for key in keys}
    for key in keys:
        shadow = load_shadow(jira_dir, key)
        if shadow is None:
            continue
        parent_key = shadow_parent_key(shadow)
        if parent_key in selected and parent_key != key:
            dependencies[key].add(parent_key)
    return dependencies


def unselected_parent_blocker(jira_dir: Path, key: str, selected: set[str]) -> str | None:
    shadow = load_shadow(jira_dir, key)
    if shadow is None:
        return None
    parent_key = shadow_parent_key(shadow)
    if parent_key is None or parent_key in selected:
        return None
    parent_shadow = optional_shadow(jira_dir, parent_key)
    if parent_shadow is not None:
        return f"parent {parent_key} has unpushed local changes and is not included in this push"
    parent_type = local_issue_type(jira_dir, parent_key)
    if parent_type is None:
        return f"parent {parent_key} is not synced locally, cannot verify it can accept children"
    if parent_type.lower() != "epic":
        return f"parent {parent_key} is {parent_type}, not Epic"
    return None


def order_push_keys(jira_dir: Path, keys: list[str]) -> tuple[list[str], dict[str, set[str]]]:
    ordered_input = list(dict.fromkeys(keys))
    dependencies = push_dependencies(jira_dir, ordered_input)
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            return
        visiting.add(key)
        for dependency in sorted(dependencies.get(key, set()), key=issue_key_sort_key):
            visit(dependency)
        visiting.remove(key)
        visited.add(key)
        ordered.append(key)

    for key in ordered_input:
        visit(key)
    return ordered, dependencies


def refreshed_comments(client: JiraClient, key: str, issue: dict[str, Any]) -> Any:
    issue_get_comments = getattr(client, "issue_get_comments", None)
    if callable(issue_get_comments):
        return issue_get_comments(key)
    fields = issue.get("fields")
    if isinstance(fields, dict) and isinstance(fields.get("comment"), dict):
        return fields["comment"]
    return {"comments": []}


def refreshed_attachments(issue: dict[str, Any]) -> Any:
    fields = issue.get("fields")
    if isinstance(fields, dict) and isinstance(fields.get("attachment"), list):
        return fields["attachment"]
    return []


def refresh_local_issue_after_push(
    jira_dir: Path,
    key: str,
    jira_client: JiraClient,
    component_field: str,
) -> None:
    try:
        issue = jira_client.get_issue(key, fields="*all")
    except Exception as exc:
        raise ShadowError(f"{key}: Jira push succeeded but local refresh failed: {exc}") from exc
    if not isinstance(issue, dict):
        raise ShadowError(f"{key}: Jira push succeeded but local refresh returned non-object issue")

    components_dir = jira_dir / "components"
    existing = find_existing_issue(components_dir, key)
    old_dest = existing.parent if existing is not None else None
    component = component_slug(issue, component_field)
    dest = components_dir / component / key

    if old_dest is not None and old_dest != dest and dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    write_json(dest / "issue.json", issue)
    write_json(dest / "comments.json", refreshed_comments(jira_client, key, issue))
    write_json(dest / "attachments.json", refreshed_attachments(issue))
    write_json(dest / "sync.json", {"key": key, "component": component, "syncedAt": utc_now()})

    if old_dest is not None and old_dest != dest and old_dest.exists():
        shutil.rmtree(old_dest)
    build_manifest(jira_dir, component_field)


def push_key(
    jira_dir: Path,
    key: str,
    jira_client: JiraClient,
    *,
    dry_run: bool = False,
    progress: Progress | None = print,
    component_field: str = "components",
) -> str:
    shadow = load_shadow(jira_dir, key)
    if shadow is None:
        return "skipped"

    current_updated = remote_updated(jira_client, key)
    if shadow.get("baseUpdated") != current_updated:
        # The issue changed remotely since this shadow was based -- but that
        # doesn't necessarily conflict with what THIS shadow is pushing (a
        # comment from someone else, a linked dependency, an unrelated field
        # all bump the issue's own `updated` timestamp too). Only block if a
        # field the shadow itself edits also changed remotely; anything else
        # is safe to push as-is and gets reconciled by the post-push refresh
        # below regardless.
        try:
            remote_issue = jira_client.get_issue(key, fields="*all")
        except Exception as exc:
            raise ShadowError(f"could not read remote issue {key} to check for conflicts: {exc}") from exc
        if not isinstance(remote_issue, dict):
            raise ShadowError(f"could not read remote issue {key} to check for conflicts")
        conflicts = conflicting_fields(jira_dir, key, shadow, remote_issue)
        if conflicts:
            if progress:
                progress(f"{key}: blocked, remote changed for {', '.join(conflicts)} since local edits")
            return "blocked"
        if progress:
            progress(f"{key}: remote changed since local edits, but no conflicting fields -- pushing")

    unsupported = unsupported_fields(shadow)
    if unsupported:
        raise ShadowError(
            f"{key} has fields that cannot be pushed yet: {', '.join(unsupported)}. "
            f"Supported push fields: {', '.join(sorted(API_PUSH_FIELDS | TRANSITION_PUSH_FIELDS))}, customfield_*"
        )

    if dry_run:
        if progress:
            progress(f"{key}: would push")
        return "pushed"

    fields = shadow.get("fields", {})
    if isinstance(fields, dict) and fields:
        status = fields.get("status")
        if status:
            try:
                jira_client.issue_transition(key, str(status))
            except Exception as exc:
                raise ShadowError(f"{key}: could not transition to status {status}: {exc}") from exc
            status_change = shadow.get("statusChange", {})
            resolution = status_change.get("resolution") if isinstance(status_change, dict) else None
            if resolution:
                resolution_fields = {"resolution": resolution_payload(jira_client, str(resolution))}
                try:
                    jira_client.update_issue_field(
                        key,
                        resolution_fields,
                        notify_users=False,
                    )
                except Exception as exc:
                    raise jira_update_error(key, resolution_fields, exc) from exc
        update_fields = {
            api_field_name(field): api_field_value(jira_dir, field, value)
            for field, value in fields.items()
            if field != "status" and (field != "parent" or value is not None)
        }
        if fields.get("parent") is None and "parent" in fields:
            jira_client.issue_update(
                key,
                fields={},
                update={"parent": [{"set": {"none": True}}]},
                notify_users=False,
            )
        if update_fields:
            try:
                jira_client.update_issue_field(key, update_fields, notify_users=False)
            except Exception as exc:
                raise jira_update_error(key, update_fields, exc) from exc

    comments = shadow.get("comments", [])
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict) and comment.get("state") != "pushed":
                try:
                    jira_client.issue_add_comment(key, str(comment.get("body", "")))
                except Exception as exc:
                    raise ShadowError(f"{key}: could not add comment: {exc}") from exc

    comment_edits = shadow.get("commentEdits", {})
    if isinstance(comment_edits, dict):
        for comment_id, body in comment_edits.items():
            try:
                jira_client.issue_edit_comment(key, str(comment_id), str(body))
            except Exception as exc:
                raise ShadowError(f"{key}: could not edit comment {comment_id}: {exc}") from exc

    comment_deletes = shadow.get("commentDeletes", [])
    if isinstance(comment_deletes, list):
        for comment_id in comment_deletes:
            try:
                delete_remote_comment(jira_client, key, str(comment_id))
            except Exception as exc:
                raise ShadowError(f"{key}: could not delete comment {comment_id}: {exc}") from exc

    refresh_local_issue_after_push(jira_dir, key, jira_client, component_field)
    delete_shadow(jira_dir, key)
    if progress:
        progress(f"{key}: pushed")
    return "pushed"


def push_shadows(
    jira_dir: Path,
    keys: list[str],
    jira_client: JiraClient,
    *,
    dry_run: bool = False,
    progress: Progress | None = print,
    component_field: str = "components",
) -> PushResult:
    selected, dependencies = order_push_keys(jira_dir, keys or all_shadow_keys(jira_dir))
    selected_set = set(selected)
    pushed = skipped = blocked = failed = 0
    errors: list[str] = []
    results: dict[str, str] = {}
    total = len(selected)
    for index, key in enumerate(selected, start=1):
        unmet = sorted(
            (dependency for dependency in dependencies.get(key, set()) if results.get(dependency) != "pushed"),
            key=issue_key_sort_key,
        )
        if unmet:
            blocked += 1
            results[key] = "blocked"
            if progress:
                progress(f"{key}: blocked, dependency did not push: {', '.join(unmet)}")
            continue
        blocker = unselected_parent_blocker(jira_dir, key, selected_set)
        if blocker:
            blocked += 1
            results[key] = "blocked"
            if progress:
                progress(f"{key}: blocked, {blocker}")
            continue
        if progress:
            progress(f"{key}: pushing ({index}/{total})")
        try:
            result = push_key(
                jira_dir,
                key,
                jira_client,
                dry_run=dry_run,
                progress=progress,
                component_field=component_field,
            )
        except ShadowError as exc:
            failed += 1
            error = str(exc)
            errors.append(error)
            results[key] = "failed"
            if progress:
                progress(f"{key}: failed: {error}")
            continue
        if result == "pushed":
            pushed += 1
        elif result == "blocked":
            blocked += 1
        else:
            skipped += 1
        results[key] = result
    return PushResult(pushed=pushed, skipped=skipped, blocked=blocked, failed=failed, errors=tuple(errors))
