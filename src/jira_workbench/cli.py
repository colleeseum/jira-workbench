from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .acli import AcliError, AcliRunner
from .config import DEFAULT_CONFIG_PATH, ConfigError, choose, load_config
from .server import serve
from .shadow import (
    ShadowError,
    add_comment,
    commit_shadow,
    push_shadows,
    render_diff,
    set_field,
    shadow_status,
)
from .sync import SyncConfig, SyncError, sync_project
from .view import ViewError, format_work_item, interactive_view


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
    sync_parser.add_argument("--acli", default=os.environ.get("ACLI"))

    serve_parser = subparsers.add_parser("serve", help="Serve the local Jira browser")
    serve_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)

    view_parser = subparsers.add_parser("view", help="View locally synced Jira work items")
    view_parser.add_argument("key", nargs="?")
    view_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    view_parser.add_argument("--component-field", default=os.environ.get("JIRA_COMPONENT_FIELD"))
    view_mode = view_parser.add_mutually_exclusive_group()
    view_mode.add_argument("--original", action="store_true", help="Show the last synced Jira issue")
    view_mode.add_argument("--diff", action="store_true", help="Show the local shadow diff")

    shadow_parser = subparsers.add_parser("shadow", help="Manage local-only Jira changes")
    shadow_parser.add_argument("--jira-dir", default=os.environ.get("JIRA_DIR"))
    shadow_subparsers = shadow_parser.add_subparsers(dest="shadow_command")

    set_parser = shadow_subparsers.add_parser("set", help="Set a local-only field value")
    set_parser.add_argument("key")
    set_parser.add_argument("field")
    set_parser.add_argument("value")

    comment_parser = shadow_subparsers.add_parser("comment", help="Add a local-only comment")
    comment_parser.add_argument("key")
    comment_parser.add_argument("body")

    diff_parser = shadow_subparsers.add_parser("diff", help="Show local shadow diffs")
    diff_parser.add_argument("keys", nargs="*")

    commit_parser = shadow_subparsers.add_parser("commit", help="Mark local shadow changes ready")
    commit_parser.add_argument("keys", nargs="+")

    shadow_subparsers.add_parser("status", help="List local shadow changes")

    push_parser = shadow_subparsers.add_parser("push", help="Push committed local shadow changes")
    push_parser.add_argument("keys", nargs="*")
    push_parser.add_argument("--acli", default=os.environ.get("ACLI"))
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
        component_field = choose(args.component_field, config.component_field)
        jira_dir = choose(args.jira_dir, config.jira_dir)
        acli = choose(args.acli, config.acli)
        missing = [
            name
            for name, value in (
                ("project", project),
                ("component_field", component_field),
                ("jira_dir", jira_dir),
                ("acli", acli),
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
            result = sync_project(
                SyncConfig(
                    project=str(project),
                    component_field=str(component_field),
                    jira_dir=Path(str(jira_dir)),
                ),
                AcliRunner(str(acli)),
                progress=lambda message: print(message, file=sys.stderr),
            )
        except AcliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except SyncError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            f"Synced {result.work_item_count} work items "
            f"({result.changed_count} changed, {result.skipped_count} unchanged)."
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
        component_field = choose(args.component_field, config.component_field)
        if jira_dir is None:
            print(
                "error: missing required configuration: jira_dir. "
                "Set it in ~/.jira-wb.conf or pass --jira-dir.",
                file=sys.stderr,
            )
            return 2
        try:
            if args.key:
                mode = "diff" if args.diff else "original" if args.original else "shadow"
                print(
                    format_work_item(
                        Path(str(jira_dir)),
                        args.key,
                        component_field=str(component_field) if component_field else None,
                        mode=mode,
                    )
                )
            else:
                if args.diff or args.original:
                    print("error: --diff and --original require a work item key", file=sys.stderr)
                    return 2
                interactive_view(Path(str(jira_dir)), component_field=str(component_field) if component_field else None)
        except ViewError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "shadow":
        jira_dir = choose(args.jira_dir, config.jira_dir)
        if jira_dir is None:
            print(
                "error: missing required configuration: jira_dir. "
                "Set it in ~/.jira-wb.conf or pass --jira-dir.",
                file=sys.stderr,
            )
            return 2
        try:
            return run_shadow(args, Path(str(jira_dir)), config.acli)
        except AcliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ShadowError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    parser.print_help()
    return 0


def run_shadow(args: argparse.Namespace, jira_dir: Path, configured_acli: str | None) -> int:
    if args.shadow_command == "set":
        set_field(jira_dir, args.key, args.field, args.value)
        print(f"{args.key}: set local {args.field}")
        return 0

    if args.shadow_command == "comment":
        add_comment(jira_dir, args.key, args.body)
        print(f"{args.key}: added local comment")
        return 0

    if args.shadow_command == "diff":
        keys = args.keys or [row["key"] for row in shadow_status(jira_dir)]
        if not keys:
            print("no local shadow changes")
            return 0
        print("\n\n".join(render_diff(jira_dir, key) for key in keys))
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
        acli = choose(args.acli, configured_acli)
        if acli is None:
            print(
                "error: missing required configuration: acli. "
                "Set it in ~/.jira-wb.conf or pass --acli.",
                file=sys.stderr,
            )
            return 2
        result = push_shadows(
            jira_dir,
            args.keys,
            AcliRunner(str(acli)),
            dry_run=args.dry_run,
            progress=lambda message: print(message, file=sys.stderr),
        )
        print(f"push summary: pushed={result.pushed} skipped={result.skipped} blocked={result.blocked}")
        return 1 if result.blocked else 0

    print("error: missing shadow command", file=sys.stderr)
    return 2
