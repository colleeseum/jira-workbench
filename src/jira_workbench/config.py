from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit


DEFAULT_CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "jira-wb" / "config.toml"
# Local data store (synced issues, shadows, meta caches) -- a shared home for
# potentially several projects, so it belongs under XDG_DATA_HOME (app data)
# rather than requiring every user to pick and remember their own path, the
# same way a mail client keeps one local profile/mailbox regardless of where
# it's launched from.
DEFAULT_JIRA_DIR = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / "jira-wb"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectSettings:
    key: str
    default: bool = False
    read_only: bool = False
    history_months: int | None = None


@dataclass(frozen=True)
class WorkbenchConfig:
    project: str | None = None
    component_field: str | None = None
    jira_dir: str | None = None
    jira_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    projects: tuple[ProjectSettings, ...] = ()
    sync_history_months: int | None = None
    view_project: tuple[str, ...] | None = None
    view_status: tuple[str, ...] | None = None
    view_component: tuple[str, ...] | None = None
    view_fix_version: tuple[str, ...] | None = None
    view_assignee: tuple[str, ...] | None = None
    view_board: str | None = None
    view_board_scope: str | None = None
    view_filter: str | None = None
    view_swimlane: str | None = None
    view_active: bool | None = None
    view_nerd_font: bool | None = None
    view_dev_status_field: str | None = None
    view_preview_lines: int | None = None
    view_hide_done_after_days: int | None = None
    versions_filter: str | None = None
    issue_default_type: str | None = None
    host: str | None = None
    port: int | None = None

    def resolved_projects(self) -> tuple[ProjectSettings, ...]:
        """The multi-project `[[projects]]` list if configured, else a
        single synthetic entry for the legacy flat `project` key (fully
        read-write, implicitly default) -- the one place backward
        compatibility with a plain `project = "SAT"` config is resolved."""
        if self.projects:
            return self.projects
        if self.project:
            return (ProjectSettings(key=self.project, default=True),)
        return ()

    def default_project_key(self) -> str | None:
        projects = self.resolved_projects()
        for project in projects:
            if project.default:
                return project.key
        return projects[0].key if len(projects) == 1 else None

    def read_only_project_keys(self) -> frozenset[str]:
        return frozenset(project.key for project in self.resolved_projects() if project.read_only)

    def effective_history_months(self, project_key: str | None) -> int | None:
        """How many months of statusCategory=Done history to sync for this
        project -- None means unbounded (sync everything, today's default).
        A project's own `history_months` wins over the top-level
        `sync_history_months` fallback, so one huge project can get a
        tighter window without forcing it on every other configured one."""
        for project in self.resolved_projects():
            if project.key == project_key:
                return project.history_months if project.history_months is not None else self.sync_history_months
        return self.sync_history_months


def secure_config_permissions(path: Path) -> bool:
    """Tighten an overly permissive config file to 0600 -- it may hold a Jira API token.

    Returns True if permissions were changed, False if they were already
    restrictive enough (or on non-POSIX platforms, where this is a no-op --
    Windows doesn't use these permission bits).
    """
    if os.name != "posix":
        return False
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        path.chmod(0o600)
        return True
    return False


def load_config(path: Path) -> WorkbenchConfig:
    expanded = path.expanduser()
    if not expanded.exists():
        return WorkbenchConfig()
    try:
        data = tomllib.loads(expanded.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config file {expanded}: {exc}") from exc
    if not isinstance(data, dict):
        return WorkbenchConfig()

    serve = data.get("serve", {})
    if serve is None:
        serve = {}
    if not isinstance(serve, dict):
        raise ConfigError(f"invalid config file {expanded}: [serve] must be a table")
    view = data.get("view", {})
    if view is None:
        view = {}
    if not isinstance(view, dict):
        raise ConfigError(f"invalid config file {expanded}: [view] must be a table")
    versions = data.get("versions", {})
    if versions is None:
        versions = {}
    if not isinstance(versions, dict):
        raise ConfigError(f"invalid config file {expanded}: [versions] must be a table")
    issue = data.get("issue", {})
    if issue is None:
        issue = {}
    if not isinstance(issue, dict):
        raise ConfigError(f"invalid config file {expanded}: [issue] must be a table")

    return WorkbenchConfig(
        project=optional_string(data, "project", expanded),
        component_field=optional_string(data, "component_field", expanded),
        jira_dir=optional_string(data, "jira_dir", expanded),
        jira_url=optional_string(data, "jira_url", expanded),
        jira_email=optional_string(data, "jira_email", expanded),
        jira_api_token=optional_string(data, "jira_api_token", expanded),
        projects=parse_projects(data.get("projects"), expanded),
        sync_history_months=optional_positive_int(data, "sync_history_months", expanded),
        view_project=optional_string_tuple(view, "project", expanded),
        view_status=optional_string_tuple(view, "status", expanded),
        view_component=optional_string_tuple(view, "component", expanded),
        view_fix_version=optional_string_tuple(view, "fix_version", expanded),
        view_assignee=optional_string_tuple(view, "assignee", expanded),
        view_board=optional_string(view, "board", expanded),
        view_board_scope=optional_string(view, "board_scope", expanded),
        view_filter=optional_string(view, "filter", expanded),
        view_swimlane=optional_string(view, "swimlane", expanded),
        view_active=optional_bool(view, "active", expanded),
        view_nerd_font=optional_bool(view, "nerd_font", expanded),
        view_dev_status_field=optional_string(view, "dev_status_field", expanded),
        view_preview_lines=optional_int(view, "preview_lines", expanded),
        view_hide_done_after_days=optional_int(view, "hide_done_after_days", expanded),
        versions_filter=optional_string(versions, "filter", expanded),
        issue_default_type=optional_string(issue, "default_type", expanded),
        host=optional_string(serve, "host", expanded),
        port=optional_int(serve, "port", expanded),
    )


def parse_projects(value: Any, path: Path) -> tuple[ProjectSettings, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"invalid config file {path}: [[projects]] must be an array of tables")
    projects = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError(f"invalid config file {path}: each [[projects]] entry must be a table")
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"invalid config file {path}: each [[projects]] entry needs a non-empty key")
        default = entry.get("default", False)
        if not isinstance(default, bool):
            raise ConfigError(f"invalid config file {path}: projects.default must be a boolean")
        read_only = entry.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ConfigError(f"invalid config file {path}: projects.read_only must be a boolean")
        history_months = entry.get("history_months")
        if history_months is not None and (not isinstance(history_months, int) or history_months <= 0):
            raise ConfigError(f"invalid config file {path}: projects.history_months must be a positive integer")
        projects.append(
            ProjectSettings(key=key, default=default, read_only=read_only, history_months=history_months)
        )
    if sum(1 for p in projects if p.default) > 1:
        raise ConfigError(f"invalid config file {path}: only one [[projects]] entry may set default = true")
    return tuple(projects)


def optional_string(data: dict[str, Any], key: str, path: Path) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"invalid config file {path}: {key} must be a non-empty string")
    return value


def optional_string_tuple(data: dict[str, Any], key: str, path: Path) -> tuple[str, ...] | None:
    """A `[view]` field that accepts either a bare string (kept backward
    compatible with every pre-multi-select config.toml, wrapped as a
    1-tuple) or an array of strings (the new multi-select form)."""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            raise ConfigError(f"invalid config file {path}: {key} must be a non-empty string")
        return (value,)
    if isinstance(value, list):
        if not value:
            raise ConfigError(f"invalid config file {path}: {key} must not be an empty array")
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ConfigError(f"invalid config file {path}: {key} array entries must be non-empty strings")
        return tuple(value)
    raise ConfigError(f"invalid config file {path}: {key} must be a string or array of strings")


def optional_int(data: dict[str, Any], key: str, path: Path) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ConfigError(f"invalid config file {path}: {key} must be an integer")
    return value


def optional_positive_int(data: dict[str, Any], key: str, path: Path) -> int | None:
    value = optional_int(data, key, path)
    if value is not None and value <= 0:
        raise ConfigError(f"invalid config file {path}: {key} must be a positive integer")
    return value


def optional_bool(data: dict[str, Any], key: str, path: Path) -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ConfigError(f"invalid config file {path}: {key} must be a boolean")
    return value


def choose(
    flag: str | int | None, configured: str | int | tuple[str, ...] | None
) -> str | int | tuple[str, ...] | None:
    return flag if flag is not None else configured


def resolve_jira_dir(cli_value: str | None, config_value: str | None) -> Path:
    """An explicit --jira-dir/JIRA_DIR/config value always wins; otherwise
    falls back to the shared default data directory rather than requiring
    every user to configure one (see DEFAULT_JIRA_DIR)."""
    return Path(choose(cli_value, config_value) or str(DEFAULT_JIRA_DIR))


def save_view_defaults(path: Path, updates: dict[str, str | bool | list[str] | None]) -> None:
    """Persist `updates` into the `[view]` table of `path`, preserving comments/formatting.

    A None, empty-string, or empty-list value removes that key entirely, so
    saving a filter that's currently unset clears any previously saved
    default rather than leaving a stale value behind. A non-empty list is
    written as a native TOML array (tomlkit serializes lists as-is).
    """
    expanded = path.expanduser()
    if expanded.exists():
        try:
            document = tomlkit.parse(expanded.read_text())
        except tomlkit.exceptions.ParseError as exc:
            raise ConfigError(f"invalid config file {expanded}: {exc}") from exc
    else:
        expanded.parent.mkdir(parents=True, exist_ok=True)
        document = tomlkit.document()

    view = document.get("view")
    if not isinstance(view, dict):
        view = tomlkit.table()
        document["view"] = view

    for key, value in updates.items():
        if value is None or value == "" or value == []:
            view.pop(key, None)
        else:
            view[key] = value

    expanded.write_text(tomlkit.dumps(document))
    secure_config_permissions(expanded)

