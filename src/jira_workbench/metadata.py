from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .config import ProjectSettings
from .jql import compile_jql, scope_predicate_to_project
from .sync import FALLBACK_DONE_STATUS_NAMES, bump_manifest_generation, read_json, utc_now, write_json


DEFAULT_METADATA_TTL_SECONDS = 3600


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

    def get_all_agile_boards(self, project_key: str | None = None) -> Any:
        pass

    def get_agile_board_configuration(self, board_id: object) -> Any:
        pass

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
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


def versions_path(jira_dir: Path, project: str) -> Path:
    return jira_dir / "meta" / project / "versions.json"


def components_path(jira_dir: Path, project: str) -> Path:
    return jira_dir / "meta" / project / "components.json"


def boards_path(jira_dir: Path, project: str) -> Path:
    return jira_dir / "meta" / project / "boards.json"


def assignees_path(jira_dir: Path, project: str) -> Path:
    return jira_dir / "meta" / project / "assignees.json"


def component_field_options_path(jira_dir: Path, project: str, field_id: str) -> Path:
    return jira_dir / "meta" / project / f"{field_id}-options.json"


def field_names_path(jira_dir: Path) -> Path:
    return jira_dir / "meta" / "field-names.json"


def project_registry_path(jira_dir: Path) -> Path:
    return jira_dir / "meta" / "projects.json"


def board_settings_path(jira_dir: Path) -> Path:
    return jira_dir / "meta" / "board_settings.json"


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


def normalize_boards(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("values", "boards"):
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


def sort_assignees(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(users, key=lambda user: str(user.get("displayName") or "").lower())


def load_versions(jira_dir: Path, project: str) -> dict[str, Any] | None:
    path = versions_path(jira_dir, project)
    if not path.exists():
        return None
    value = read_json(path)
    return value if isinstance(value, dict) else None


def load_components(jira_dir: Path, project: str) -> dict[str, Any] | None:
    path = components_path(jira_dir, project)
    if not path.exists():
        return None
    value = read_json(path)
    return value if isinstance(value, dict) else None


def load_boards(jira_dir: Path, project: str) -> dict[str, Any] | None:
    path = boards_path(jira_dir, project)
    if not path.exists():
        return None
    value = read_json(path)
    return value if isinstance(value, dict) else None


def load_assignees(jira_dir: Path, project: str) -> dict[str, Any] | None:
    path = assignees_path(jira_dir, project)
    if not path.exists():
        return None
    value = read_json(path)
    return value if isinstance(value, dict) else None


def load_component_field_options(jira_dir: Path, project: str, field_id: str) -> dict[str, Any] | None:
    path = component_field_options_path(jira_dir, project, field_id)
    if not path.exists():
        return None
    value = read_json(path)
    return value if isinstance(value, dict) else None


def load_all_versions(jira_dir: Path) -> list[dict[str, Any]]:
    """Every synced project's cached versions, merged and deduped by id.

    Version ids are unique across a whole Jira instance, not scoped per
    project, so merging them all together is always safe/correct -- this is
    what lets view.py's per-item lookups (fix-version name resolution, the
    version picker) stay project-agnostic even though the caches themselves
    are now namespaced per project.
    """
    meta_dir = jira_dir / "meta"
    if not meta_dir.is_dir():
        return []
    merged: dict[str, dict[str, Any]] = {}
    unidentified: list[dict[str, Any]] = []
    for project_dir in sorted(meta_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        cache = load_versions(jira_dir, project_dir.name)
        if cache is None:
            continue
        for version in normalize_versions(cache.get("versions")):
            version_id = version.get("id")
            if isinstance(version_id, str) and version_id:
                merged[version_id] = version
            else:
                # No id to dedupe by (shouldn't happen for real Jira data,
                # but test fixtures and hand-authored caches sometimes omit
                # it) -- still include it rather than silently dropping it.
                unidentified.append(version)
    return sort_versions([*merged.values(), *unidentified])


def load_all_components(jira_dir: Path) -> list[dict[str, Any]]:
    """Every synced project's cached native components, merged.

    Unlike versions/boards, components have no globally-unique id to dedupe
    by, so this reuses merge_components' existing name-based merge instead.
    """
    meta_dir = jira_dir / "meta"
    if not meta_dir.is_dir():
        return []
    sources: list[list[dict[str, Any]]] = []
    for project_dir in sorted(meta_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        cache = load_components(jira_dir, project_dir.name)
        if cache is not None:
            sources.append(normalize_components(cache.get("components")))
    return merge_components(*sources) if sources else []


def load_all_boards(jira_dir: Path) -> list[dict[str, Any]]:
    """Every synced project's cached boards, merged and deduped by id (see
    load_all_versions -- board ids are likewise instance-wide, not
    per-project)."""
    meta_dir = jira_dir / "meta"
    if not meta_dir.is_dir():
        return []
    merged: dict[object, dict[str, Any]] = {}
    unidentified: list[dict[str, Any]] = []
    for project_dir in sorted(meta_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        cache = load_boards(jira_dir, project_dir.name)
        if cache is None:
            continue
        for board in normalize_boards(cache.get("boards")):
            board_id = board.get("id")
            if board_id is not None:
                merged[board_id] = board
            else:
                unidentified.append(board)
    return [*merged.values(), *unidentified]


def load_field_names(jira_dir: Path) -> dict[str, str]:
    """Locally-cached field id -> display name, e.g. "customfield_10082" ->
    "Customers SAT" -- built up opportunistically (see remember_field_names)
    from whatever live editmeta/createmeta fetches already happen elsewhere,
    never its own API call, so this stays available offline once discovered."""
    path = field_names_path(jira_dir)
    if not path.exists():
        return {}
    value = read_json(path)
    names = value.get("names") if isinstance(value, dict) else None
    if not isinstance(names, dict):
        return {}
    return {key: name for key, name in names.items() if isinstance(key, str) and isinstance(name, str)}


def remember_field_names(jira_dir: Path, names: dict[str, str]) -> None:
    if not names:
        return
    existing = load_field_names(jira_dir)
    existing.update(names)
    write_json(field_names_path(jira_dir), {"names": existing})


def write_project_registry(jira_dir: Path, projects: tuple[ProjectSettings, ...]) -> None:
    """Snapshot the config's [[projects]] declarations (default/read-only)
    into jira_dir itself. Unlike every other meta/*.json cache, this isn't
    data fetched from Jira -- it's local config, mirrored here so shadow.py
    can enforce read-only status from just a jira_dir, the one thing it
    already has at every call site, instead of threading a new parameter
    through every set_field/push_key caller across the CLI and TUI."""
    write_json(
        project_registry_path(jira_dir),
        {"projects": {p.key: {"readOnly": p.read_only, "default": p.default} for p in projects}},
    )


def seed_project_registry(jira_dir: Path, projects: tuple[ProjectSettings, ...]) -> None:
    """Fill in a registry entry for any project config.toml declares that the
    registry has never recorded at all, so a freshly added `read_only = true`
    takes effect on the very next command instead of staying "fully permissive"
    (see load_project_registry) until someone happens to run a full `sync`.

    Deliberately additive-only: a project already present in the registry --
    however it got there -- is left untouched, even if this config disagrees
    (e.g. a `--config` pointed at a file without read_only info). Only
    write_project_registry's full overwrite, from `sync`, may change or
    remove an existing entry; this only ever adds ones that were missing."""
    existing = load_project_registry(jira_dir)
    missing = {p.key: p for p in projects if p.key not in existing}
    if not missing:
        return
    merged = {**existing, **{key: {"readOnly": p.read_only, "default": p.default} for key, p in missing.items()}}
    write_json(project_registry_path(jira_dir), {"projects": merged})


def load_project_registry(jira_dir: Path) -> dict[str, dict[str, bool]]:
    # Missing/malformed -> {} (fully permissive -- nothing read-only, same
    # as a jira_dir synced before this feature existed, or one that's never
    # had `jira-wb sync` write this file yet).
    path = project_registry_path(jira_dir)
    if not path.exists():
        return {}
    value = read_json(path)
    projects = value.get("projects") if isinstance(value, dict) else None
    if not isinstance(projects, dict):
        return {}
    result: dict[str, dict[str, bool]] = {}
    for key, entry in projects.items():
        if isinstance(key, str) and isinstance(entry, dict):
            result[key] = {
                "readOnly": bool(entry.get("readOnly")),
                "default": bool(entry.get("default")),
            }
    return result


def is_project_read_only(jira_dir: Path, project_key: str | None) -> bool:
    if not project_key:
        return False
    return bool(load_project_registry(jira_dir).get(project_key, {}).get("readOnly"))


def load_board_settings(jira_dir: Path) -> dict[str, Any]:
    """Local, user-owned board data -- bespoke "local" board definitions
    (name/fieldFilters/pattern, matched the same way filter_items matches
    any other item, no JQL involved) plus which Jira board ids the user has
    toggled off in the picker. Unlike every other meta/*.json cache, none of
    this is fetched from Jira -- it's never touched by a sync/refresh."""
    path = board_settings_path(jira_dir)
    default: dict[str, Any] = {"localBoards": [], "disabledBoardIds": []}
    if not path.exists():
        return default
    value = read_json(path)
    if not isinstance(value, dict):
        return default
    local_boards = value.get("localBoards")
    disabled_ids = value.get("disabledBoardIds")
    return {
        "localBoards": [board for board in local_boards if isinstance(board, dict)]
        if isinstance(local_boards, list)
        else [],
        "disabledBoardIds": [str(item) for item in disabled_ids if isinstance(item, (str, int))]
        if isinstance(disabled_ids, list)
        else [],
    }


def write_board_settings(jira_dir: Path, settings: dict[str, Any]) -> None:
    write_json(board_settings_path(jira_dir), settings)


def _all_board_names(jira_dir: Path, settings: dict[str, Any]) -> set[str]:
    names = {str(board.get("name") or "").strip().lower() for board in load_all_boards(jira_dir)}
    names.update(str(board.get("name") or "").strip().lower() for board in settings["localBoards"])
    names.discard("")
    return names


def add_local_board(
    jira_dir: Path,
    name: str,
    field_filters: dict[str, list[str]],
    pattern: str | None = None,
    active_filter: dict[str, Any] | None = None,
) -> None:
    clean_name = name.strip()
    if not clean_name:
        raise MetadataError("board name must be non-empty")
    settings = load_board_settings(jira_dir)
    if clean_name.lower() in _all_board_names(jira_dir, settings):
        raise MetadataError(f"a board named {clean_name} already exists")
    settings["localBoards"].append(
        {
            "name": clean_name,
            "active": True,
            "fieldFilters": dict(field_filters),
            "pattern": pattern,
            "activeFilter": active_filter,
        }
    )
    write_board_settings(jira_dir, settings)


def rename_local_board(jira_dir: Path, name: str, new_name: str) -> None:
    clean_new = new_name.strip()
    if not clean_new:
        raise MetadataError("board name must be non-empty")
    settings = load_board_settings(jira_dir)
    board = next((b for b in settings["localBoards"] if b.get("name") == name), None)
    if board is None:
        raise MetadataError(f"local board {name} not found")
    if clean_new.lower() != name.strip().lower() and clean_new.lower() in _all_board_names(jira_dir, settings):
        raise MetadataError(f"a board named {clean_new} already exists")
    board["name"] = clean_new
    write_board_settings(jira_dir, settings)


def set_local_board_filters(
    jira_dir: Path,
    name: str,
    field_filters: dict[str, list[str]],
    pattern: str | None,
    active_filter: dict[str, Any] | None = None,
) -> None:
    settings = load_board_settings(jira_dir)
    board = next((b for b in settings["localBoards"] if b.get("name") == name), None)
    if board is None:
        raise MetadataError(f"local board {name} not found")
    board["fieldFilters"] = dict(field_filters)
    board["pattern"] = pattern
    board["activeFilter"] = active_filter
    write_board_settings(jira_dir, settings)


def delete_local_board(jira_dir: Path, name: str) -> None:
    settings = load_board_settings(jira_dir)
    remaining = [b for b in settings["localBoards"] if b.get("name") != name]
    if len(remaining) == len(settings["localBoards"]):
        raise MetadataError(f"local board {name} not found")
    settings["localBoards"] = remaining
    write_board_settings(jira_dir, settings)


def set_board_active(jira_dir: Path, kind: str, identifier: str, active: bool) -> None:
    """Toggle the local, purely-cosmetic "shown in the board picker" switch
    -- identifier is a board id for kind="jira" (stable across renames) or
    a board name for kind="local" (which has no separate id). Never affects
    matching for a board that's already selected -- only the Filters
    screen's picker options are filtered by this."""
    identifier = str(identifier)
    settings = load_board_settings(jira_dir)
    if kind == "local":
        board = next((b for b in settings["localBoards"] if b.get("name") == identifier), None)
        if board is None:
            raise MetadataError(f"local board {identifier} not found")
        board["active"] = active
    else:
        disabled = set(settings["disabledBoardIds"])
        if active:
            disabled.discard(identifier)
        else:
            disabled.add(identifier)
        settings["disabledBoardIds"] = sorted(disabled)
    write_board_settings(jira_dir, settings)


def load_all_boards_with_settings(jira_dir: Path) -> list[dict[str, Any]]:
    """Every synced project's Jira boards (see load_all_boards) plus every
    locally-defined board, tagged with kind ("jira"/"local") and active (a
    purely local on/off switch -- independent of whether the board's own
    filter is supported, see its unsupportedReason). This is the one list
    both the Meta > Boards screen and the Filters picker consume."""
    settings = load_board_settings(jira_dir)
    disabled_ids = set(settings["disabledBoardIds"])
    jira_boards = [
        {**board, "kind": "jira", "active": str(board.get("id")) not in disabled_ids}
        for board in load_all_boards(jira_dir)
    ]
    local_boards = [{**board, "kind": "local"} for board in settings["localBoards"]]
    return [*jira_boards, *local_boards]


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
    write_json(versions_path(jira_dir, project), cache)
    return cache


def cache_versions(jira_dir: Path, project: str, versions: list[dict[str, Any]]) -> dict[str, Any]:
    cache = {
        "project": project,
        "fetchedAt": utc_now(),
        "versions": sort_versions(versions),
    }
    write_json(versions_path(jira_dir, project), cache)
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
    write_json(components_path(jira_dir, project), cache)
    return cache


_PROJECT_ACCESS_ROLE_NAMES = {"administrator", "member"}


def fetch_project_access_members(client: Any, project_key: str) -> list[dict[str, Any]]:
    """Everyone with real, current access to this project -- the union of
    its Administrator and Member roles, the same roster Jira's own Project
    Settings > Access page shows (.../jira/software/projects/{key}/settings
    /access). Deliberately NOT Jira's generic "assignable users" search
    (rest/api/2/user/assignable/search): confirmed live against a real
    instance that it still returns someone who's been removed from the
    project's own Access list, as long as they retain assign permission
    through some other, broader grant -- Access-role membership is what
    actually tracks "is this still someone I work with on this project"
    day to day. The Viewer role (can't be assigned issues) and the
    addon/service system roles (atlassian-addons-project-access,
    jira-guest-member) are excluded on purpose."""
    try:
        roles = client.get_project_roles(project_key)
    except Exception as exc:
        raise MetadataError(f"could not list project roles for {project_key}: {exc}") from exc
    if not isinstance(roles, dict):
        return []
    users_by_id: dict[str, dict[str, Any]] = {}
    for role_name, role_url in roles.items():
        if str(role_name).strip().lower() not in _PROJECT_ACCESS_ROLE_NAMES:
            continue
        role_id = str(role_url).rstrip("/").rsplit("/", 1)[-1]
        try:
            role_detail = client.get_project_actors_for_role_project(project_key, role_id)
        except Exception as exc:
            raise MetadataError(f"could not list actors for role {role_name} on {project_key}: {exc}") from exc
        actors = role_detail.get("actors") if isinstance(role_detail, dict) else role_detail
        if not isinstance(actors, list):
            continue
        for actor in actors:
            if not isinstance(actor, dict) or actor.get("type") != "atlassian-user-role-actor":
                continue
            display_name = actor.get("displayName")
            if not isinstance(display_name, str) or not display_name:
                continue
            actor_user = actor.get("actorUser")
            account_id = actor_user.get("accountId") if isinstance(actor_user, dict) else None
            users_by_id[str(account_id or display_name)] = {"accountId": account_id, "displayName": display_name}
    return list(users_by_id.values())


_PROJECT_ACTORS_QUERY = """query ProjectActorsQuery($filter: ProjectActorInputs, $projectId: Long!) {
  projectActors(filter: $filter, projectId: $projectId) {
    actors {
      email
      avatarUrl
      active
      accountId
      roleTypeId
      type
      displayName
      roles
      isGuest
      __typename
    }
    isLastBatch
    __typename
  }
  projectActorsLimits(projectId: $projectId) {
    limit
    totalCount
    __typename
  }
}"""


def fetch_project_actors_internal(client: Any, project_id: object) -> list[dict[str, Any]]:
    """The exact internal GraphQL query Jira's own web UI issues for a
    team-managed project's Settings > Access page (POST rest/gira/1/
    ?operation=ProjectActorsQuery) -- reverse-engineered from that page's
    own network traffic. Confirmed against a real instance to be the only
    source whose "active" flag actually tracks current Access-page
    membership: both the classic project-role actors API
    (fetch_project_access_members) and Jira's public "assignable users"
    search kept including people well after they'd been removed from the
    project, apparently because neither correctly reflects a site-
    deactivated account the way this internal query does.

    UNDOCUMENTED and unsupported -- not part of the Jira REST v3 contract,
    so Atlassian can change or remove it without notice. Callers should
    treat any failure here as expected and fall back to the public,
    supported role-based API (see refresh_assignees_api)."""
    actors: list[dict[str, Any]] = []
    page_number = 1
    while True:
        body = {
            "operationName": "ProjectActorsQuery",
            "variables": {
                "projectId": project_id,
                "filter": {
                    "page": {"number": page_number, "size": 100},
                    "orderBy": {"field": "NAME", "direction": "ASC"},
                    "roles": {"ids": [], "actorTypes": []},
                },
            },
            "query": _PROJECT_ACTORS_QUERY,
        }
        payload = client.post("rest/gira/1/", json=body, params={"operation": "ProjectActorsQuery"})
        data = payload.get("data") if isinstance(payload, dict) else None
        project_actors = data.get("projectActors") if isinstance(data, dict) else None
        page_actors = project_actors.get("actors") if isinstance(project_actors, dict) else None
        if not isinstance(page_actors, list) or not page_actors:
            break
        actors.extend(actor for actor in page_actors if isinstance(actor, dict))
        if project_actors.get("isLastBatch"):
            break
        page_number += 1
    return actors


def refresh_assignees_api(
    jira_dir: Path,
    project: str,
    client: Any,
) -> dict[str, Any]:
    try:
        project_id = client.project(project).get("id")
        actors = fetch_project_actors_internal(client, project_id)
        users = [
            {"accountId": actor.get("accountId"), "displayName": actor.get("displayName")}
            for actor in actors
            if actor.get("active") is True and isinstance(actor.get("displayName"), str) and actor.get("displayName")
        ]
        source = "internal-access-api"
    except Exception:
        # The internal query is undocumented -- Atlassian can change or
        # remove it without notice. Degrading to the classic, public
        # role-based API keeps the refresh functional rather than failing
        # outright, but that source is known to be less accurate (see
        # fetch_project_access_members's own docstring: it can still
        # include someone who's actually stopped working on the project).
        # "source" is recorded in the cache itself, not just logged at
        # refresh time, so a stale degraded cache stays visible to anyone
        # inspecting it later, not only to whoever happened to be watching
        # the terminal when the refresh ran.
        users = fetch_project_access_members(client, project)
        source = "role-api-fallback"
    cache = {
        "project": project,
        "fetchedAt": utc_now(),
        "assignees": sort_assignees(users),
        "source": source,
    }
    write_json(assignees_path(jira_dir, project), cache)
    return cache


def field_clause_names(client: ProjectMetadataClient, field_id: str) -> list[str]:
    """Every name usable in a JQL clause for this field.

    A field's plain "name" (what's shown in the UI) is not necessarily what
    JQL accepts -- Jira disambiguates same-named fields by giving each a
    distinct "clauseNames" alias (e.g. a custom field named "Components"
    might only be queryable as "Components[Dropdown]"), so board filters
    must be matched against clauseNames, not name.
    """
    if field_id == "components":
        # The native multi-value Components field is queried in JQL as the
        # singular "component", unlike custom fields which use clauseNames.
        return ["component"]
    try:
        fields = client.get("rest/api/2/field")
    except Exception as exc:
        raise MetadataError(f"could not read Jira field metadata: {exc}") from exc
    if isinstance(fields, list):
        for entry in fields:
            if isinstance(entry, dict) and entry.get("id") == field_id:
                clause_names = entry.get("clauseNames")
                if isinstance(clause_names, list) and clause_names:
                    return [str(name) for name in clause_names]
                name = entry.get("name")
                if isinstance(name, str) and name:
                    return [name]
    return [field_id]


def fetch_backlog_keys(client: ProjectMetadataClient, board_id: object) -> list[str]:
    keys: list[str] = []
    start = 0
    while True:
        page = client.get(
            f"rest/agile/1.0/board/{board_id}/backlog",
            params={"startAt": start, "maxResults": 100, "fields": "key"},
        )
        if not isinstance(page, dict):
            break
        issues = page.get("issues")
        if not isinstance(issues, list):
            break
        keys.extend(issue["key"] for issue in issues if isinstance(issue, dict) and isinstance(issue.get("key"), str))
        total = page.get("total")
        if not isinstance(total, int) or start + 100 >= total:
            break
        start += 100
    return keys


def fetch_board_keys(client: Any, board_id: object) -> list[str]:
    """Same shape as fetch_backlog_keys, but against the board's own issue
    list (GET board/{id}/issue) rather than its backlog -- Jira returns both
    already ordered by rank, so this doubles as the "active" section's
    display order the same way fetch_backlog_keys's return value already is
    for "backlog" (see with_local_index_fields, view.py), even though
    nothing used that ordering property until now."""
    keys: list[str] = []
    start = 0
    while True:
        page = client.get(
            f"rest/agile/1.0/board/{board_id}/issue",
            params={"startAt": start, "maxResults": 100, "fields": "key"},
        )
        if not isinstance(page, dict):
            break
        issues = page.get("issues")
        if not isinstance(issues, list):
            break
        keys.extend(issue["key"] for issue in issues if isinstance(issue, dict) and isinstance(issue.get("key"), str))
        total = page.get("total")
        if not isinstance(total, int) or start + 100 >= total:
            break
        start += 100
    return keys


def move_issue_to_backlog(client: Any, key: str) -> None:
    """POST rest/agile/1.0/backlog/issue -- removes `key` from whatever
    sprint/board placement it has, same as dragging a card down into Jira's
    own Backlog section."""
    client.move_issues_to_backlog([key])


def move_issue_to_board(client: Any, board_id: object, key: str) -> None:
    """POST rest/agile/1.0/board/{boardId}/issue -- the "move onto the
    board" counterpart to move_issue_to_backlog. Not wrapped by the
    atlassian-python-api library (it only has the GET side, get_issues_for_
    board), so this goes through the client's own generic post() directly,
    the same way fetch_backlog_keys/fetch_board_keys go through its generic
    get()."""
    client.post(client.get_agile_resource_url(f"board/{board_id}/issue"), json={"issues": [key]})


def fetch_rank_field_id(client: Any) -> str | None:
    """Jira's Agile "Rank" field is a custom field whose id varies per
    instance -- discovered here by display name rather than assumed, using
    the same instance-wide field listing the New Issue page's create-meta
    lookup already relies on (fetch_all_field_names, issue.py). Returns
    None (not an exception) when no field is named "Rank" -- callers must
    treat that as "ranking unavailable here", not a hard failure, since some
    instances hide or rename it."""
    from .issue import fetch_all_field_names

    for field_id, name in fetch_all_field_names(client).items():
        if name.strip().lower() == "rank":
            return field_id
    return None


def rank_issue_before(client: Any, key: str, before_key: str, rank_field_id: str) -> None:
    """PUT rest/agile/1.0/issue/rank -- places `key` immediately before
    `before_key` in Jira's own rank order, the same operation dragging a
    card to a new position in Jira's backlog view performs."""
    client.update_rank([key], before_key, rank_field_id)


def refresh_boards_api(
    jira_dir: Path,
    project: str,
    client: ProjectMetadataClient,
    component_field: str,
) -> dict[str, Any]:
    try:
        component_field_names = field_clause_names(client, component_field)
        raw_boards = client.get_all_agile_boards(project_key=project)
    except Exception as exc:
        raise MetadataError(f"could not refresh Jira boards for {project}: {exc}") from exc

    boards: list[dict[str, Any]] = []
    for raw_board in normalize_boards(raw_boards):
        board_id = raw_board.get("id")
        name = str(raw_board.get("name") or board_id)
        board_type = str(raw_board.get("type") or "unknown")
        jql: str | None = None
        predicate: dict[str, Any] | None = None
        reason: str | None = None
        try:
            config = client.get_agile_board_configuration(board_id)
            filter_ref = config.get("filter") if isinstance(config, dict) else None
            filter_id = filter_ref.get("id") if isinstance(filter_ref, dict) else None
            if filter_id:
                filter_payload = client.get(f"rest/api/2/filter/{filter_id}")
                jql = filter_payload.get("jql") if isinstance(filter_payload, dict) else None
        except Exception as exc:
            reason = f"could not read board filter: {exc}"

        if reason is None:
            if not jql:
                reason = "board has no filter JQL"
            else:
                predicate, reason = compile_jql(jql, component_field_names=component_field_names)
                if predicate is not None:
                    # A board only ever shows issues from its own project,
                    # even when its saved filter doesn't literally say so
                    # (that's real Jira behavior, not a JQL quirk) -- so an
                    # unscoped filter is implicitly AND-ed with this board's
                    # own project rather than matching every project.
                    predicate = scope_predicate_to_project(predicate, project)

        backlog_keys: list[str] | None = None
        try:
            backlog_keys = fetch_backlog_keys(client, board_id)
        except Exception:
            backlog_keys = None

        board_keys: list[str] | None = None
        try:
            board_keys = fetch_board_keys(client, board_id)
        except Exception:
            board_keys = None

        boards.append(
            {
                "id": board_id,
                "name": name,
                "type": board_type,
                "jql": jql,
                "predicate": predicate,
                "unsupportedReason": reason,
                "backlogKeys": backlog_keys,
                "boardKeys": board_keys,
            }
        )

    cache = {"project": project, "fetchedAt": utc_now(), "boards": boards}
    write_json(boards_path(jira_dir, project), cache)
    # Board membership/active-vs-backlog classification is computed fresh
    # from this exact file on every load_manifest_items() call (see
    # with_local_index_fields, view.py), not baked into manifest.json at
    # sync time -- so a cached all_items() result (server.py) needs to
    # know this changed too, the same way a shadow write or a full sync
    # already bumps this counter.
    bump_manifest_generation(jira_dir)
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
    write_json(component_field_options_path(jira_dir, project, field_id), cache)
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
    client: ProjectMetadataClient,
    *,
    max_age_seconds: int = DEFAULT_METADATA_TTL_SECONDS,
) -> MetadataResult:
    cache = load_versions(jira_dir, project)
    if cache is not None and cache_is_fresh(cache, project, max_age_seconds):
        return MetadataResult(cache=cache, refreshed=False)
    try:
        return MetadataResult(cache=refresh_versions_api(jira_dir, project, client), refreshed=True)
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


def format_boards(cache: dict[str, Any]) -> str:
    boards = normalize_boards(cache.get("boards"))
    if not boards:
        return "no boards found\n"

    names = [str(board.get("name") or "") for board in boards]
    width = max(len("board"), *(len(name) for name in names))
    rows = [f"{'board':<{width}}  type    membership   backlog"]
    for board, name in zip(boards, names, strict=True):
        board_type = str(board.get("type") or "")
        reason = board.get("unsupportedReason")
        jql = board.get("jql")
        membership = f"unsupported: {reason}" if reason else "ok"
        if jql:
            # "Supported"/"unsupported" is a jira-wb concept (whether
            # compile_jql could translate this board's own saved filter
            # into a local predicate) -- always showing the real JQL next
            # to it makes that verifiable at a glance, whichever way it
            # went, rather than trusting the reason text alone.
            membership += f" (jql: {jql})"
        backlog_keys = board.get("backlogKeys")
        backlog_display = f"{len(backlog_keys)} issues" if isinstance(backlog_keys, list) else "n/a"
        rows.append(f"{name:<{width}}  {board_type:<6}  {membership:<12} {backlog_display}")
    return "\n".join(rows) + "\n"


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


def load_component_summary(jira_dir: Path, project: str | None = None) -> list[dict[str, Any]]:
    """Component usage across locally synced issues, with active/total
    counts -- scoped to `project` when given. Derived directly from the
    manifest's own workItems (each of which already knows its own project),
    not from the manifest's separate top-level "components" list: that list
    is built from the on-disk components/<slug>/<KEY>/ directory structure,
    which is shared across every synced project regardless of which
    project's custom component field a given slug actually belongs to --
    using it unscoped mixed every other project's component values (and
    their _unassigned bucket's project-wide counts) into what should have
    been one project's own short list."""
    path = manifest_path(jira_dir)
    if not path.exists():
        return []
    value = read_json(path)
    if not isinstance(value, dict):
        return []
    work_items = value.get("workItems")
    if not isinstance(work_items, list):
        components = value.get("components")
        return [component for component in components if isinstance(component, dict)] if isinstance(components, list) else []
    by_component: dict[str, dict[str, Any]] = {}
    for item in work_items:
        if not isinstance(item, dict):
            continue
        if project is not None and item.get("project") != project:
            continue
        component = str(item.get("component") or "").strip()
        if not component:
            continue
        entry = by_component.setdefault(component.lower(), {"component": component, "active": 0, "total": 0})
        entry["total"] += 1
        category = item.get("statusCategory")
        is_done = (
            category == "done"
            if isinstance(category, str)
            else str(item.get("status") or "").strip().lower() in FALLBACK_DONE_STATUS_NAMES
        )
        if not is_done:
            entry["active"] += 1
    return sort_components(list(by_component.values()))


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
