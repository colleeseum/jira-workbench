from __future__ import annotations

import argparse
import curses
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .config import DEFAULT_CONFIG_PATH, ConfigError, choose, load_config
from .metadata import (
    DEFAULT_METADATA_TTL_SECONDS,
    JiraApiConfig,
    MetadataError,
    add_component_api,
    add_component_field_option_api,
    add_version_api,
    archive_version_api,
    check_jira_api_config,
    delete_version_api,
    format_component_field_option_cache,
    format_doctor_checks,
    format_components as format_meta_components,
    format_component_cache,
    format_versions,
    jira_api_client,
    load_component_field_options,
    load_component_summary,
    load_components,
    load_versions,
    merge_components,
    normalize_components,
    normalize_versions,
    release_version_api,
    rename_version_api,
    refresh_component_field_options_api,
    refresh_components_api,
    refresh_versions_api,
    version_name,
)
from .server import serve
from .shadow import (
    ShadowError,
    add_comment,
    commit_shadow,
    push_shadows,
    refresh_local_issue_after_push,
    render_diff,
    set_field,
    set_status_change,
    shadow_status,
    unset_field,
)
from .sync import SyncConfig, SyncError, sync_project
from .view import ViewError, detailed_shadow_report_lines, format_components, format_work_item, interactive_view


def sync_progress_printer(stream: object = sys.stderr) -> Callable[[str], None]:
    in_place = False
    last_len = 0
    is_tty = bool(getattr(stream, "isatty", lambda: False)())

    def progress(message: str) -> None:
        nonlocal in_place, last_len
        if is_tty and message.startswith("[3/5] Syncing changed issues... "):
            padding = " " * max(0, last_len - len(message))
            getattr(stream, "write")(f"\r{message}{padding}")
            getattr(stream, "flush")()
            in_place = True
            last_len = len(message)
            return
        if in_place:
            getattr(stream, "write")("\n")
            in_place = False
            last_len = 0
        print(message, file=stream)

    return progress


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jira-wb")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Configuration file path. Default: ~/.jira-wb.conf",
    )
    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser("sync", help="Sync Jira work items into a local directory")
    sync_parser.add_argument("--project", default=os.environ.get("JIRA_PROJECT"))
    sync_parser.add_argument("--component-field", default=os.environ.get("JIRA_COMPONENT_FIELD"))
    sync_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    sync_parser.add_argument("--jira-url", default=os.environ.get("JIRA_URL"))
    sync_parser.add_argument("--jira-email", default=os.environ.get("JIRA_EMAIL"))
    sync_parser.add_argument("--jira-api-token", default=os.environ.get("JIRA_API_TOKEN"))

    serve_parser = subparsers.add_parser("serve", help="Serve the local Jira browser")
    serve_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)

    view_parser = subparsers.add_parser("view", help="View locally synced Jira work items")
    view_parser.add_argument("key", nargs="?")
    view_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    view_parser.add_argument(
        "--component-field",
        default=os.environ.get("JIRA_COMPONENT_FIELD"),
        help="Override the issue field used as Components for this view command",
    )
    view_parser.add_argument("--component", help="Filter the interactive item list by synced component")
    view_parser.add_argument("--filter", help="Filter the interactive item list by text pattern")
    view_parser.add_argument(
        "--swimlane",
        choices=["none", "epic", "version", "component"],
        help="Group the interactive item list by epic, version, or component",
    )
    view_parser.add_argument("--components", action="store_true", help="List synced components and exit")
    view_parser.add_argument(
        "-r",
        "--read-only",
        action="store_true",
        help="Print a work item to stdout instead of opening the curses view",
    )
    view_parser.add_argument(
        "--all",
        action="store_true",
        help="In the interactive item list, include closed, done, and resolved work items",
    )
    view_mode = view_parser.add_mutually_exclusive_group()
    view_mode.add_argument("--original", action="store_true", help="Show the last synced Jira issue")
    view_mode.add_argument("--diff", action="store_true", help="Show the local shadow diff")

    issue_parser = subparsers.add_parser("issue", help="Create and manage Jira issues")
    issue_parser.add_argument("--project", default=os.environ.get("JIRA_PROJECT"))
    issue_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    issue_parser.add_argument("--jira-url", default=os.environ.get("JIRA_URL"))
    issue_parser.add_argument("--jira-email", default=os.environ.get("JIRA_EMAIL"))
    issue_parser.add_argument("--jira-api-token", default=os.environ.get("JIRA_API_TOKEN"))
    issue_parser.add_argument("--component-field", default=os.environ.get("JIRA_COMPONENT_FIELD"))
    issue_parser.set_defaults(help_parser=issue_parser)
    issue_subparsers = issue_parser.add_subparsers(dest="issue_command")

    issue_create = issue_subparsers.add_parser("create", help="Create a Jira issue and sync it locally")
    issue_create.add_argument("--type", default="Improvement", help="Issue type name. Default: Improvement.")
    issue_create.add_argument("--summary", required=True)
    issue_create.add_argument("--description")
    issue_create.add_argument("--description-file")
    issue_create.add_argument("--component", help="Effective workbench component value")
    issue_create.add_argument("--parent", help="Parent issue key, usually an Epic")
    issue_create.add_argument("--priority", help="Priority name")
    issue_create.add_argument("--dry-run", action="store_true")

    meta_parser = subparsers.add_parser("meta", help="Manage cached Jira project metadata")
    meta_parser.add_argument("--project", default=os.environ.get("JIRA_PROJECT"))
    meta_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    meta_parser.add_argument("--jira-url", default=os.environ.get("JIRA_URL"))
    meta_parser.add_argument("--jira-email", default=os.environ.get("JIRA_EMAIL"))
    meta_parser.add_argument("--jira-api-token", default=os.environ.get("JIRA_API_TOKEN"))
    meta_parser.add_argument("--component-field", default=os.environ.get("JIRA_COMPONENT_FIELD"))
    meta_subparsers = meta_parser.add_subparsers(dest="meta_command")

    meta_refresh = meta_subparsers.add_parser("refresh", help="Refresh cached Jira project metadata")
    meta_refresh.add_argument("--versions", action="store_true", help="Refresh project fix versions")
    meta_refresh.add_argument("--components", action="store_true", help="Refresh project components")

    meta_versions = meta_subparsers.add_parser("versions", help="List cached Jira project fix versions")
    meta_versions.add_argument(
        "--cached",
        action="store_true",
        help="Use cached versions only and do not refresh stale metadata",
    )
    meta_versions.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_METADATA_TTL_SECONDS,
        help=f"Metadata freshness window. Default: {DEFAULT_METADATA_TTL_SECONDS}.",
    )

    meta_components = meta_subparsers.add_parser("components", help="List effective workbench components")
    meta_components.add_argument(
        "--cached",
        action="store_true",
        help="Use cached effective components only and do not refresh project metadata",
    )
    meta_components.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_METADATA_TTL_SECONDS,
        help=f"Metadata freshness window. Default: {DEFAULT_METADATA_TTL_SECONDS}.",
    )

    version_add = meta_subparsers.add_parser("version-add", help="Create a Jira project fix version")
    version_add.add_argument("name")

    version_rename = meta_subparsers.add_parser("version-rename", help="Rename a Jira project fix version")
    version_rename.add_argument("version")
    version_rename.add_argument("new_name")

    version_release = meta_subparsers.add_parser("version-release", help="Mark a Jira project fix version released")
    version_release.add_argument("version")
    version_release.add_argument("--release-date", help="Release date in Jira's expected date format")

    version_archive = meta_subparsers.add_parser("version-archive", help="Archive a Jira project fix version")
    version_archive.add_argument("version")

    version_delete = meta_subparsers.add_parser("version-delete", help="Delete a Jira project fix version")
    version_delete.add_argument("version")
    version_delete.add_argument("--move-fix-to", help="Move fixVersion references to another version")
    version_delete.add_argument("--move-affected-to", help="Move affectedVersion references to another version")

    native_components = meta_subparsers.add_parser("native-components", help="List native Jira project components")
    native_components.add_argument(
        "--cached",
        action="store_true",
        help="Use cached native Jira components only",
    )

    native_component_add = meta_subparsers.add_parser(
        "native-component-add",
        help="Create a native Jira project component",
    )
    native_component_add.add_argument("name")

    component_add = meta_subparsers.add_parser(
        "component-add",
        help="Deprecated: use component-field-add or native-component-add",
    )
    component_add.add_argument("name")

    component_field_options = meta_subparsers.add_parser(
        "component-field-options",
        help="List configured custom component field options",
    )
    component_field_options.add_argument(
        "--cached",
        action="store_true",
        help="Use cached custom field options only",
    )

    component_field_add = meta_subparsers.add_parser(
        "component-field-add",
        help="Create an option in the configured custom component field",
    )
    component_field_add.add_argument("name")
    component_field_add.add_argument(
        "--context-id",
        help="Jira custom field context id. Required unless the option already exists.",
    )

    meta_subparsers.add_parser("doctor", help="Test Jira Workbench metadata configuration")

    shadow_parser = subparsers.add_parser(
        "shadow",
        help="Manage local-only Jira changes",
        description="Manage local-only Jira changes",
    )
    shadow_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    shadow_parser.set_defaults(help_parser=shadow_parser)
    shadow_subparsers = shadow_parser.add_subparsers(dest="shadow_command")

    set_parser = shadow_subparsers.add_parser("set", help="Set a local-only field value")
    set_parser.add_argument("key")
    set_parser.add_argument("field")
    set_parser.add_argument("value")

    unset_parser = shadow_subparsers.add_parser("unset", help="Remove a local-only field value")
    unset_parser.add_argument("key")
    unset_parser.add_argument("field")

    comment_parser = shadow_subparsers.add_parser("comment", help="Add a local-only comment")
    comment_parser.add_argument("key")
    comment_parser.add_argument("body")

    status_change_parser = shadow_subparsers.add_parser(
        "status-change",
        help="Set local-only transition metadata such as resolution",
    )
    status_change_parser.add_argument("key")
    status_change_parser.add_argument("--resolution")

    diff_parser = shadow_subparsers.add_parser("diff", help="Show local shadow diffs")
    diff_parser.add_argument("keys", nargs="*")
    diff_parser.add_argument("-o", "--output", help="Write the diff to a file instead of stdout")

    report_parser = shadow_subparsers.add_parser("report", help="Report local shadow changes")
    report_parser.add_argument("keys", nargs="*")
    report_parser.add_argument("-o", "--output", help="Write the report to a file instead of stdout")

    commit_parser = shadow_subparsers.add_parser("commit", help="Mark local shadow changes ready")
    commit_parser.add_argument("keys", nargs="+")

    shadow_subparsers.add_parser("status", help="List local shadow changes")

    push_parser = shadow_subparsers.add_parser("push", help="Push committed local shadow changes")
    push_parser.add_argument("keys", nargs="*")
    push_parser.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "sync":
        project = choose(args.project, config.project)
        component_field = choose(args.component_field, config.component_field) or "components"
        jira_dir = choose(args.jira_dir, config.jira_dir)
        jira_url = choose(args.jira_url, config.jira_url)
        jira_email = choose(args.jira_email, config.jira_email)
        jira_api_token = choose(args.jira_api_token, config.jira_api_token)
        missing = [
            name
            for name, value in (
                ("project", project),
                ("jira_dir", jira_dir),
            )
            if value is None
        ]
        if missing:
            print(
                "error: missing required configuration: "
                f"{', '.join(missing)}. Set them in ~/.jira-wb.conf or pass flags.",
                file=sys.stderr,
            )
            return 2
        try:
            api_project, api_client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        except MetadataError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            result = sync_project(
                SyncConfig(
                    project=api_project,
                    component_field=str(component_field),
                    jira_dir=Path(str(jira_dir)),
                ),
                api_client,
                progress=sync_progress_printer(sys.stderr),
            )
        except SyncError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            f"Synced {result.work_item_count} work items "
            f"({result.changed_count} changed, {result.skipped_count} unchanged, "
            f"{result.version_count} versions cached)."
        )
        return 0

    if args.command == "serve":
        jira_dir = choose(args.jira_dir, config.jira_dir)
        host = choose(args.host, config.host)
        port = choose(args.port, config.port)
        missing = [
            name
            for name, value in (("jira_dir", jira_dir), ("serve.host", host), ("serve.port", port))
            if value is None
        ]
        if missing:
            print(
                "error: missing required configuration: "
                f"{', '.join(missing)}. Set them in ~/.jira-wb.conf or pass flags.",
                file=sys.stderr,
            )
            return 2
        serve(Path(str(jira_dir)), str(host), int(port))
        return 0

    if args.command == "view":
        jira_dir = choose(args.jira_dir, config.jira_dir)
        component_field = choose(args.component_field, config.component_field) or "components"
        view_component = choose(args.component, config.view_component)
        view_filter = choose(args.filter, config.view_filter)
        view_swimlane = choose(args.swimlane, config.view_swimlane) or "none"
        if jira_dir is None:
            print(
                "error: missing required configuration: jira_dir. "
                "Set it in ~/.jira-wb.conf or pass --jira-dir.",
                file=sys.stderr,
            )
            return 2
        try:
            if args.components:
                print(format_components(Path(str(jira_dir))), end="")
            elif args.key:
                mode = "diff" if args.diff else "original" if args.original else "shadow"
                if args.read_only:
                    print(
                        format_work_item(
                            Path(str(jira_dir)),
                            args.key,
                            component_field=str(component_field) if component_field else None,
                            mode=mode,
                        )
                    )
                else:
                    interactive_view(
                        Path(str(jira_dir)),
                        component_field=str(component_field) if component_field else None,
                        component=str(view_component) if view_component else None,
                        pattern=str(view_filter) if view_filter else None,
                        active=not args.all,
                        jira_url=str(config.jira_url) if config.jira_url else None,
                        jira_email=str(config.jira_email) if config.jira_email else None,
                        jira_api_token=str(config.jira_api_token) if config.jira_api_token else None,
                        initial_key=args.key,
                        initial_mode=mode,
                        swimlane=str(view_swimlane),
                    )
            else:
                if args.diff or args.original:
                    print("error: --diff and --original require a work item key", file=sys.stderr)
                    return 2
                interactive_view(
                    Path(str(jira_dir)),
                    component_field=str(component_field) if component_field else None,
                    component=str(view_component) if view_component else None,
                    pattern=str(view_filter) if view_filter else None,
                    active=not args.all,
                    jira_url=str(config.jira_url) if config.jira_url else None,
                    jira_email=str(config.jira_email) if config.jira_email else None,
                    jira_api_token=str(config.jira_api_token) if config.jira_api_token else None,
                    swimlane=str(view_swimlane),
                )
        except ViewError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "issue":
        if args.issue_command is None:
            issue_help = getattr(args, "help_parser", None)
            if issue_help is not None:
                issue_help.print_help()
            else:
                parser.print_help()
            return 0
        jira_dir = choose(args.jira_dir, config.jira_dir)
        project = choose(args.project, config.project)
        jira_url = choose(args.jira_url, config.jira_url)
        jira_email = choose(args.jira_email, config.jira_email)
        jira_api_token = choose(args.jira_api_token, config.jira_api_token)
        component_field = choose(args.component_field, config.component_field) or "components"
        if jira_dir is None:
            print(
                "error: missing required configuration: jira_dir. "
                "Set it in ~/.jira-wb.conf or pass --jira-dir.",
                file=sys.stderr,
            )
            return 2
        try:
            return run_issue(
                args,
                Path(str(jira_dir)),
                project,
                component_field,
                jira_url,
                jira_email,
                jira_api_token,
            )
        except MetadataError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ShadowError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.command == "shadow":
        if args.shadow_command is None:
            args.help_parser.print_help()
            return 0
        jira_dir = choose(args.jira_dir, config.jira_dir)
        if jira_dir is None:
            print(
                "error: missing required configuration: jira_dir. "
                "Set it in ~/.jira-wb.conf or pass --jira-dir.",
                file=sys.stderr,
            )
            return 2
        try:
            return run_shadow(args, Path(str(jira_dir)), config)
        except MetadataError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ShadowError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.command == "meta":
        jira_dir = choose(args.jira_dir, config.jira_dir)
        project = choose(args.project, config.project)
        jira_url = choose(args.jira_url, config.jira_url)
        jira_email = choose(args.jira_email, config.jira_email)
        jira_api_token = choose(args.jira_api_token, config.jira_api_token)
        component_field = choose(args.component_field, config.component_field) or "components"
        versions_filter = config.versions_filter
        if jira_dir is None:
            print(
                "error: missing required configuration: jira_dir. "
                "Set it in ~/.jira-wb.conf or pass --jira-dir.",
                file=sys.stderr,
            )
            return 2
        try:
            try:
                return run_meta(
                    args,
                    Path(str(jira_dir)),
                    project,
                    jira_url,
                    jira_email,
                    jira_api_token,
                    versions_filter,
                    component_field,
                )
            except KeyboardInterrupt:
                print()
                return 130
        except MetadataError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    parser.print_help()
    return 0


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
            f"{', '.join(missing)}. Set them in ~/.jira-wb.conf or pass flags."
        )
    return str(project), jira_api_client(
        JiraApiConfig(url=str(jira_url), email=str(jira_email), api_token=str(jira_api_token))
    )


def issue_description(args: argparse.Namespace) -> str:
    if args.description and args.description_file:
        raise MetadataError("pass only one of --description or --description-file")
    if args.description_file:
        path = Path(str(args.description_file))
        try:
            return path.read_text()
        except OSError as exc:
            raise MetadataError(f"could not read description file {path}: {exc}") from exc
    return str(args.description or "")


def issue_type_create_fields(client: object, project: str, issue_type: str) -> dict[str, object]:
    try:
        metadata = client.issue_createmeta(project)
    except Exception as exc:
        raise MetadataError(f"could not read Jira create metadata for {project}: {exc}") from exc
    projects = metadata.get("projects") if isinstance(metadata, dict) else None
    if not isinstance(projects, list):
        raise MetadataError(f"Jira create metadata for {project} did not include projects")
    for project_meta in projects:
        if not isinstance(project_meta, dict):
            continue
        issue_types = project_meta.get("issuetypes")
        if not isinstance(issue_types, list):
            continue
        for item in issue_types:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").lower() != issue_type.lower():
                continue
            fields = item.get("fields")
            if isinstance(fields, dict):
                return fields
    raise MetadataError(f"issue type {issue_type} is not available for project {project}")


def issue_component_value(component_field: str, component: str) -> object:
    if component_field == "components":
        return [{"name": component}]
    return {"value": component}


def issue_create_fields(
    *,
    project: str,
    issue_type: str,
    summary: str,
    description: str,
    component_field: str,
    component: str | None,
    parent: str | None,
    priority: str | None,
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
            raise MetadataError(f"issue type {issue_type} cannot set parent during create")
        fields["parent"] = {"key": parent}
    if priority:
        if "priority" not in create_fields:
            raise MetadataError(f"issue type {issue_type} cannot set priority during create")
        fields["priority"] = {"name": priority}
    if component:
        if component_field not in create_fields:
            raise MetadataError(f"issue type {issue_type} cannot set component field {component_field} during create")
        fields[component_field] = issue_component_value(component_field, component)
    return fields


def run_issue(
    args: argparse.Namespace,
    jira_dir: Path,
    project: object | None,
    component_field: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> int:
    if args.issue_command == "create":
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        summary = str(args.summary).strip()
        if not summary:
            raise MetadataError("summary must be non-empty")
        issue_type = str(args.type).strip()
        if not issue_type:
            raise MetadataError("issue type must be non-empty")
        create_fields = issue_type_create_fields(client, api_project, issue_type)
        reporter = None
        if "reporter" in create_fields:
            try:
                myself = client.myself()
            except Exception as exc:
                raise MetadataError(f"could not resolve current Jira user for reporter: {exc}") from exc
            account_id = myself.get("accountId") if isinstance(myself, dict) else None
            if not account_id:
                raise MetadataError("current Jira user response did not include accountId")
            reporter = {"accountId": account_id}
        fields = issue_create_fields(
            project=api_project,
            issue_type=issue_type,
            summary=summary,
            description=issue_description(args),
            component_field=str(component_field or "components"),
            component=str(args.component).strip() if args.component else None,
            parent=str(args.parent).strip() if args.parent else None,
            priority=str(args.priority).strip() if args.priority else None,
            create_fields=create_fields,
            reporter=reporter,
        )
        if args.dry_run:
            for key in sorted(fields):
                print(f"{key}: {fields[key]}")
            return 0
        try:
            created = client.issue_create(fields)
        except Exception as exc:
            raise MetadataError(f"could not create Jira issue {summary}: {exc}") from exc
        key = created.get("key") if isinstance(created, dict) else None
        if not key:
            raise MetadataError(f"Jira create response for {summary} did not include key")
        refresh_local_issue_after_push(jira_dir, str(key), client, str(component_field or "components"))
        print(f"created {key}: {summary}")
        return 0

    print("error: missing issue command", file=sys.stderr)
    return 2


def parse_shadow_set_value(value: str) -> object:
    stripped = value.strip()
    if not stripped:
        return value
    if stripped[0] in "{[\"" or stripped in {"null", "true", "false"} or re.fullmatch(r"-?\d+(\.\d+)?", stripped):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def run_meta(
    args: argparse.Namespace,
    jira_dir: Path,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
    versions_filter: object | None = None,
    component_field: object | None = None,
) -> int:
    if args.meta_command is None:
        return interactive_meta(
            jira_dir,
            project,
            jira_url,
            jira_email,
            jira_api_token,
            str(versions_filter) if versions_filter else None,
            str(component_field) if component_field else None,
        )

    if args.meta_command == "refresh":
        refresh_versions_requested = args.versions or not args.components
        if refresh_versions_requested:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_versions_api(jira_dir, api_project, client)
            print(f"refreshed {len(cache.get('versions', []))} versions")
        if args.components:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            if component_field and str(component_field) != "components":
                cache = refresh_component_field_options_api(jira_dir, api_project, str(component_field), client)
                print(f"refreshed {len(cache.get('options', []))} component field options")
            else:
                cache = refresh_components_api(jira_dir, api_project, client)
                print(f"refreshed {len(cache.get('components', []))} native components")
        return 0

    if args.meta_command == "versions":
        if args.cached:
            cache = load_versions(jira_dir)
        else:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_versions_api(jira_dir, api_project, client)

        if cache is None:
            print(
                "error: no cached versions found. Run jira-wb meta refresh with Jira API configured.",
                file=sys.stderr,
            )
            return 1
        print(format_versions(cache), end="")
        return 0

    if args.meta_command == "components":
        if component_field and str(component_field) != "components":
            print(
                meta_component_field_options_output(
                    jira_dir,
                    project,
                    component_field,
                    jira_url,
                    jira_email,
                    jira_api_token,
                    cached=args.cached,
                ),
                end="",
            )
            return 0
        components = load_component_summary(jira_dir)
        if components:
            print(format_meta_components(components), end="")
            return 0
        if not args.cached and all(value is not None for value in (project, jira_url, jira_email, jira_api_token)):
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_components_api(jira_dir, api_project, client)
            print(format_component_cache(cache), end="")
            return 0

        print(
            "error: no effective components found. Run jira-wb meta refresh --components or jira-wb sync first.",
            file=sys.stderr,
        )
        return 1

    if args.meta_command == "version-add":
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = add_version_api(jira_dir, api_project, args.name, client)
        print(f"created version {args.name}")
        print(f"refreshed {len(cache.get('versions', []))} versions")
        return 0

    if args.meta_command == "version-rename":
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = rename_version_api(jira_dir, api_project, args.version, args.new_name, client)
        print(f"renamed version {args.version} to {args.new_name}")
        print(f"refreshed {len(cache.get('versions', []))} versions")
        return 0

    if args.meta_command == "version-release":
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = release_version_api(
            jira_dir,
            api_project,
            args.version,
            client,
            release_date=args.release_date,
        )
        print(getattr(cache, "message", f"released version {args.version}"))
        print(f"refreshed {len(cache.get('versions', []))} versions")
        return 0

    if args.meta_command == "version-archive":
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = archive_version_api(jira_dir, api_project, args.version, client)
        print(getattr(cache, "message", f"archived version {args.version}"))
        print(f"refreshed {len(cache.get('versions', []))} versions")
        return 0

    if args.meta_command == "version-delete":
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = delete_version_api(
            jira_dir,
            api_project,
            args.version,
            client,
            move_fix_to=args.move_fix_to,
            move_affected_to=args.move_affected_to,
        )
        print(f"deleted version {args.version}")
        print(f"refreshed {len(cache.get('versions', []))} versions")
        return 0

    if args.meta_command == "component-add":
        print(
            "error: component-add is ambiguous. Use component-field-add for the configured "
            "single-select component field, or native-component-add for Jira native Components.",
            file=sys.stderr,
        )
        return 2

    if args.meta_command == "native-components":
        cache = load_components(jira_dir) if args.cached else None
        if cache is None and not args.cached:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_components_api(jira_dir, api_project, client)
        if cache is None:
            print("error: no cached native Jira components found", file=sys.stderr)
            return 1
        print(format_component_cache(cache), end="")
        return 0

    if args.meta_command == "native-component-add":
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = add_component_api(jira_dir, api_project, args.name, client)
        print(f"created native component {args.name}")
        print(f"refreshed {len(cache.get('components', []))} components")
        return 0

    if args.meta_command == "component-field-options":
        print(
            meta_component_field_options_output(
                jira_dir,
                project,
                component_field,
                jira_url,
                jira_email,
                jira_api_token,
                cached=args.cached,
            ),
            end="",
        )
        return 0

    if args.meta_command == "component-field-add":
        print(
            add_meta_component_field_option(
                jira_dir,
                args.name,
                project,
                component_field,
                args.context_id,
                jira_url,
                jira_email,
                jira_api_token,
            ),
            end="",
        )
        return 0

    if args.meta_command == "doctor":
        api_config = None
        if jira_url is not None or jira_email is not None or jira_api_token is not None:
            if jira_url is not None and jira_email is not None and jira_api_token is not None:
                api_config = JiraApiConfig(
                    url=str(jira_url),
                    email=str(jira_email),
                    api_token=str(jira_api_token),
                )
        checks = []
        checks.extend(check_jira_api_config(str(project) if project is not None else None, api_config))
        print(format_doctor_checks(checks), end="")
        return 0 if all(check.ok for check in checks) else 1

    print("error: missing meta command", file=sys.stderr)
    return 2


MetaAction = Callable[[], str]


def meta_versions_output(
    jira_dir: Path,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> str:
    cache = load_versions(jira_dir)
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
    cache = load_components(jira_dir)
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
    field_id = str(component_field)
    cache = load_component_field_options(jira_dir, field_id)
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


def prompt_text(stdscr: curses.window, prompt: str) -> str | None:
    height, width = stdscr.getmaxyx()
    curses.echo()
    try:
        stdscr.move(height - 1, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(height - 1, 0, prompt, max(1, width - 1))
        value = stdscr.getstr(height - 1, min(len(prompt), max(0, width - 1)), max(1, width - len(prompt) - 1))
    finally:
        curses.noecho()
    text = value.decode(errors="replace").strip()
    return text or None


def confirm_text(stdscr: curses.window, lines: list[str], expected: str) -> bool:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    for offset, line in enumerate(lines[: max(0, height - 2)]):
        attr = curses.A_BOLD if offset == 0 else curses.A_NORMAL
        stdscr.addnstr(offset, 0, line, max(1, width - 1), attr)
    stdscr.refresh()
    curses.curs_set(1)
    try:
        answer = prompt_text(stdscr, f"Type {expected} to confirm: ")
    finally:
        curses.curs_set(0)
    return answer == expected


def load_meta_versions(
    jira_dir: Path,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> list[dict[str, object]]:
    cache = load_versions(jira_dir)
    if cache is None:
        if project is None or jira_url is None or jira_email is None or jira_api_token is None:
            raise MetadataError("no cached versions found and live metadata access is not configured")
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        cache = refresh_versions_api(jira_dir, api_project, client)
    return normalize_versions(cache.get("versions"))


def version_identifier(version: dict[str, object]) -> str:
    value = version.get("id")
    if isinstance(value, str) and value:
        return value
    return version_name(version)


def version_row(version: dict[str, object]) -> str:
    name = version_name(version)
    status = "released" if version.get("released") else "unreleased"
    archived = " archived" if version.get("archived") else ""
    version_id = str(version.get("id") or "")
    return f"{name:<38} {status:<10}{archived:<10} {version_id}"


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


def render_version_list(
    stdscr: curses.window,
    versions: list[dict[str, object]],
    selected: int,
    message: str,
    pattern: str | None,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    title = "Versions  (j/k move, / filter, \\ clear, e rename, r release, a archive, d delete, n new, q back)"
    if pattern:
        title += f"  [filter={pattern}]"
    stdscr.addnstr(0, 0, title, max(1, width - 1), curses.A_BOLD)
    stdscr.addnstr(2, 0, f"{'name':<38} {'state':<10} {'archive':<10} id", max(1, width - 1), curses.A_BOLD)
    visible_height = max(1, height - 6)
    start = min(max(0, selected - visible_height + 1), max(0, len(versions) - visible_height))
    for offset, version in enumerate(versions[start : start + visible_height]):
        index = start + offset
        attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
        stdscr.addnstr(offset + 3, 0, version_row(version), max(1, width - 1), attr)
    if message:
        stdscr.addnstr(height - 2, 0, message.splitlines()[0], max(1, width - 1))
    stdscr.refresh()


def interactive_versions(
    stdscr: curses.window,
    jira_dir: Path,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
    default_filter: str | None = None,
) -> str:
    try:
        versions = load_meta_versions(jira_dir, project, jira_url, jira_email, jira_api_token)
    except MetadataError as exc:
        return f"error: {exc}"
    selected = 0
    message = ""
    current_filter = default_filter
    while True:
        visible_versions, filter_error = filter_versions(versions, current_filter)
        if filter_error:
            message = filter_error
        if visible_versions:
            selected = min(selected, len(visible_versions) - 1)
        else:
            selected = 0
        render_version_list(stdscr, visible_versions, selected, message, current_filter)
        key = stdscr.getch()
        if key in (ord("q"), 27, 3):
            return message or "version list closed"
        if key in (curses.KEY_DOWN, ord("j")) and visible_versions:
            selected = min(len(visible_versions) - 1, selected + 1)
            continue
        if key in (curses.KEY_UP, ord("k")) and visible_versions:
            selected = max(0, selected - 1)
            continue
        if key == ord("/"):
            curses.curs_set(1)
            try:
                value = prompt_text(stdscr, "Version regex filter: ")
            finally:
                curses.curs_set(0)
            current_filter = value
            selected = 0
            message = "filter updated" if value else "filter cleared"
            continue
        if key == ord("\\"):
            current_filter = None
            selected = 0
            message = "filter cleared"
            continue
        if key == ord("n"):
            curses.curs_set(1)
            try:
                name = prompt_text(stdscr, "Add version: ")
            finally:
                curses.curs_set(0)
            if not name:
                message = "add cancelled"
                continue
            try:
                message = add_meta_version(jira_dir, name, project, jira_url, jira_email, jira_api_token)
                versions = load_meta_versions(jira_dir, project, jira_url, jira_email, jira_api_token)
            except MetadataError as exc:
                message = f"error: {exc}"
            continue
        if not visible_versions:
            message = "no versions found"
            continue
        current = visible_versions[selected]
        identifier = version_identifier(current)
        current_name = version_name(current)
        if key == ord("e"):
            curses.curs_set(1)
            try:
                new_name = prompt_text(stdscr, f"Rename {current_name} to: ")
            finally:
                curses.curs_set(0)
            if not new_name:
                message = "rename cancelled"
                continue
            try:
                message = rename_meta_version(
                    jira_dir, identifier, new_name, project, jira_url, jira_email, jira_api_token
                )
                versions = load_meta_versions(jira_dir, project, jira_url, jira_email, jira_api_token)
            except MetadataError as exc:
                message = f"error: {exc}"
            continue
        if key == ord("r"):
            curses.curs_set(1)
            try:
                release_date = prompt_text(stdscr, f"Release date for {current_name} (optional): ")
            finally:
                curses.curs_set(0)
            try:
                message = release_meta_version(
                    jira_dir, identifier, release_date, project, jira_url, jira_email, jira_api_token
                )
                versions = load_meta_versions(jira_dir, project, jira_url, jira_email, jira_api_token)
            except MetadataError as exc:
                message = f"error: {exc}"
            continue
        if key == ord("a"):
            confirmed = confirm_text(
                stdscr,
                [
                    "Archive Jira version",
                    "",
                    f"Version: {current_name}",
                    f"ID: {identifier}",
                    "",
                    "Archived versions are hidden from normal version selection in Jira.",
                ],
                "archive",
            )
            if not confirmed:
                message = "archive cancelled"
                continue
            try:
                message = archive_meta_version(jira_dir, identifier, project, jira_url, jira_email, jira_api_token)
                versions = load_meta_versions(jira_dir, project, jira_url, jira_email, jira_api_token)
            except MetadataError as exc:
                message = f"error: {exc}"
            continue
        if key == ord("d"):
            confirmed = confirm_text(
                stdscr,
                [
                    "Delete Jira version",
                    "",
                    f"Version: {current_name}",
                    f"ID: {identifier}",
                    "",
                    "This deletes the version from Jira.",
                    "Issues using this fixVersion will have it removed unless you choose a move target next.",
                ],
                "delete",
            )
            if not confirmed:
                message = "delete cancelled"
                continue
            curses.curs_set(1)
            try:
                move_fix_to = prompt_text(stdscr, "Move fixVersion to (optional): ")
            finally:
                curses.curs_set(0)
            try:
                message = delete_meta_version(
                    jira_dir,
                    identifier,
                    move_fix_to,
                    project,
                    jira_url,
                    jira_email,
                    jira_api_token,
                )
                versions = load_meta_versions(jira_dir, project, jira_url, jira_email, jira_api_token)
            except MetadataError as exc:
                message = f"error: {exc}"
            continue


def render_meta_screen(
    stdscr: curses.window,
    actions: list[tuple[str, str]],
    selected: int,
    output: str,
    show_help: bool,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    title = "Jira metadata  (j/k move, Enter open, n new, e edit, r release, a archive, d delete, h help, q quit)"
    stdscr.addnstr(0, 0, title, max(1, width - 1), curses.A_BOLD)

    if show_help:
        lines = [
            "Help",
            "",
            "j/k or arrows  Move selection",
            "Enter          Show selected metadata",
            "n              Add selected metadata type",
            "e              Rename selected version",
            "r              Release selected version",
            "a              Archive selected version",
            "d              Delete selected version",
            "h              Toggle help",
            "q or Esc       Quit",
        ]
    else:
        lines = []
        for index, (label, description) in enumerate(actions):
            prefix = "> " if index == selected else "  "
            attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
            stdscr.addnstr(index + 2, 0, f"{prefix}{label:<12} {description}", max(1, width - 1), attr)
        start = len(actions) + 3
        lines = output.splitlines() or ["Press Enter to view metadata."]
        for offset, line in enumerate(lines[: max(0, height - start - 1)]):
            stdscr.addnstr(start + offset, 0, line, max(1, width - 1))

    if show_help:
        for offset, line in enumerate(lines[: max(0, height - 2)]):
            stdscr.addnstr(offset + 2, 0, line, max(1, width - 1))
    stdscr.refresh()


def interactive_meta(
    jira_dir: Path,
    project: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
    versions_filter: str | None,
    component_field: str | None,
) -> int:
    actions = [
        ("Versions", "List fix versions"),
        ("Components", "List effective workbench components"),
    ]

    def run(stdscr: curses.window) -> int:
        curses.curs_set(0)
        selected = 0
        output = ""
        show_help = False
        while True:
            render_meta_screen(stdscr, actions, selected, output, show_help)
            key = stdscr.getch()
            if key in (ord("q"), 27, 3):
                return 0
            if key in (ord("h"),):
                show_help = not show_help
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(actions) - 1, selected + 1)
                show_help = False
                continue
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
                show_help = False
                continue
            if key in (10, 13, curses.KEY_ENTER):
                show_help = False
                try:
                    if selected == 0:
                        output = interactive_versions(
                            stdscr,
                            jira_dir,
                            project,
                            jira_url,
                            jira_email,
                            jira_api_token,
                            versions_filter,
                        )
                    else:
                        output = meta_components_output(
                            jira_dir, project, component_field, jira_url, jira_email, jira_api_token
                        )
                except MetadataError as exc:
                    output = f"error: {exc}"
                continue
            if key == ord("n"):
                show_help = False
                label = "version" if selected == 0 else "component"
                curses.curs_set(1)
                try:
                    name = prompt_text(stdscr, f"Add {label}: ")
                finally:
                    curses.curs_set(0)
                if not name:
                    output = "add cancelled"
                    continue
                try:
                    if selected == 0:
                        output = add_meta_version(
                            jira_dir, name, project, jira_url, jira_email, jira_api_token
                        )
                    elif component_field and component_field != "components":
                        curses.curs_set(1)
                        try:
                            context_id = prompt_text(stdscr, f"Context id for {component_field}: ")
                        finally:
                            curses.curs_set(0)
                        output = add_meta_component_field_option(
                            jira_dir,
                            name,
                            project,
                            component_field,
                            context_id,
                            jira_url,
                            jira_email,
                            jira_api_token,
                        )
                    else:
                        output = add_meta_component(
                            jira_dir, name, project, jira_url, jira_email, jira_api_token
                        )
                except MetadataError as exc:
                    output = f"error: {exc}"
                continue
        return 0

    return curses.wrapper(run)


def run_shadow(args: argparse.Namespace, jira_dir: Path, config: object) -> int:
    if args.shadow_command == "set":
        set_field(jira_dir, args.key, args.field, parse_shadow_set_value(args.value))
        print(f"{args.key}: set local {args.field}")
        return 0

    if args.shadow_command == "unset":
        unset_field(jira_dir, args.key, args.field)
        print(f"{args.key}: unset local {args.field}")
        return 0

    if args.shadow_command == "comment":
        add_comment(jira_dir, args.key, args.body)
        print(f"{args.key}: added local comment")
        return 0

    if args.shadow_command == "status-change":
        set_status_change(jira_dir, args.key, resolution=args.resolution)
        if args.resolution:
            print(f"{args.key}: set local status-change resolution")
        else:
            print(f"{args.key}: cleared local status-change metadata")
        return 0

    if args.shadow_command == "diff":
        keys = args.keys or [row["key"] for row in shadow_status(jira_dir)]
        if not keys:
            print("no local shadow changes")
            return 0
        output = "\n\n".join(render_diff(jira_dir, key) for key in keys) + "\n"
        if args.output:
            path = Path(str(args.output))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output)
            print(f"wrote shadow diff to {path}")
        else:
            print(output, end="")
        return 0

    if args.shadow_command == "report":
        keys = args.keys or [row["key"] for row in shadow_status(jira_dir)]
        if not keys:
            print("no local shadow changes")
            return 0
        output = "\n".join(detailed_shadow_report_lines(jira_dir, keys)) + "\n"
        if args.output:
            path = Path(str(args.output))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output)
            print(f"wrote shadow report to {path}")
        else:
            print(output, end="")
        return 0

    if args.shadow_command == "commit":
        for key in args.keys:
            commit_shadow(jira_dir, key)
            print(f"{key}: committed local shadow changes")
        return 0

    if args.shadow_command == "status":
        rows = shadow_status(jira_dir)
        if not rows:
            print("no local shadow changes")
            return 0
        for row in rows:
            print(
                f"{row['key']} {row['state']} "
                f"fields={row['fields']} comments={row['comments']} baseUpdated={row['baseUpdated']}"
            )
        return 0

    if args.shadow_command == "push":
        jira_url = getattr(config, "jira_url", None)
        jira_email = getattr(config, "jira_email", None)
        jira_api_token = getattr(config, "jira_api_token", None)
        if not (jira_url and jira_email and jira_api_token):
            print(
                "error: missing required Jira API configuration: "
                "jira_url, jira_email, jira_api_token. Set them in ~/.jira-wb.conf.",
                file=sys.stderr,
            )
            return 2
        jira_client = jira_api_client(
            JiraApiConfig(url=str(jira_url), email=str(jira_email), api_token=str(jira_api_token))
        )
        result = push_shadows(
            jira_dir,
            args.keys,
            jira_client=jira_client,
            dry_run=args.dry_run,
            progress=lambda message: print(message, file=sys.stderr),
            component_field=str(config.component_field or "components"),
        )
        print(
            f"push summary: pushed={result.pushed} skipped={result.skipped} "
            f"blocked={result.blocked} failed={result.failed}"
        )
        if result.errors:
            print("push errors:", file=sys.stderr)
            for error in result.errors:
                print(f"- {error}", file=sys.stderr)
        return 1 if result.blocked or result.failed else 0

    args.help_parser.print_help()
    return 2
