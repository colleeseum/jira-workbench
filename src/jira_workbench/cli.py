from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .config import DEFAULT_CONFIG_PATH, ConfigError, choose, load_config, resolve_jira_dir, secure_config_permissions
from .metadata import (
    DEFAULT_METADATA_TTL_SECONDS,
    JiraApiConfig,
    MetadataError,
    add_component_api,
    check_jira_api_config,
    delete_version_api,
    format_boards,
    format_doctor_checks,
    format_components as format_meta_components,
    format_component_cache,
    format_versions,
    is_project_read_only,
    jira_api_client,
    load_all_boards,
    load_all_components,
    load_all_versions,
    load_boards,
    load_component_summary,
    load_components,
    load_versions,
    refresh_assignees_api,
    refresh_boards_api,
    refresh_component_field_options_api,
    refresh_components_api,
    refresh_versions_api,
    seed_project_registry,
    write_project_registry,
)
from .issue import IssueError, build_create_fields, create_issue, fetch_issue_type_fields, resolve_reporter
from .server import serve
from .service import (
    add_meta_component_field_option,
    add_meta_version,
    api_client_from_config,
    archive_meta_version,
    meta_component_field_options_output,
    release_meta_version,
    rename_meta_version,
)
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
    project_filter: str | tuple[str, ...] | None = None,
    status_filter: str | tuple[str, ...] | None = None,
    component: str | tuple[str, ...] | None = None,
    fix_version: str | tuple[str, ...] | None = None,
    assignee: str | tuple[str, ...] | None = None,
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
    version_filters_by_component: dict[str, dict[str, str]] | None = None,
    preview_lines: int | None = None,
    hide_done_after_days: int | None = None,
    config_path: Path | None = None,
    nerd_font: bool = False,
    dev_status_field: str | None = None,
) -> None:
    from .tui.app import run_view as run_textual_view

    run_textual_view(
        jira_dir,
        component_field=component_field,
        project_filter=project_filter,
        status_filter=status_filter,
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
        version_filters_by_component=version_filters_by_component,
        preview_lines=preview_lines,
        hide_done_after_days=hide_done_after_days,
        config_path=config_path,
        nerd_font=nerd_font,
        dev_status_field=dev_status_field,
    )


_IN_PLACE_PROGRESS_PREFIXES = (
    "[3/6] Syncing changed issues... ",
    "[4/6] Backfilling parents excluded by the history cutoff... ",
)


def sync_progress_printer(stream: object = sys.stderr, *, project: str | None = None) -> Callable[[str], None]:
    in_place = False
    last_len = 0
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    prefix = f"{project} " if project else ""

    def progress(message: str) -> None:
        nonlocal in_place, last_len
        full_message = f"{prefix}{message}"
        if is_tty and message.startswith(_IN_PLACE_PROGRESS_PREFIXES):
            padding = " " * max(0, last_len - len(full_message))
            getattr(stream, "write")(f"\r{full_message}{padding}")
            getattr(stream, "flush")()
            in_place = True
            last_len = len(full_message)
            return
        if in_place:
            getattr(stream, "write")("\n")
            in_place = False
            last_len = 0
        print(full_message, file=stream)

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
    sync_parser.add_argument(
        "--history-months",
        type=int,
        default=os.environ.get("JIRA_SYNC_HISTORY_MONTHS"),
        help=(
            "Only sync Done issues that have been in that status for this many months or less "
            "(currently-open issues are always synced regardless of age). Overrides "
            "sync_history_months/[[projects]] history_months in config for this run, for every "
            "project being synced. Unset means unbounded, same as today."
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
    meta_refresh.add_argument(
        "--assignees", action="store_true", help="Refresh the project's currently assignable (active) users"
    )

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

    db_parser = subparsers.add_parser("db", help="Manage the local SQLite index (index.db)")
    db_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    db_parser.add_argument("--component-field", default=os.environ.get("JIRA_COMPONENT_FIELD"))
    db_subparsers = db_parser.add_subparsers(dest="db_command")

    db_subparsers.add_parser(
        "reindex", help="Rebuild index.db's items table from the locally synced issue.json files"
    )

    db_backup = db_subparsers.add_parser("backup", help="Back up index.db via SQLite's online backup API")
    db_backup.add_argument("dest", help="Destination path for the backup copy")

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

    # Seed the on-disk read-only/default registry for any project config.toml
    # declares that the registry has never recorded at all -- otherwise
    # is_project_read_only() defaults such a project to "fully permissive"
    # until a full `sync` happens to run for it first. This only fills in
    # missing keys; it never touches an entry the registry already has
    # (that stays authoritative, and only `sync`'s full write_project_registry
    # call is allowed to change it -- see is_project_read_only/shadow.py).
    seed_project_registry(resolve_jira_dir(getattr(args, "jira_dir", None), config.jira_dir), config.resolved_projects())

    if args.command == "sync":
        jira_dir = resolve_jira_dir(args.jira_dir, config.jira_dir)
        jira_url = choose(args.jira_url, config.jira_url)
        jira_email = choose(args.jira_email, config.jira_email)
        jira_api_token = choose(args.jira_api_token, config.jira_api_token)

        # An explicit --project always means "just sync this one," matching
        # the pre-multi-project CLI. Otherwise sync every project declared
        # in config (a single flat `project = "..."` resolves to one via
        # resolved_projects()'s own backward-compat fallback).
        if args.project:
            projects_to_sync = [str(args.project)]
        else:
            projects_to_sync = [settings.key for settings in config.resolved_projects()]

        if not projects_to_sync:
            print(
                "error: missing required configuration: "
                "project. Set it in ~/.config/jira-wb/config.toml or pass --project.",
                file=sys.stderr,
            )
            return 2

        if args.history_months is not None and int(args.history_months) <= 0:
            print("error: --history-months must be a positive integer", file=sys.stderr)
            return 2

        exit_code = 0
        for project in projects_to_sync:
            component_field = choose(args.component_field, config.effective_component_field(project)) or "components"
            try:
                api_project, api_client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            except MetadataError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            history_months = (
                int(args.history_months) if args.history_months is not None else config.effective_history_months(project)
            )
            try:
                result = sync_project(
                    SyncConfig(
                        project=api_project,
                        component_field=str(component_field),
                        jira_dir=jira_dir,
                        force=args.force,
                        history_months=history_months,
                    ),
                    api_client,
                    progress=sync_progress_printer(
                        sys.stderr, project=project if len(projects_to_sync) > 1 else None
                    ),
                )
            except SyncError as exc:
                print(f"error: syncing {project}: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            prefix = "Synced" if len(projects_to_sync) == 1 else f"Synced {project}:"
            backfilled_note = (
                f", {result.backfilled_parent_count} parent(s) backfilled" if result.backfilled_parent_count else ""
            )
            print(
                f"{prefix} {result.work_item_count} work items "
                f"({result.changed_count} changed, {result.skipped_count} unchanged, "
                f"{result.version_count} versions cached{backfilled_note})."
            )

        # Keep the read-only/default registry current even when only a
        # single project was actively re-synced (see metadata.py's
        # write_project_registry/is_project_read_only).
        write_project_registry(jira_dir, config.resolved_projects())
        return exit_code

    if args.command == "serve":
        jira_dir = resolve_jira_dir(args.jira_dir, config.jira_dir)
        host = choose(args.host, config.host)
        port = choose(args.port, config.port)
        missing = [name for name, value in (("serve.host", host), ("serve.port", port)) if value is None]
        if missing:
            print(
                "error: missing required configuration: "
                f"{', '.join(missing)}. Set them in ~/.config/jira-wb/config.toml or pass flags.",
                file=sys.stderr,
            )
            return 2
        serve(jira_dir, str(host), int(port), config, expanded_config_path)
        return 0

    if args.command == "view":
        jira_dir = resolve_jira_dir(args.jira_dir, config.jira_dir)
        # config.project is only the legacy flat `project = "SAT"` key -- a
        # [[projects]] config (see resolved_projects) needs this instead, or
        # the TUI thinks no project is configured at all (breaks Meta boards/
        # versions and anything else keyed off self.app.project), even
        # though sync already resolved a real default project just fine.
        default_project = config.default_project_key()
        component_field = choose(args.component_field, config.effective_component_field(default_project)) or "components"
        view_project = config.view_project
        view_status = config.view_status
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
        try:
            if args.components:
                print(format_components(jira_dir), end="")
            elif args.key:
                mode = "diff" if args.diff else "original" if args.original else "shadow"
                if args.read_only:
                    print(
                        format_work_item(
                            jira_dir,
                            args.key,
                            component_field=str(component_field) if component_field else None,
                            mode=mode,
                        )
                    )
                else:
                    open_interactive_view(
                        jira_dir,
                        component_field=str(component_field) if component_field else None,
                        project_filter=view_project,
                        status_filter=view_status,
                        component=view_component,
                        fix_version=view_fix_version,
                        assignee=view_assignee,
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
                        project=default_project,
                        versions_filter=str(config.versions_filter) if config.versions_filter else None,
                        version_filters_by_component=config.version_filters_by_component,
                        preview_lines=config.view_preview_lines,
                        hide_done_after_days=config.view_hide_done_after_days,
                        config_path=config_path,
                        nerd_font=view_nerd_font,
                        dev_status_field=str(config.view_dev_status_field) if config.view_dev_status_field else None,
                    )
            else:
                if args.diff or args.original:
                    print("error: --diff and --original require a work item key", file=sys.stderr)
                    return 2
                open_interactive_view(
                    jira_dir,
                    component_field=str(component_field) if component_field else None,
                    project_filter=view_project,
                    status_filter=view_status,
                    component=view_component,
                    fix_version=view_fix_version,
                    assignee=view_assignee,
                    board=str(view_board) if view_board else None,
                    board_scope=str(view_board_scope) if view_board_scope else None,
                    pattern=str(view_filter) if view_filter else None,
                    active=view_active,
                    jira_url=str(config.jira_url) if config.jira_url else None,
                    jira_email=str(config.jira_email) if config.jira_email else None,
                    jira_api_token=str(config.jira_api_token) if config.jira_api_token else None,
                    swimlane=str(view_swimlane),
                    project=default_project,
                    versions_filter=str(config.versions_filter) if config.versions_filter else None,
                    version_filters_by_component=config.version_filters_by_component,
                    preview_lines=config.view_preview_lines,
                    hide_done_after_days=config.view_hide_done_after_days,
                    config_path=config_path,
                    nerd_font=view_nerd_font,
                    dev_status_field=str(config.view_dev_status_field) if config.view_dev_status_field else None,
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
        jira_dir = resolve_jira_dir(args.jira_dir, config.jira_dir)
        # config.project is only the legacy flat `project = "SAT"` key -- a
        # [[projects]] config (see resolved_projects) needs this instead,
        # same fix as the view command's own default_project resolution.
        project = choose(args.project, config.default_project_key())
        jira_url = choose(args.jira_url, config.jira_url)
        jira_email = choose(args.jira_email, config.jira_email)
        jira_api_token = choose(args.jira_api_token, config.jira_api_token)
        component_field = choose(args.component_field, config.effective_component_field(project)) or "components"
        issue_type = choose(args.type, config.issue_default_type)
        try:
            return run_issue(
                args,
                jira_dir,
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
        jira_dir = resolve_jira_dir(args.jira_dir, config.jira_dir)
        try:
            return run_shadow(args, jira_dir, config)
        except MetadataError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ShadowError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.command == "meta":
        jira_dir = resolve_jira_dir(args.jira_dir, config.jira_dir)
        # Same fix as run_issue above -- config.project is only the legacy
        # flat key, [[projects]] needs default_project_key() instead.
        project = choose(args.project, config.default_project_key())
        jira_url = choose(args.jira_url, config.jira_url)
        jira_email = choose(args.jira_email, config.jira_email)
        jira_api_token = choose(args.jira_api_token, config.jira_api_token)
        component_field = choose(args.component_field, config.effective_component_field(project)) or "components"
        versions_filter = config.versions_filter
        try:
            try:
                return run_meta(
                    args,
                    jira_dir,
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

    if args.command == "db":
        jira_dir = resolve_jira_dir(args.jira_dir, config.jira_dir)
        project = config.default_project_key()
        component_field = choose(args.component_field, config.effective_component_field(project)) or "components"
        return run_db(args, jira_dir, component_field)

    parser.print_help()
    return 0


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
        if is_project_read_only(jira_dir, api_project):
            raise IssueError(f"cannot create an issue: project {api_project} is read-only")
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

    if project is not None:
        project = str(project)

    mutation_commands = {
        "version-add",
        "version-rename",
        "version-release",
        "version-archive",
        "version-delete",
        "native-component-add",
        "component-field-add",
    }
    if args.meta_command in mutation_commands and project is not None and is_project_read_only(jira_dir, project):
        print(f"error: cannot modify metadata: project {project} is read-only", file=sys.stderr)
        return 2

    if args.meta_command == "refresh":
        # No flags at all means "refresh everything" -- naming one or more
        # flags switches to refreshing only those, same as before. Without
        # this, a bare `meta refresh` silently refreshed versions only,
        # which reads as "did nothing" for components/boards/assignees.
        no_flags_given = not (args.versions or args.components or args.boards or args.assignees)
        if args.versions or no_flags_given:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_versions_api(jira_dir, api_project, client)
            print(f"refreshed {len(cache.get('versions', []))} versions")
        if args.components or no_flags_given:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            if component_field and str(component_field) != "components":
                cache = refresh_component_field_options_api(jira_dir, api_project, str(component_field), client)
                print(f"refreshed {len(cache.get('options', []))} component field options")
            else:
                cache = refresh_components_api(jira_dir, api_project, client)
                print(f"refreshed {len(cache.get('components', []))} native components")
        if args.boards or no_flags_given:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_boards_api(jira_dir, api_project, client, str(component_field or "components"))
            print(f"refreshed {len(cache.get('boards', []))} boards")
        if args.assignees or no_flags_given:
            api_project, client = api_client_from_config(project, jira_url, jira_email, jira_api_token)
            cache = refresh_assignees_api(jira_dir, api_project, client)
            print(f"refreshed {len(cache.get('assignees', []))} assignable users")
            if cache.get("source") == "role-api-fallback":
                print(
                    "warning: Jira's internal Access-page API was unavailable -- fell back to the "
                    "classic role-based API, which can still include people who no longer actually "
                    "have project access (see config.toml's exclude_assignees for a manual override)",
                    file=sys.stderr,
                )
        return 0

    if args.meta_command == "versions":
        if args.cached:
            if project is not None:
                cache = load_versions(jira_dir, project)
            else:
                merged = load_all_versions(jira_dir)
                cache = {"versions": merged} if merged else None
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
        components = load_component_summary(jira_dir, str(project) if project is not None else None)
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
            if project is not None:
                cache = load_boards(jira_dir, project)
            else:
                merged = load_all_boards(jira_dir)
                cache = {"boards": merged} if merged else None
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
        print(add_meta_version(jira_dir, args.name, project, jira_url, jira_email, jira_api_token), end="")
        return 0

    if args.meta_command == "version-rename":
        print(
            rename_meta_version(jira_dir, args.version, args.new_name, project, jira_url, jira_email, jira_api_token),
            end="",
        )
        return 0

    if args.meta_command == "version-release":
        print(
            release_meta_version(
                jira_dir, args.version, args.release_date, project, jira_url, jira_email, jira_api_token
            ),
            end="",
        )
        return 0

    if args.meta_command == "version-archive":
        print(
            archive_meta_version(jira_dir, args.version, project, jira_url, jira_email, jira_api_token),
            end="",
        )
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
        cache = None
        if args.cached:
            if project is not None:
                cache = load_components(jira_dir, project)
            else:
                merged = load_all_components(jira_dir)
                cache = {"components": merged} if merged else None
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


def run_db(args: argparse.Namespace, jira_dir: Path, component_field: str) -> int:
    from . import db

    if args.db_command == "reindex":
        count = db.reindex_items(jira_dir, component_field)
        print(f"reindexed {count} items into {db.db_path(jira_dir)}")
        return 0

    if args.db_command == "backup":
        dest = Path(str(args.dest))
        db.backup(jira_dir, dest)
        print(f"backed up index.db to {dest}")
        return 0

    print("error: missing db command", file=sys.stderr)
    return 2


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
            component_field=str(config.effective_component_field(config.default_project_key()) or "components"),
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
