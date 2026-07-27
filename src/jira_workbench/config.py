from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    view_filter: str | None = None
    view_swimlane: str | None = None
    view_preview_lines: int | None = None
    versions_filter: str | None = None
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

    return WorkbenchConfig(
        project=optional_string(data, "project", expanded),
        component_field=optional_string(data, "component_field", expanded),
        jira_dir=optional_string(data, "jira_dir", expanded),
        jira_url=optional_string(data, "jira_url", expanded),
        jira_email=optional_string(data, "jira_email", expanded),
        jira_api_token=optional_string(data, "jira_api_token", expanded),
        view_component=optional_string(view, "component", expanded),
        view_filter=optional_string(view, "filter", expanded),
        view_swimlane=optional_string(view, "swimlane", expanded),
        view_preview_lines=optional_int(view, "preview_lines", expanded),
        versions_filter=optional_string(versions, "filter", expanded),
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


def choose(flag: str | int | None, configured: str | int | None) -> str | int | None:
    return flag if flag is not None else configured
