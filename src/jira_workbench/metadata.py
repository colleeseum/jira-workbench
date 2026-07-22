from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .sync import read_json, utc_now, write_json


class JsonRunner(Protocol):
    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        pass


class CommandRunner(JsonRunner, Protocol):
    def run(self, args: list[str], *, allow_failure: bool = False) -> str:
        pass


DEFAULT_METADATA_TTL_SECONDS = 3600
DONE_STATUSES = {"close", "closed", "done", "resolved"}


@dataclass(frozen=True)
class MetadataResult:
    cache: dict[str, Any]
    refreshed: bool
    stale: bool = False
    error: Exception | None = None


class VersionActionResult(dict[str, Any]):
    def __init__(self, cache: dict[str, Any], *, changed: bool, message: str) -> None:
        super().__init__(cache)
        self.changed = changed
        self.message = message


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class JiraApiConfig:
    url: str
    email: str
    api_token: str


class MetadataError(RuntimeError):
    pass


class ProjectMetadataClient(Protocol):
    def project(self, key: str) -> dict[str, Any]:
        pass

    def get_project_versions(self, key: str) -> Any:
        pass

    def add_version(
        self,
        key: str,
        project_id: str,
        version: str,
        *,
        is_archived: bool = False,
        is_released: bool = False,
    ) -> Any:
        pass

    def update_version(
        self,
        version: str,
        name: str | None = None,
        description: str | None = None,
        is_archived: bool | None = None,
        is_released: bool | None = None,
        start_date: str | None = None,
        release_date: str | None = None,
    ) -> Any:
        pass

    def delete_version(
        self,
        version: str,
        moved_fixed: str | None = None,
        move_affected: str | None = None,
    ) -> Any:
        pass

    def get_project_components(self, key: str) -> Any:
        pass

    def create_component(self, component: dict[str, Any]) -> Any:
        pass

    def issue_createmeta(self, project: str, expand: str = "projects.issuetypes.fields") -> dict[str, Any] | None:
        pass

    def add_custom_field_option(self, field_id: str | int, context_id: str | int, options: list[str]) -> Any:
        pass


def jira_api_client(config: JiraApiConfig) -> ProjectMetadataClient:
    try:
        from atlassian import Jira
    except ImportError as exc:
        raise MetadataError(
            "atlassian-python-api is required for Jira admin metadata commands. "
            "Install project dependencies with python -m pip install -e ."
        ) from exc
    return Jira(url=config.url, username=config.email, password=config.api_token, cloud=True)


def check_jira_api_config(
    project: str | None,
    config: JiraApiConfig | None,
    client: ProjectMetadataClient | None = None,
) -> list[DoctorCheck]:
    checks = []
    if config is None:
        return [DoctorCheck("jira api config", False, "jira_url, jira_email, or jira_api_token is missing")]
    checks.append(DoctorCheck("jira_url", config.url.startswith("https://"), redact_url(config.url)))
    checks.append(DoctorCheck("jira_email", "@" in config.email, config.email))
    checks.append(DoctorCheck("jira_api_token", bool(config.api_token.strip()), redacted_token(config.api_token)))
    if not all(check.ok for check in checks):
        return checks
    if project is None:
        checks.append(DoctorCheck("jira api project", False, "project is missing"))
        return checks

    try:
        live_client = client or jira_api_client(config)
        project_metadata = live_client.project(project)
        project_key = project_metadata.get("key") if isinstance(project_metadata, dict) else None
        checks.append(DoctorCheck("jira api project", project_key == project, f"project={project_key}"))
    except Exception as exc:
        checks.append(DoctorCheck("jira api project", False, str(exc) or type(exc).__name__))
        return checks

    try:
        versions = normalize_versions(live_client.get_project_versions(project))
        checks.append(DoctorCheck("jira api versions", True, f"{len(versions)} versions visible"))
    except Exception as exc:
        checks.append(DoctorCheck("jira api versions", False, str(exc) or type(exc).__name__))
    return checks


def check_acli_config(
    project: str | None,
    runner: CommandRunner | None,
) -> list[DoctorCheck]:
    if runner is None:
        return [DoctorCheck("acli config", False, "acli is missing")]
    checks = []
    try:
        status = runner.run(["auth", "status"])
        first_line = status.splitlines()[0] if status else "authenticated"
        checks.append(DoctorCheck("acli auth", True, first_line))
    except Exception as exc:
        checks.append(DoctorCheck("acli auth", False, str(exc) or type(exc).__name__))
        return checks

    if project is None:
        checks.append(DoctorCheck("acli project", False, "project is missing"))
        return checks
    try:
        payload = runner.json(["jira", "project", "view", "--key", project, "--json"])
        project_key = payload.get("key") if isinstance(payload, dict) else None
        checks.append(DoctorCheck("acli project", project_key == project, f"project={project_key}"))
    except Exception as exc:
        checks.append(DoctorCheck("acli project", False, str(exc) or type(exc).__name__))
    return checks


def redacted_token(token: str) -> str:
    stripped = token.strip()
    return f"<set, length {len(stripped)}>" if stripped else "<missing>"


def redact_url(url: str) -> str:
    return url.rstrip("/")


def format_doctor_checks(checks: list[DoctorCheck]) -> str:
    if not checks:
        return "no checks ran\n"
    width = max(len(check.name) for check in checks)
    lines = []
    for check in checks:
        status = "ok" if check.ok else "fail"
        lines.append(f"{status:<4} {check.name:<{width}}  {check.detail}")
    return "\n".join(lines) + "\n"


def versions_path(jira_dir: Path) -> Path:
    return jira_dir / "meta" / "versions.json"


def components_path(jira_dir: Path) -> Path:
    return jira_dir / "meta" / "components.json"


def component_field_options_path(jira_dir: Path, field_id: str) -> Path:
    return jira_dir / "meta" / f"{field_id}-options.json"


def manifest_path(jira_dir: Path) -> Path:
    return jira_dir / "manifest.json"


def normalize_versions(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("values", "versions"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def normalize_components(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("values", "components"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def version_name(version: dict[str, Any]) -> str:
    for key in ("name", "value", "id"):
        value = version.get(key)
        if isinstance(value, str):
            return value
    return ""


def component_name(component: dict[str, Any]) -> str:
    value = component.get("name") or component.get("component") or component.get("value") or component.get("id")
    return value if isinstance(value, str) else ""


def sort_versions(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(versions, key=lambda version: version_name(version).lower())


def sort_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(components, key=lambda component: component_name(component).lower())


def load_versions(jira_dir: Path) -> dict[str, Any] | None:
    path = versions_path(jira_dir)
    if not path.exists():
        return None
    value = read_json(path)
    return value if isinstance(value, dict) else None


def load_components(jira_dir: Path) -> dict[str, Any] | None:
    path = components_path(jira_dir)
    if not path.exists():
        return None
    value = read_json(path)
    return value if isinstance(value, dict) else None


def load_component_field_options(jira_dir: Path, field_id: str) -> dict[str, Any] | None:
    path = component_field_options_path(jira_dir, field_id)
    if not path.exists():
        return None
    value = read_json(path)
    return value if isinstance(value, dict) else None


def refresh_versions(
    jira_dir: Path,
    project: str,
    runner: JsonRunner,
    *,
    allow_failure: bool = False,
) -> dict[str, Any]:
    payload = runner.json(
        ["jira", "project", "view", "--key", project, "--json"],
        allow_failure=allow_failure,
    )
    cache = {
        "project": project,
        "fetchedAt": utc_now(),
        "versions": sort_versions(normalize_versions(payload)),
    }
    write_json(versions_path(jira_dir), cache)
    return cache


def refresh_versions_api(
    jira_dir: Path,
    project: str,
    client: ProjectMetadataClient,
) -> dict[str, Any]:
    try:
        versions = client.get_project_versions(project)
    except Exception as exc:
        raise MetadataError(f"could not refresh Jira versions for {project}: {exc}") from exc
    cache = {
        "project": project,
        "fetchedAt": utc_now(),
        "versions": sort_versions(normalize_versions(versions)),
    }
    write_json(versions_path(jira_dir), cache)
    return cache


def cache_versions(jira_dir: Path, project: str, versions: list[dict[str, Any]]) -> dict[str, Any]:
    cache = {
        "project": project,
        "fetchedAt": utc_now(),
        "versions": sort_versions(versions),
    }
    write_json(versions_path(jira_dir), cache)
    return cache


def refresh_components_api(
    jira_dir: Path,
    project: str,
    client: ProjectMetadataClient,
) -> dict[str, Any]:
    try:
        components = client.get_project_components(project)
    except Exception as exc:
        raise MetadataError(f"could not refresh Jira components for {project}: {exc}") from exc
    cache = {
        "project": project,
        "fetchedAt": utc_now(),
        "components": sort_components(normalize_components(components)),
    }
    write_json(components_path(jira_dir), cache)
    return cache


def component_field_options_from_createmeta(payload: Any, field_id: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    projects = payload.get("projects")
    if not isinstance(projects, list):
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for project in projects:
        if not isinstance(project, dict):
            continue
        issue_types = project.get("issuetypes")
        if not isinstance(issue_types, list):
            continue
        for issue_type in issue_types:
            if not isinstance(issue_type, dict):
                continue
            fields = issue_type.get("fields")
            if not isinstance(fields, dict):
                continue
            field = fields.get(field_id)
            if not isinstance(field, dict):
                continue
            values = field.get("allowedValues")
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                name = component_name(value).strip()
                if not name:
                    continue
                key = str(value.get("id") or name.lower())
                by_id[key] = value
    return sort_components(list(by_id.values()))


def refresh_component_field_options_api(
    jira_dir: Path,
    project: str,
    field_id: str,
    client: ProjectMetadataClient,
) -> dict[str, Any]:
    try:
        metadata = client.issue_createmeta(project)
    except Exception as exc:
        raise MetadataError(f"could not refresh Jira field options for {field_id}: {exc}") from exc
    cache = {
        "project": project,
        "field": field_id,
        "fetchedAt": utc_now(),
        "options": component_field_options_from_createmeta(metadata, field_id),
    }
    write_json(component_field_options_path(jira_dir, field_id), cache)
    return cache


def project_id(client: ProjectMetadataClient, project: str) -> str:
    try:
        value = client.project(project)
    except Exception as exc:
        raise MetadataError(f"could not read Jira project {project}: {exc}") from exc
    if not isinstance(value, dict):
        raise MetadataError(f"project {project} returned unexpected metadata")
    raw_id = value.get("id")
    if isinstance(raw_id, str) and raw_id:
        return raw_id
    raise MetadataError(f"project {project} metadata does not include an id")


def add_version_api(
    jira_dir: Path,
    project: str,
    name: str,
    client: ProjectMetadataClient,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise MetadataError("version name must be non-empty")
    try:
        client.add_version(project, project_id(client, project), clean_name)
    except MetadataError:
        raise
    except Exception as exc:
        raise MetadataError(f"could not create Jira version {clean_name}: {exc}") from exc
    return refresh_versions_api(jira_dir, project, client)


def resolve_version(
    client: ProjectMetadataClient,
    project: str,
    identifier: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    clean_identifier = identifier.strip()
    if not clean_identifier:
        raise MetadataError("version identifier must be non-empty")
    try:
        versions = normalize_versions(client.get_project_versions(project))
    except Exception as exc:
        raise MetadataError(f"could not read Jira versions for {project}: {exc}") from exc

    matches = [
        version
        for version in versions
        if str(version.get("id") or "") == clean_identifier or version_name(version) == clean_identifier
    ]
    if not matches:
        raise MetadataError(f"version {clean_identifier} was not found in project {project}")
    if len(matches) > 1:
        ids = ", ".join(str(version.get("id") or "") for version in matches)
        raise MetadataError(f"version {clean_identifier} matched multiple versions: {ids}")
    return matches[0], versions


def resolve_version_id(client: ProjectMetadataClient, project: str, identifier: str) -> str:
    clean_identifier = identifier.strip()
    version, _ = resolve_version(client, project, clean_identifier)
    version_id = version.get("id")
    if not isinstance(version_id, str) or not version_id:
        raise MetadataError(f"version {clean_identifier} does not include an id")
    return version_id


def rename_version_api(
    jira_dir: Path,
    project: str,
    identifier: str,
    new_name: str,
    client: ProjectMetadataClient,
) -> dict[str, Any]:
    clean_name = new_name.strip()
    if not clean_name:
        raise MetadataError("new version name must be non-empty")
    version_id = resolve_version_id(client, project, identifier)
    try:
        client.update_version(version_id, name=clean_name)
    except Exception as exc:
        raise MetadataError(f"could not rename Jira version {identifier}: {exc}") from exc
    return refresh_versions_api(jira_dir, project, client)


def release_version_api(
    jira_dir: Path,
    project: str,
    identifier: str,
    client: ProjectMetadataClient,
    *,
    release_date: str | None = None,
) -> dict[str, Any]:
    version, versions = resolve_version(client, project, identifier)
    version_id = version.get("id")
    if not isinstance(version_id, str) or not version_id:
        raise MetadataError(f"version {identifier.strip()} does not include an id")
    if version.get("released"):
        name = version_name(version) or identifier
        cache = cache_versions(jira_dir, project, versions)
        return VersionActionResult(cache, changed=False, message=f"version {name} is already released")
    try:
        client.update_version(version_id, is_released=True, release_date=release_date)
    except Exception as exc:
        raise MetadataError(f"could not release Jira version {identifier}: {exc}") from exc
    cache = refresh_versions_api(jira_dir, project, client)
    return VersionActionResult(cache, changed=True, message=f"released version {identifier}")


def archive_version_api(
    jira_dir: Path,
    project: str,
    identifier: str,
    client: ProjectMetadataClient,
) -> dict[str, Any]:
    version, versions = resolve_version(client, project, identifier)
    version_id = version.get("id")
    if not isinstance(version_id, str) or not version_id:
        raise MetadataError(f"version {identifier.strip()} does not include an id")
    if version.get("archived"):
        name = version_name(version) or identifier
        cache = cache_versions(jira_dir, project, versions)
        return VersionActionResult(cache, changed=False, message=f"version {name} is already archived")
    try:
        client.update_version(version_id, is_archived=True)
    except Exception as exc:
        raise MetadataError(f"could not archive Jira version {identifier}: {exc}") from exc
    cache = refresh_versions_api(jira_dir, project, client)
    return VersionActionResult(cache, changed=True, message=f"archived version {identifier}")


def delete_version_api(
    jira_dir: Path,
    project: str,
    identifier: str,
    client: ProjectMetadataClient,
    *,
    move_fix_to: str | None = None,
    move_affected_to: str | None = None,
) -> dict[str, Any]:
    version_id = resolve_version_id(client, project, identifier)
    moved_fixed = resolve_version_id(client, project, move_fix_to) if move_fix_to else None
    move_affected = resolve_version_id(client, project, move_affected_to) if move_affected_to else None
    try:
        client.delete_version(version_id, moved_fixed=moved_fixed, move_affected=move_affected)
    except Exception as exc:
        raise MetadataError(f"could not delete Jira version {identifier}: {exc}") from exc
    return refresh_versions_api(jira_dir, project, client)


def add_component_api(
    jira_dir: Path,
    project: str,
    name: str,
    client: ProjectMetadataClient,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise MetadataError("component name must be non-empty")
    try:
        client.create_component({"name": clean_name, "project": project})
    except Exception as exc:
        raise MetadataError(f"could not create Jira component {clean_name}: {exc}") from exc
    return refresh_components_api(jira_dir, project, client)


def add_component_field_option_api(
    jira_dir: Path,
    project: str,
    field_id: str,
    context_id: str | None,
    name: str,
    client: ProjectMetadataClient,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise MetadataError("component field option name must be non-empty")
    cache = refresh_component_field_options_api(jira_dir, project, field_id, client)
    existing = {component_name(option).strip().lower() for option in normalize_components(cache.get("options"))}
    if clean_name.lower() in existing:
        return cache
    if not context_id:
        raise MetadataError(
            f"cannot add option {clean_name} to {field_id}: missing context id. "
            "Pass --context-id, or ask a Jira administrator for the custom field context id."
        )
    try:
        client.add_custom_field_option(field_id, context_id, [clean_name])
    except Exception as exc:
        raise MetadataError(f"could not add option {clean_name} to {field_id}: {exc}") from exc
    refreshed = refresh_component_field_options_api(jira_dir, project, field_id, client)
    refreshed_names = {
        component_name(option).strip().lower()
        for option in normalize_components(refreshed.get("options"))
    }
    if clean_name.lower() not in refreshed_names:
        raise MetadataError(
            f"added option {clean_name} to {field_id} context {context_id}, but it is not available "
            f"for project {project}. The context id may not apply to this project or issue type."
        )
    return refreshed


def cache_age_seconds(cache: dict[str, Any], now: datetime | None = None) -> float | None:
    fetched_at = cache.get("fetchedAt")
    if not isinstance(fetched_at, str):
        return None
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now or datetime.now(UTC)
    return (current - fetched).total_seconds()


def cache_is_fresh(cache: dict[str, Any], project: str, max_age_seconds: int) -> bool:
    if cache.get("project") != project:
        return False
    age = cache_age_seconds(cache)
    return age is not None and age <= max_age_seconds


def ensure_versions(
    jira_dir: Path,
    project: str,
    runner: JsonRunner,
    *,
    max_age_seconds: int = DEFAULT_METADATA_TTL_SECONDS,
) -> MetadataResult:
    cache = load_versions(jira_dir)
    if cache is not None and cache_is_fresh(cache, project, max_age_seconds):
        return MetadataResult(cache=cache, refreshed=False)
    try:
        return MetadataResult(cache=refresh_versions(jira_dir, project, runner), refreshed=True)
    except Exception as exc:
        if cache is not None:
            return MetadataResult(cache=cache, refreshed=False, stale=True, error=exc)
        raise


def format_versions(cache: dict[str, Any]) -> str:
    versions = normalize_versions(cache.get("versions"))
    if not versions:
        return "no versions found\n"

    names = [version_name(version) for version in versions]
    released = ["released" if version.get("released") else "unreleased" for version in versions]
    archived = ["archived" if version.get("archived") else "" for version in versions]
    width = max(len("name"), *(len(name) for name in names))
    rows = [f"{'name':<{width}}  released    archived"]
    for version, name, released_value, archived_value in zip(versions, names, released, archived, strict=True):
        row_name = name or str(version.get("id") or "")
        rows.append(f"{row_name:<{width}}  {released_value:<10}  {archived_value}")
    return "\n".join(rows) + "\n"


def format_component_cache(cache: dict[str, Any]) -> str:
    components = normalize_components(cache.get("components"))
    return format_components(components)


def format_component_field_option_cache(cache: dict[str, Any]) -> str:
    options = normalize_components(cache.get("options"))
    return format_components(options)


def merge_components(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for components in sources:
        for component in components:
            name = component_name(component).strip()
            if not name:
                continue
            key = name.lower()
            if key not in merged:
                merged[key] = {"name": name}
            merged[key].update({field: value for field, value in component.items() if value not in (None, "")})
            if "component" in component and "name" not in component:
                merged[key]["name"] = name
    return sort_components(list(merged.values()))


def load_component_summary(jira_dir: Path) -> list[dict[str, Any]]:
    path = manifest_path(jira_dir)
    if not path.exists():
        return []
    value = read_json(path)
    if not isinstance(value, dict):
        return []
    components = value.get("components")
    if not isinstance(components, list):
        return []
    summaries = [component for component in components if isinstance(component, dict)]
    work_items = value.get("workItems")
    if not isinstance(work_items, list):
        return summaries
    by_component: dict[str, dict[str, int]] = {}
    for item in work_items:
        if not isinstance(item, dict):
            continue
        component = str(item.get("component") or "").strip()
        if not component:
            continue
        counts = by_component.setdefault(component.lower(), {"active": 0, "total": 0})
        counts["total"] += 1
        status = str(item.get("status") or "").strip().lower()
        if status not in DONE_STATUSES:
            counts["active"] += 1
    enriched = []
    for component in summaries:
        name = component_name(component).strip()
        counts = by_component.get(name.lower())
        if counts is None:
            enriched.append(component)
            continue
        enriched.append({**component, "active": counts["active"], "total": counts["total"]})
    return enriched


def format_components(components: list[dict[str, Any]]) -> str:
    if not components:
        return "no components found\n"

    def column_width(header: str, values: list[object]) -> int:
        return max(len(header), *(len(str(value)) for value in values))

    def total_value(component: dict[str, Any]) -> object:
        if component.get("total") is not None:
            return component.get("total")
        if component.get("count") is not None:
            return component.get("count")
        return ""

    names = [component_name(component) for component in components]
    width = max(len("component"), *(len(name) for name in names))
    show_id = any(component.get("id") for component in components)
    show_total = any(component.get("total") is not None or component.get("count") is not None for component in components)
    show_active = any(component.get("active") is not None for component in components)
    id_width = column_width("id", [component.get("id") or "" for component in components]) if show_id else 0
    active_width = column_width(
        "active",
        [component.get("active") if component.get("active") is not None else "" for component in components],
    ) if show_active else 0
    total_width = column_width("total", [total_value(component) for component in components]) if show_total else 0
    headings = [f"{'component':<{width}}"]
    if show_id:
        headings.append(f"{'id':>{id_width}}")
    if show_active:
        headings.append(f"{'active':>{active_width}}")
    if show_total:
        headings.append(f"{'total':>{total_width}}")
    rows = ["  ".join(headings)]
    for component, name in zip(components, names, strict=True):
        cells = [f"{name:<{width}}"]
        if show_id:
            cells.append(f"{str(component.get('id') or ''):>{id_width}}")
        if show_active:
            active = component.get("active") if component.get("active") is not None else ""
            cells.append(f"{str(active):>{active_width}}")
        if show_total:
            total = total_value(component)
            cells.append(f"{str(total):>{total_width}}")
        rows.append("  ".join(cells))
    return "\n".join(rows) + "\n"
