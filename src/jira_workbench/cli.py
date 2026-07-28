from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .config import DEFAULT_CONFIG_PATH, ConfigError, choose, load_config, secure_config_permissions
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
    format_boards,
    format_component_field_option_cache,
    format_doctor_checks,
    format_components as format_meta_components,
    format_component_cache,
    format_versions,
    jira_api_client,
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
from .issue import IssueError, build_create_fields, create_issue, fetch_issue_type_fields, resolve_reporter
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
from .view import ViewError, detailed_shadow_report_lines, format_components, format_work_item


def open_interactive_view(
    jira_dir: Path,
    *,
    component_field: str | None = None,
    component: str | None = None,
    fix_version: str | None = None,
    assignee: str | None = None,
    board: str | None = None,
    board_scope: str | None = None,
    pattern: str | None = None,
    active: bool = True,
    jira_url: str | None = None,
    jira_email: str | None = None,
    jira_api_token: str | None = None,
    initial_key: str | None = None,
    initial_mode: str = "shadow",
    swimlane: str | None = None,
    project: str | None = None,
    versions_filter: str | None = None,
    preview_lines: int | None = None,
    hide_done_after_days: int | None = None,
    config_path: Path | None = None,
    nerd_font: bool = False,
) -> None:
    from .tui.app import run_view as run_textual_view

    run_textual_view(
        jira_dir,
        component_field=component_field,
        component=component,
        fix_version=fix_version,
        assignee=assignee,
        board=board,
        board_scope=board_scope,
        pattern=pattern,
        active=active,
        swimlane=swimlane,
        initial_key=initial_key,
        initial_mode=initial_mode,
        jira_url=jira_url,
        jira_email=jira_email,
        jira_api_token=jira_api_token,
        project=project,
        versions_filter=versions_filter,
        preview_lines=preview_lines,
        hide_done_after_days=hide_done_after_days,
        config_path=config_path,
        nerd_font=nerd_font,
    )


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
        default=None,
        help="Configuration file path. Default: ~/.config/jira-wb/config.toml",
    )
    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser("sync", help="Sync Jira work items into a local directory")
    sync_parser.add_argument("--project", default=os.environ.get("JIRA_PROJECT"))
    sync_parser.add_argument("--component-field", default=os.environ.get("JIRA_COMPONENT_FIELD"))
    sync_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    sync_parser.add_argument("--jira-url", default=os.environ.get("JIRA_URL"))
    sync_parser.add_argument("--jira-email", default=os.environ.get("JIRA_EMAIL"))
    sync_parser.add_argument("--jira-api-token", default=os.environ.get("JIRA_API_TOKEN"))
    sync_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-fetch every issue regardless of its updated timestamp. Normal syncs skip an issue "
            "whose own updated timestamp hasn't changed, which misses drift caused by something else "
            "changing (e.g. a fix version renamed in Jira) -- --force refreshes everything to catch that."
        ),
    )

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
        help="Print a work item to stdout instead of opening the interactive view",
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
    issue_create.add_argument(
        "--type", default=None, help="Issue type name. Required via this flag or [issue].default_type in config."
    )
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
    meta_refresh.add_argument("--boards", action="store_true", help="Refresh Jira Kanban boards and membership")

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

    meta_boards = meta_subparsers.add_parser("boards", help="List cached Jira Kanban boards and membership")
    meta_boards.add_argument(
        "--cached",
        action="store_true",
        help="Use cached boards only and do not refresh from Jira",
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
    config_path = Path(args.config) if args.config is not None else DEFAULT_CONFIG_PATH
    expanded_config_path = config_path.expanduser()
    if expanded_config_path.exists() and secure_config_permissions(expanded_config_path):
        print(
            f"note: tightened permissions on {expanded_config_path} to 0600 (it may contain a Jira API token)",
            file=sys.stderr,
        )
    try:
        config = load_config(config_path)
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
                f"{', '.join(missing)}. Set them in ~/.config/jira-wb/config.toml or pass flags.",
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
                    force=args.force,
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
                f"{', '.join(missing)}. Set them in ~/.config/jira-wb/config.toml or pass flags.",
                file=sys.stderr,
            )
            return 2
        serve(Path(str(jira_dir)), str(host), int(port))
        return 0

    if args.command == "view":
        jira_dir = choose(args.jira_dir, config.jira_dir)
        component_field = choose(args.component_field, config.component_field) or "components"
        view_component = choose(args.component, config.view_component)
        view_fix_version = config.view_fix_version
        view_assignee = config.view_assignee
        view_board = config.view_board
        view_board_scope = config.view_board_scope
        view_filter = choose(args.filter, config.view_filter)
        view_swimlane = choose(args.swimlane, config.view_swimlane) or "none"
        if args.all:
            view_active = False
        elif config.view_active is not None:
            view_active = config.view_active
        else:
            view_active = True
        view_nerd_font = config.view_nerd_font or False
        if jira_dir is None:
            print(
                "error: missing required configuration: jira_dir. "
                "Set it in ~/.config/jira-wb/config.toml or pass --jira-dir.",
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
                    open_interactive_view(
                        Path(str(jira_dir)),
                        component_field=str(component_field) if component_field else None,
                        component=str(view_component) if view_component else None,
                        fix_version=str(view_fix_version) if view_fix_version else None,
                        assignee=str(view_assignee) if view_assignee else None,
                        board=str(view_board) if view_board else None,
                        board_scope=str(view_board_scope) if view_board_scope else None,
                        pattern=str(view_filter) if view_filter else None,
                        active=view_active,
                        jira_url=str(config.jira_url) if config.jira_url else None,
                        jira_email=str(config.jira_email) if config.jira_email else None,
                        jira_api_token=str(config.jira_api_token) if config.jira_api_token else None,
                        initial_key=args.key,
                        initial_mode=mode,
                        swimlane=str(view_swimlane),
                        project=str(config.project) if config.project else None,
                        versions_filter=str(config.versions_filter) if config.versions_filter else None,
                        preview_lines=config.view_preview_lines,
                        hide_done_after_days=config.view_hide_done_after_days,
                        config_path=config_path,
                        nerd_font=view_nerd_font,
                    )
            else:
                if args.diff or args.original:
                    print("error: --diff and --original require a work item key", file=sys.stderr)
                    return 2
                open_interactive_view(
                    Path(str(jira_dir)),
                    component_field=str(component_field) if component_field else None,
                    component=str(view_component) if view_component else None,
                    fix_version=str(view_fix_version) if view_fix_version else None,
                    assignee=str(view_assignee) if view_assignee else None,
                    board=str(view_board) if view_board else None,
                    board_scope=str(view_board_scope) if view_board_scope else None,
                    pattern=str(view_filter) if view_filter else None,
                    active=view_active,
                    jira_url=str(config.jira_url) if config.jira_url else None,
                    jira_email=str(config.jira_email) if config.jira_email else None,
                    jira_api_token=str(config.jira_api_token) if config.jira_api_token else None,
                    swimlane=str(view_swimlane),
                    project=str(config.project) if config.project else None,
                    versions_filter=str(config.versions_filter) if config.versions_filter else None,
                    preview_lines=config.view_preview_lines,
                    hide_done_after_days=config.view_hide_done_after_days,
                    config_path=config_path,
                    nerd_font=view_nerd_font,
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
                "Set it in ~/.config/jira-wb/config.toml or pass --jira-dir.",
                file=sys.stderr,
            )
            return 2
        issue_type = choose(args.type, config.issue_default_type)
        try:
            return run_issue(
                args,
                Path(str(jira_dir)),
                project,
                component_field,
                jira_url,
                jira_email,
                jira_api_token,
                issue_type,
            )
        except MetadataError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ShadowError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except IssueError as exc:
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
                "Set it in ~/.config/jira-wb/config.toml or pass --jira-dir.",
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
                "Set it in ~/.config/jira-wb/config.toml or pass --jira-dir.",
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
                    config_path,
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
            f"{', '.join(missing)}. Set them in ~/.config/jira-wb/config.toml or pass flags."
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


def run_issue(
    args: argparse.Namespace,
    jira_dir: Path,
    project: object | None,
    component_field: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
    issue_type: object | None,
) -> int:
    if args.issue_command == "create":
        api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
        summary = str(args.summary).strip()
        if not summary:
            raise MetadataError("summary must be non-empty")
        issue_type = str(issue_type).strip() if issue_type else ""
        if not issue_type:
            raise MetadataError(
                "issue type is required: pass --type or set [issue].default_type in "
                "~/.config/jira-wb/config.toml"
            )
        type_fields = fetch_issue_type_fields(client, api_project)
        matched_type = next((name for name in type_fields if name.lower() == issue_type.lower()), None)
        if matched_type is None:
            raise IssueError(f"issue type {issue_type} is not available for project {api_project}")
        create_fields = type_fields[matched_type]
        reporter = resolve_reporter(client) if "reporter" in create_fields else None
        fields = build_create_fields(
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
        key = create_issue(client, fields)
        refresh_local_issue_after_push(jira_dir, key, client, str(component_field or "components"))
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
    config_path: Path | None = None,
) -> int:
    if args.meta_command is None:
        from .tui.app import run_meta_app

        return run_meta_app(
            jira_dir,
            project=str(project) if project else None,
            jira_url=str(jira_url) if jira_url else None,
            jira_email=str(jira_email) if jira_email else None,
            jira_api_token=str(jira_api_token) if jira_api_token else None,
            versions_filter=str(versions_filter) if versions_filter else None,
            component_field=str(component_field) if component_field else None,
            config_path=config_path,
        )

    if args.meta_command == "refresh":
        refresh_versions_requested = args.versions or not (args.components or args.boards)
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
        if args.boards:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_boards_api(jira_dir, api_project, client, str(component_field or "components"))
            print(f"refreshed {len(cache.get('boards', []))} boards")
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

    if args.meta_command == "boards":
        if args.cached:
            cache = load_boards(jira_dir)
        else:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_boards_api(jira_dir, api_project, client, str(component_field or "components"))

        if cache is None:
            print(
                "error: no cached boards found. Run jira-wb meta refresh --boards with Jira API configured.",
                file=sys.stderr,
            )
            return 1
        print(format_boards(cache), end="")
        return 0

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


def load_meta_boards(
    jira_dir: Path,
    project: object | None,
    component_field: object | None,
    jira_url: object | None,
    jira_email: object | None,
    jira_api_token: object | None,
) -> list[dict[str, object]]:
    cache = load_boards(jira_dir)
    if cache is None:
        if project is None or jira_url is None or jira_email is None or jira_api_token is None:
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
                "jira_url, jira_email, jira_api_token. Set them in ~/.config/jira-wb/config.toml.",
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
