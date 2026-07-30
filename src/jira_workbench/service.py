from __future__ import annotations

import re
from pathlib import Path

from .metadata import (
    JiraApiConfig,
    MetadataError,
    add_component_api,
    add_component_field_option_api,
    add_version_api,
    archive_version_api,
    delete_version_api,
    format_component_field_option_cache,
    format_components as format_meta_components,
    format_component_cache,
    format_versions,
    jira_api_client,
    load_all_components,
    load_all_versions,
    load_boards,
    load_component_field_options,
    load_component_summary,
    load_components,
    load_versions,
    merge_components,
    normalize_boards,
    normalize_components,
    normalize_versions,
    release_version_api,
    rename_version_api,
    refresh_boards_api,
    refresh_component_field_options_api,
    refresh_components_api,
    refresh_versions_api,
    version_name,
)


def api_client_from_config(
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> tuple[str, object]:
    missing = [
        name
        for name, value in (
            ("project", project),
            ("jira_url", jira_url),
            ("jira_email", jira_email),
            ("jira_api_token", jira_api_token),
        )
        if value is None
    ]
    if missing:
        raise MetadataError(
            "missing required configuration for Jira API: "
            f"{', '.join(missing)}. Set them in ~/.config/jira-wb/config.toml or pass flags."
        )
    return str(project), jira_api_client(
        JiraApiConfig(url=str(jira_url), email=str(jira_email), api_token=str(jira_api_token))
    )


def meta_versions_output(
    jira_dir: Path,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    if project is None:
        merged = load_all_versions(jira_dir)
        if merged:
            return format_versions({"versions": merged})
    else:
        cache = load_versions(jira_dir, str(project))
        if cache is not None:
            return format_versions(cache)
    if project is not None and jira_url is not None and jira_email is not None and jira_api_token is not None:
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        return format_versions(refresh_versions_api(jira_dir, api_project, client))
    raise MetadataError("no cached versions found and live metadata access is not configured")


def meta_components_output(
    jira_dir: Path,
    project: object | None,
    component_field: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    if component_field and str(component_field) != "components":
        return meta_component_field_options_output(
            jira_dir,
            project,
            component_field,
            jira_url,
            jira_email,
            jira_api_token,
        )
    summary = load_component_summary(jira_dir)
    if summary:
        return format_meta_components(summary)
    if project is None:
        merged = load_all_components(jira_dir)
        if merged:
            return format_component_cache({"components": merged})
    else:
        cache = load_components(jira_dir, str(project))
        if cache is not None:
            return format_component_cache(cache)
    if project is not None and jira_url is not None and jira_email is not None and jira_api_token is not None:
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        return format_component_cache(refresh_components_api(jira_dir, api_project, client))
    raise MetadataError("no cached components found and live metadata access is not configured")


def meta_component_field_options_output(
    jira_dir: Path,
    project: object | None,
    component_field: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
    *,
    cached: bool = False,
) -> str:
    if component_field is None:
        raise MetadataError("component_field is required to list custom component field options")
    if project is None:
        raise MetadataError(f"no cached options found for {component_field}")
    field_id = str(component_field)
    cache = load_component_field_options(jira_dir, str(project), field_id)
    if cache is None and not cached:
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = refresh_component_field_options_api(jira_dir, api_project, field_id, client)
    if cache is None:
        raise MetadataError(f"no cached options found for {field_id}")
    summary = load_component_summary(jira_dir)
    if summary:
        return format_meta_components(merge_components(normalize_components(cache.get("options")), summary))
    return format_component_field_option_cache(cache)


def add_meta_version(
    jira_dir: Path,
    name: str,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
    cache = add_version_api(jira_dir, api_project, name, client)
    return f"created version {name}\nrefreshed {len(cache.get('versions', []))} versions\n"


def add_meta_component(
    jira_dir: Path,
    name: str,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
    cache = add_component_api(jira_dir, api_project, name, client)
    return f"created component {name}\nrefreshed {len(cache.get('components', []))} components\n"


def add_meta_component_field_option(
    jira_dir: Path,
    name: str,
    project: object | None,
    component_field: object | None,
    context_id: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    if component_field is None:
        raise MetadataError("component_field is required to add custom component field options")
    api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
    cache = add_component_field_option_api(
        jira_dir,
        api_project,
        str(component_field),
        str(context_id) if context_id else None,
        name,
        client,
    )
    return f"ensured custom component option {name} in {component_field}\nrefreshed {len(cache.get('options', []))} options\n"


def rename_meta_version(
    jira_dir: Path,
    identifier: str,
    new_name: str,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
    cache = rename_version_api(jira_dir, api_project, identifier, new_name, client)
    return f"renamed version {identifier} to {new_name}\nrefreshed {len(cache.get('versions', []))} versions\n"


def release_meta_version(
    jira_dir: Path,
    identifier: str,
    release_date: str | None,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
    cache = release_version_api(jira_dir, api_project, identifier, client, release_date=release_date)
    return f"{getattr(cache, 'message', f'released version {identifier}')}\nrefreshed {len(cache.get('versions', []))} versions\n"


def archive_meta_version(
    jira_dir: Path,
    identifier: str,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
    cache = archive_version_api(jira_dir, api_project, identifier, client)
    return f"{getattr(cache, 'message', f'archived version {identifier}')}\nrefreshed {len(cache.get('versions', []))} versions\n"


def delete_meta_version(
    jira_dir: Path,
    identifier: str,
    move_fix_to: str | None,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
    cache = delete_version_api(
        jira_dir,
        api_project,
        identifier,
        client,
        move_fix_to=move_fix_to,
    )
    detail = f"deleted version {identifier}"
    if move_fix_to:
        detail += f"; moved fixVersion references to {move_fix_to}"
    return f"{detail}\nrefreshed {len(cache.get('versions', []))} versions\n"


def load_meta_versions(
    jira_dir: Path,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> list[dict[str, object]]:
    if project is None:
        raise MetadataError("no cached versions found and live metadata access is not configured")
    cache = load_versions(jira_dir, str(project))
    if cache is None:
        if jira_url is None or jira_email is None or jira_api_token is None:
            raise MetadataError("no cached versions found and live metadata access is not configured")
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = refresh_versions_api(jira_dir, api_project, client)
    return normalize_versions(cache.get("versions"))


def load_meta_components(
    jira_dir: Path,
    project: object | None,
    component_field: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> list[dict[str, object]]:
    """Structured equivalent of meta_components_output -- same cache/live
    precedence (component-field options first if a custom field is
    configured, then the manifest's own usage summary, then the native
    components cache, then a live refresh), but returns the data itself
    instead of a formatted report, for a JSON API consumer."""
    if component_field and str(component_field) != "components":
        if project is None:
            raise MetadataError(f"no cached options found for {component_field}")
        field_id = str(component_field)
        cache = load_component_field_options(jira_dir, str(project), field_id)
        if cache is None:
            if jira_url is None or jira_email is None or jira_api_token is None:
                raise MetadataError(f"no cached options found for {field_id}")
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_component_field_options_api(jira_dir, api_project, field_id, client)
        options = normalize_components(cache.get("options"))
        summary = load_component_summary(jira_dir)
        return merge_components(options, summary) if summary else options

    summary = load_component_summary(jira_dir)
    if summary:
        return summary
    if project is None:
        merged = load_all_components(jira_dir)
        if merged:
            return merged
    else:
        cache = load_components(jira_dir, str(project))
        if cache is not None:
            return normalize_components(cache.get("components"))
    if project is not None and jira_url is not None and jira_email is not None and jira_api_token is not None:
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        return normalize_components(refresh_components_api(jira_dir, api_project, client).get("components"))
    raise MetadataError("no cached components found and live metadata access is not configured")


def load_meta_boards(
    jira_dir: Path,
    project: object | None,
    component_field: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> list[dict[str, object]]:
    if project is None:
        raise MetadataError("no cached boards found and live metadata access is not configured")
    cache = load_boards(jira_dir, str(project))
    if cache is None:
        if jira_url is None or jira_email is None or jira_api_token is None:
            raise MetadataError("no cached boards found and live metadata access is not configured")
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = refresh_boards_api(jira_dir, api_project, client, str(component_field or "components"))
    return normalize_boards(cache.get("boards"))


def version_identifier(version: dict[str, object]) -> str:
    value = version.get("id")
    if isinstance(value, str) and value:
        return value
    return version_name(version)


def version_filter_text(version: dict[str, object]) -> str:
    return " ".join(
        [
            version_name(version),
            str(version.get("id") or ""),
            "released" if version.get("released") else "unreleased",
            "archived" if version.get("archived") else "",
        ]
    )


def filter_versions(
    versions: list[dict[str, object]],
    pattern: str | None,
) -> tuple[list[dict[str, object]], str | None]:
    if not pattern:
        return versions, None
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return versions, f"invalid regex: {exc}"
    return [version for version in versions if regex.search(version_filter_text(version))], None
