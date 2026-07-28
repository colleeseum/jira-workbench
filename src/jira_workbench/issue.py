from __future__ import annotations

from typing import Any


class IssueError(RuntimeError):
    pass


def fetch_issue_type_fields(client: Any, project: str) -> dict[str, dict[str, object]]:
    """One issue_createmeta call -> {issue_type_name: create_fields_dict} for
    every creatable issue type in the project."""
    try:
        metadata = client.issue_createmeta(project)
    except Exception as exc:
        raise IssueError(f"could not read Jira create metadata for {project}: {exc}") from exc
    projects = metadata.get("projects") if isinstance(metadata, dict) else None
    if not isinstance(projects, list):
        raise IssueError(f"Jira create metadata for {project} did not include projects")
    result: dict[str, dict[str, object]] = {}
    for project_meta in projects:
        if not isinstance(project_meta, dict):
            continue
        issue_types = project_meta.get("issuetypes")
        if not isinstance(issue_types, list):
            continue
        for item in issue_types:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            fields = item.get("fields")
            if isinstance(name, str) and isinstance(fields, dict):
                result[name] = fields
    return result


def fetch_issue_edit_fields(client: Any, key: str) -> dict[str, dict[str, object]]:
    """One issue_editmeta call -> {field_id: field_metadata} for this issue's
    own edit screen -- the existing-issue counterpart to
    fetch_issue_type_fields's create-screen metadata."""
    try:
        metadata = client.issue_editmeta(key)
    except Exception as exc:
        raise IssueError(f"could not read Jira edit metadata for {key}: {exc}") from exc
    fields = metadata.get("fields") if isinstance(metadata, dict) else None
    if not isinstance(fields, dict):
        raise IssueError(f"Jira edit metadata for {key} did not include fields")
    return fields


def fetch_all_field_names(client: Any) -> dict[str, str]:
    """All Jira field id -> display name pairs (system and custom), e.g.
    "customfield_10082" -> "Customers SAT" -- one cheap, instance-wide call
    that covers every custom field regardless of which issue/type you're
    viewing, unlike issue_editmeta (scoped to one issue's own edit screen)."""
    try:
        fields = client.get_all_fields()
    except Exception as exc:
        raise IssueError(f"could not read Jira field list: {exc}") from exc
    if not isinstance(fields, list):
        raise IssueError("Jira field list response was not a list")
    return {
        field["id"]: field["name"]
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("id"), str) and isinstance(field.get("name"), str)
    }


def fetch_current_user(client: Any) -> dict[str, object]:
    try:
        myself = client.myself()
    except Exception as exc:
        raise IssueError(f"could not resolve current Jira user: {exc}") from exc
    if not isinstance(myself, dict) or not myself.get("accountId"):
        raise IssueError("current Jira user response did not include accountId")
    return myself


def resolve_reporter(client: Any) -> dict[str, object]:
    return {"accountId": fetch_current_user(client)["accountId"]}


def issue_component_value(component_field: str, component: str) -> object:
    if component_field == "components":
        return [{"name": component}]
    return {"value": component}


def build_create_fields(
    *,
    project: str,
    issue_type: str,
    summary: str,
    description: str,
    component_field: str,
    component: str | None,
    parent: str | None,
    priority: str | None,
    fix_version: str | None = None,
    labels: list[str] | None = None,
    assignee: dict[str, object] | None = None,
    create_fields: dict[str, object],
    reporter: dict[str, object] | None,
) -> dict[str, object]:
    fields: dict[str, object] = {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
        "summary": summary,
    }
    if description:
        fields["description"] = description
    if reporter is not None and "reporter" in create_fields:
        fields["reporter"] = reporter
    if parent:
        if "parent" not in create_fields:
            raise IssueError(f"issue type {issue_type} cannot set parent during create")
        fields["parent"] = {"key": parent}
    if priority:
        if "priority" not in create_fields:
            raise IssueError(f"issue type {issue_type} cannot set priority during create")
        fields["priority"] = {"name": priority}
    if component:
        if component_field not in create_fields:
            raise IssueError(f"issue type {issue_type} cannot set component field {component_field} during create")
        fields[component_field] = issue_component_value(component_field, component)
    if fix_version:
        if "fixVersions" not in create_fields:
            raise IssueError(f"issue type {issue_type} cannot set fix version during create")
        fields["fixVersions"] = [{"name": fix_version}]
    if labels:
        if "labels" not in create_fields:
            raise IssueError(f"issue type {issue_type} cannot set labels during create")
        fields["labels"] = labels
    if assignee is not None:
        if "assignee" not in create_fields:
            raise IssueError(f"issue type {issue_type} cannot set assignee during create")
        fields["assignee"] = assignee
    return fields


def create_issue(client: Any, fields: dict[str, object]) -> str:
    try:
        created = client.issue_create(fields)
    except Exception as exc:
        summary = fields.get("summary")
        raise IssueError(f"could not create Jira issue {summary}: {exc}") from exc
    key = created.get("key") if isinstance(created, dict) else None
    if not key:
        raise IssueError("Jira create response did not include key")
    return str(key)
