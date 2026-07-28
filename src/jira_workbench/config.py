from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit


DEFAULT_CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "jira-wb" / "config.toml"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkbenchConfig:
    project: str | None = None
    component_field: str | None = None
    jira_dir: str | None = None
    jira_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    view_component: str | None = None
    view_fix_version: str | None = None
    view_assignee: str | None = None
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
        view_component=optional_string(view, "component", expanded),
        view_fix_version=optional_string(view, "fix_version", expanded),
        view_assignee=optional_string(view, "assignee", expanded),
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


def optional_string(data: dict[str, Any], key: str, path: Path) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"invalid config file {path}: {key} must be a non-empty string")
    return value


def optional_int(data: dict[str, Any], key: str, path: Path) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ConfigError(f"invalid config file {path}: {key} must be an integer")
    return value


def optional_bool(data: dict[str, Any], key: str, path: Path) -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ConfigError(f"invalid config file {path}: {key} must be a boolean")
    return value


def choose(flag: str | int | None, configured: str | int | None) -> str | int | None:
    return flag if flag is not None else configured


def save_view_defaults(path: Path, updates: dict[str, str | bool | None]) -> None:
    """Persist `updates` into the `[view]` table of `path`, preserving comments/formatting.

    A None or empty-string value removes that key entirely, so saving a
    filter that's currently unset clears any previously saved default
    rather than leaving a stale value behind.
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
        if value is None or value == "":
            view.pop(key, None)
        else:
            view[key] = value

    expanded.write_text(tomlkit.dumps(document))
    secure_config_permissions(expanded)
