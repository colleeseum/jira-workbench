"""SQLite index over the file-based `~/.local/share/jira-wb` store.

`index.db` is, in this pass, entirely disposable: every table it holds is
either 100% rebuildable from the raw `issue.json`/`meta/*.json` files (or a
live Jira refresh), or -- for `users` -- deterministically re-derived on
every fresh `connect()`. `rm index.db && jira-wb sync && jira-wb meta
refresh` fully reconstructs it. Raw ticket content (`issue.json`'s
description, `comments.json`) is deliberately never duplicated in here --
it stays file-only, human/AI-readable. See
`~/.claude/plans/gentle-mixing-book.md` for the full design rationale.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .sync import issue_path_sort_key, read_json, updated_at, utc_now
from .view import (
    as_dict,
    attach_board_fields,
    compute_issue_fields,
    display_name,
    load_cached_boards,
    observed_status_category_map,
    version_id_to_name_map,
)

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_SLUG = "jira"


def db_path(jira_dir: Path) -> Path:
    return jira_dir / "index.db"


def _migration_0001_initial(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            id           INTEGER PRIMARY KEY,
            slug         TEXT NOT NULL UNIQUE,
            kind         TEXT NOT NULL,
            display_name TEXT NOT NULL,
            config       TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id           INTEGER PRIMARY KEY,
            slug         TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS items (
            id              INTEGER PRIMARY KEY,
            source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            external_key    TEXT NOT NULL,
            project         TEXT,
            component       TEXT,
            title           TEXT,
            status          TEXT,
            status_category TEXT,
            item_type       TEXT,
            priority        TEXT,
            assignee        TEXT,
            fix_version     TEXT,
            epic_key        TEXT,
            epic_summary    TEXT,
            updated_at      TEXT,
            local_path      TEXT,
            extra           TEXT NOT NULL DEFAULT '{}',
            synced_at       TEXT NOT NULL,
            UNIQUE(source_id, external_key)
        );
        CREATE INDEX IF NOT EXISTS idx_items_project         ON items(project);
        CREATE INDEX IF NOT EXISTS idx_items_component       ON items(component);
        CREATE INDEX IF NOT EXISTS idx_items_status_category ON items(status_category);
        CREATE INDEX IF NOT EXISTS idx_items_assignee        ON items(assignee);

        -- Replaces meta/<PROJECT>/versions.json.
        CREATE TABLE IF NOT EXISTS versions (
            id           INTEGER PRIMARY KEY,
            source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            project      TEXT NOT NULL,
            external_id  TEXT,
            name         TEXT NOT NULL,
            released     INTEGER NOT NULL DEFAULT 0,
            archived     INTEGER NOT NULL DEFAULT 0,
            extra        TEXT NOT NULL DEFAULT '{}',
            fetched_at   TEXT NOT NULL,
            UNIQUE(source_id, project, external_id)
        );

        -- Replaces meta/<PROJECT>/components.json AND
        -- meta/<PROJECT>/customfield_<id>-options.json -- field_id is NULL
        -- for the native "components" field, set to the custom field id
        -- otherwise.
        CREATE TABLE IF NOT EXISTS components (
            id           INTEGER PRIMARY KEY,
            source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            project      TEXT NOT NULL,
            field_id     TEXT,
            external_id  TEXT,
            name         TEXT NOT NULL,
            extra        TEXT NOT NULL DEFAULT '{}',
            fetched_at   TEXT NOT NULL,
            UNIQUE(source_id, project, field_id, name)
        );

        -- Replaces meta/<PROJECT>/assignees.json.
        CREATE TABLE IF NOT EXISTS assignable_users (
            id            INTEGER PRIMARY KEY,
            source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            project       TEXT NOT NULL,
            external_id   TEXT,
            display_name  TEXT NOT NULL,
            fetch_source  TEXT,
            fetched_at    TEXT NOT NULL,
            UNIQUE(source_id, project, external_id)
        );

        -- Replaces meta/<PROJECT>/boards.json for kind='jira' rows ONLY --
        -- local boards stay in board_settings.json for this pass. Nothing
        -- writes kind='local' here yet.
        CREATE TABLE IF NOT EXISTS boards (
            id            INTEGER PRIMARY KEY,
            source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            project       TEXT NOT NULL,
            external_id   TEXT,
            name          TEXT NOT NULL,
            kind          TEXT NOT NULL DEFAULT 'jira',
            active        INTEGER NOT NULL DEFAULT 1,
            predicate     TEXT,
            extra         TEXT NOT NULL DEFAULT '{}',
            fetched_at    TEXT NOT NULL,
            UNIQUE(source_id, project, name)
        );
        """
    )


def _migration_0002_add_has_shadow(conn: sqlite3.Connection) -> None:
    """Whether a shadow.json exists for this item -- a derived/rebuildable
    flag (reindex_items recomputes it from disk for every item on every
    reindex), not authoritative state, same bucket as status/component/etc.
    Lets load_manifest_items skip a filesystem check for every item that
    doesn't have one instead of stat-ing all of them to find the ~14 (on
    the real dataset) that do."""
    conn.execute("ALTER TABLE items ADD COLUMN has_shadow INTEGER NOT NULL DEFAULT 0")


def _migration_0003_add_resolution(conn: sqlite3.Connection) -> None:
    """Resolution isn't part of compute_issue_fields' enrichment (nothing
    reads it off an already-enriched item) -- it exists purely so
    observed_field_options can list every resolution name ever observed,
    for the status-change form's dropdown, without its own full
    issue.json rescan on every Items-list page load."""
    conn.execute("ALTER TABLE items ADD COLUMN resolution TEXT")


def _migration_0004_add_board_fields(conn: sqlite3.Connection) -> None:
    """Board membership -- unlike everything else in items, this is kept
    fresh by proactively recomputing it (recompute_board_membership) at
    every place board data can change (a full reindex, a Jira boards
    refresh, or any local board CRUD -- see write_board_settings/
    refresh_boards_api), rather than by being computed at read time. It's
    still a derived/rebuildable pair of columns, same bucket as
    has_shadow -- recompute_board_membership can always regenerate them
    from the current items + boards data with no other source of truth."""
    conn.execute("ALTER TABLE items ADD COLUMN boards TEXT NOT NULL DEFAULT '[]'")
    conn.execute("ALTER TABLE items ADD COLUMN board_status TEXT NOT NULL DEFAULT '{}'")


# Additive only -- CREATE TABLE IF NOT EXISTS / ALTER TABLE ... ADD COLUMN,
# never DROP TABLE on an existing table. A schema change that can't be
# expressed additively gets a real data-preserving migration function when
# it lands, not a version-bump-triggered wipe.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_0001_initial,
    _migration_0002_add_has_shadow,
    _migration_0003_add_resolution,
    _migration_0004_add_board_fields,
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, migration in enumerate(MIGRATIONS, start=1):
        if index <= current:
            continue
        migration(conn)
        conn.execute(f"PRAGMA user_version = {index}")


def _bootstrap_rows(conn: sqlite3.Connection) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO sources (slug, kind, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO NOTHING
        """,
        (DEFAULT_SOURCE_SLUG, DEFAULT_SOURCE_SLUG, "Jira", now, now),
    )
    user_slug = os.environ.get("USER") or os.environ.get("USERNAME") or "local"
    conn.execute(
        """
        INSERT INTO users (slug, display_name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(slug) DO NOTHING
        """,
        (user_slug, user_slug, now),
    )
    conn.commit()


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def connect(jira_dir: Path) -> sqlite3.Connection:
    path = db_path(jira_dir)
    try:
        conn = _open(path)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError:
        if path.exists():
            corrupt_path = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            logger.warning(
                "index.db at %s failed to open (corrupt or not a database); "
                "moving aside to %s and rebuilding -- every table it holds is "
                "rebuildable in this release, so this is a full, no-loss recovery",
                path,
                corrupt_path,
            )
            path.rename(corrupt_path)
        conn = _open(path)
    _run_migrations(conn)
    _bootstrap_rows(conn)
    return conn


def source_id(conn: sqlite3.Connection, slug: str = DEFAULT_SOURCE_SLUG) -> int:
    row = conn.execute("SELECT id FROM sources WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise LookupError(f"unknown source slug: {slug!r}")
    return int(row["id"])


def item_id(jira_dir: Path, external_key: str, *, source_slug: str = DEFAULT_SOURCE_SLUG) -> int | None:
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        row = conn.execute(
            "SELECT id FROM items WHERE source_id = ? AND external_key = ?", (sid, external_key)
        ).fetchone()
        return int(row["id"]) if row is not None else None
    finally:
        conn.close()


def observed_status_categories(jira_dir: Path, *, source_slug: str = DEFAULT_SOURCE_SLUG) -> dict[str, str] | None:
    """Status display name -> Jira's real statusCategory.key, straight from
    the index instead of view.py's file-scan fallback (`local_issues` over
    every issue.json) -- items.status/status_category are computed at
    reindex time from each issue's own *un-shadowed* status (reindex_items
    never applies a shadow), the same "never shadow-merged" source
    view.py's observed_status_category_map promises. Returns None (not an
    empty dict) when this source has no indexed items at all, so the
    caller knows to fall back to the file scan instead of reporting a
    real dataset as having zero observed statuses."""
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        total = conn.execute("SELECT COUNT(*) FROM items WHERE source_id = ?", (sid,)).fetchone()[0]
        if total == 0:
            return None
        rows = conn.execute(
            """
            SELECT DISTINCT status, status_category FROM items
            WHERE source_id = ? AND status IS NOT NULL AND status_category IS NOT NULL
            """,
            (sid,),
        ).fetchall()
    finally:
        conn.close()
    return {row["status"]: row["status_category"] for row in rows}


_EXTRA_KEYS = (
    "labels",
    "fixVersions",
    "rawFixVersions",
    "devStatus",
    "assigneeAvatarUrl",
    "statusCategoryChangeDate",
    "issueId",
)


def reindex_items(
    jira_dir: Path,
    component_field: str | None = None,
    *,
    dev_status_field: str | None = None,
    source_slug: str = DEFAULT_SOURCE_SLUG,
) -> int:
    """Upsert every item currently found on disk, then delete only the rows
    whose key no longer exists there -- never a blanket DELETE before the
    inserts, since that would churn the surrogate id (and any future FK'd
    shadow/link) of every unchanged item in the batch. Idempotent and safe
    to re-run; unchanged items keep their `id`."""
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        status_categories = observed_status_category_map(jira_dir)
        version_names = version_id_to_name_map(jira_dir)
        components_dir = jira_dir / "components"
        count = 0
        conn.execute("CREATE TEMP TABLE reindex_seen (external_key TEXT PRIMARY KEY)")
        try:
            if components_dir.exists():
                for issue_path in sorted(components_dir.glob("*/*/issue.json"), key=issue_path_sort_key):
                    issue_dir = issue_path.parent
                    component = issue_dir.parent.name
                    key = issue_dir.name
                    issue = read_json(issue_path)
                    if not isinstance(issue, dict):
                        continue
                    fields = compute_issue_fields(
                        jira_dir,
                        issue,
                        component_field,
                        fallback_component=component,
                        status_categories=status_categories,
                        version_names=version_names,
                        dev_status_field=dev_status_field,
                    )
                    local_path = issue_dir.relative_to(jira_dir).as_posix()
                    extra = json.dumps({field: fields[field] for field in _EXTRA_KEYS})
                    has_shadow = 1 if (issue_dir / "shadow.json").exists() else 0
                    resolution = display_name(as_dict(issue.get("fields")).get("resolution")).strip()
                    conn.execute("INSERT INTO reindex_seen VALUES (?)", (key,))
                    conn.execute(
                        """
                        INSERT INTO items (
                            source_id, external_key, project, component, title, status,
                            status_category, item_type, priority, assignee, fix_version,
                            epic_key, epic_summary, updated_at, local_path, extra, has_shadow,
                            resolution, synced_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_id, external_key) DO UPDATE SET
                            project = excluded.project,
                            component = excluded.component,
                            title = excluded.title,
                            status = excluded.status,
                            status_category = excluded.status_category,
                            item_type = excluded.item_type,
                            priority = excluded.priority,
                            assignee = excluded.assignee,
                            fix_version = excluded.fix_version,
                            epic_key = excluded.epic_key,
                            epic_summary = excluded.epic_summary,
                            updated_at = excluded.updated_at,
                            local_path = excluded.local_path,
                            extra = excluded.extra,
                            has_shadow = excluded.has_shadow,
                            resolution = excluded.resolution,
                            synced_at = excluded.synced_at
                        """,
                        (
                            sid,
                            key,
                            fields["project"],
                            fields["component"],
                            fields["summary"],
                            fields["status"],
                            fields["statusCategory"],
                            fields["type"],
                            fields["priority"],
                            fields["assignee"],
                            fields["fixVersion"],
                            fields["epic"],
                            fields["epicSummary"],
                            updated_at(issue),
                            local_path,
                            extra,
                            has_shadow,
                            resolution or None,
                            utc_now(),
                        ),
                    )
                    count += 1
            conn.execute(
                """
                DELETE FROM items
                WHERE source_id = ?
                  AND NOT EXISTS (SELECT 1 FROM reindex_seen s WHERE s.external_key = items.external_key)
                """,
                (sid,),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute("DROP TABLE IF EXISTS reindex_seen")
    finally:
        conn.close()
    recompute_board_membership(jira_dir, source_slug=source_slug)
    return count


def recompute_board_membership(jira_dir: Path, *, source_slug: str = DEFAULT_SOURCE_SLUG) -> None:
    """Recomputes and stores every indexed item's board membership.

    Called after anything that can change it: a full reindex (see
    reindex_items above), a Jira boards refresh, or any local board CRUD
    (see refresh_boards_api/write_board_settings in metadata.py). Kept out
    of reindex_items' own per-issue loop on purpose -- board data
    (boards.json + board_settings.json) can change independently of a
    resync, which is exactly why board membership used to have to stay a
    read-time-only computation (see attach_board_fields's own docstring).
    Proactively recomputing it at every one of those choke points instead
    gets the same freshness guarantee without paying the cost on every
    single read.
    """
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        rows = conn.execute("SELECT * FROM items WHERE source_id = ?", (sid,)).fetchall()
        items = [_row_to_item(row) for row in rows]
        boards = load_cached_boards(jira_dir)
        updates = []
        for item in items:
            enriched = attach_board_fields(dict(item), item["key"], item.get("labels") or [], boards)
            updates.append(
                (json.dumps(enriched["boards"]), json.dumps(enriched["boardStatus"]), sid, item["key"])
            )
        conn.executemany(
            "UPDATE items SET boards = ?, board_status = ? WHERE source_id = ? AND external_key = ?", updates
        )
        conn.commit()
    finally:
        conn.close()


def set_item_has_shadow(
    jira_dir: Path, external_key: str, has_shadow: bool, *, source_slug: str = DEFAULT_SOURCE_SLUG
) -> None:
    """Keeps items.has_shadow accurate between reindexes -- the choke point
    is shadow.py's save_shadow/delete_shadow. A no-op if this item isn't
    indexed yet (nothing to update); the next reindex sets it correctly
    from disk regardless."""
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        conn.execute(
            "UPDATE items SET has_shadow = ? WHERE source_id = ? AND external_key = ?",
            (1 if has_shadow else 0, sid, external_key),
        )
        conn.commit()
    finally:
        conn.close()


# Only columns whose values are a plain 1:1 pass-through of one raw Jira
# field, with no per-project config or hierarchy fallback involved (unlike
# `component`, which depends on the project's configured component_field
# and a native-field fallback -- see hierarchy_component) -- safe for any
# caller to treat "distinct values of this column" as "distinct values of
# that Jira field," and also doubles as the SQL-injection allow-list for
# distinct_item_values below, since `column` is interpolated into the query.
DISTINCT_VALUE_COLUMNS = frozenset({"status", "assignee", "priority", "item_type", "resolution"})


def distinct_item_values(
    jira_dir: Path, column: str, *, source_slug: str = DEFAULT_SOURCE_SLUG
) -> list[str] | None:
    """Every distinct non-empty value of one of DISTINCT_VALUE_COLUMNS,
    straight from the index instead of view.py's file-scan fallback
    (`observed_field_options`'s `local_issues` loop over every issue.json).
    Returns None (not an empty list) when this source has no indexed items
    at all, so the caller knows to fall back to the file scan instead of
    reporting a real dataset as having zero options for a field it
    definitely has values for."""
    if column not in DISTINCT_VALUE_COLUMNS:
        raise ValueError(f"{column!r} is not in DISTINCT_VALUE_COLUMNS")
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        total = conn.execute("SELECT COUNT(*) FROM items WHERE source_id = ?", (sid,)).fetchone()[0]
        if total == 0:
            return None
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM items WHERE source_id = ? AND {column} IS NOT NULL AND {column} != ''",
            (sid,),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    extra = json.loads(row["extra"] or "{}")
    return {
        "key": row["external_key"],
        "project": row["project"],
        "component": row["component"],
        "summary": row["title"],
        "status": row["status"],
        "statusCategory": row["status_category"],
        "type": row["item_type"],
        "priority": row["priority"],
        "assignee": row["assignee"],
        "fixVersion": row["fix_version"],
        "epic": row["epic_key"],
        "epicSummary": row["epic_summary"],
        "updated": row["updated_at"],
        "path": row["local_path"],
        "hasShadow": bool(row["has_shadow"]),
        "boards": json.loads(row["boards"] or "[]"),
        "boardStatus": json.loads(row["board_status"] or "{}"),
        "labels": extra.get("labels", []),
        "fixVersions": extra.get("fixVersions", []),
        "rawFixVersions": extra.get("rawFixVersions"),
        "devStatus": extra.get("devStatus"),
        "assigneeAvatarUrl": extra.get("assigneeAvatarUrl", ""),
        "statusCategoryChangeDate": extra.get("statusCategoryChangeDate", ""),
        "issueId": extra.get("issueId"),
    }


# Fields safe to push into list_items' WHERE clause -- plain single-valued
# columns with no per-request re-resolution concern (unlike fixVersion,
# which must stay re-resolved against the live versions cache -- see
# resolve_fix_version_names -- so a stale stored name is never trusted for
# filtering; it stays Python-side, over whatever list_items returns).
_SQL_FILTERABLE_FIELDS = {"project": "project", "status": "status", "component": "component", "assignee": "assignee"}


def _items_where_clause(
    field_filters: dict[str, list[str]] | None, board: str | None
) -> tuple[str, list[Any]]:
    """Builds a WHERE fragment that's a safe *superset* of view.py's
    filter_items -- it only needs to narrow the candidate set cheaply,
    never to be the final word on what matches. Two things keep it safe:
    (1) has_shadow=1 rows are always let through regardless of whether
    they satisfy the conditions below, since a shadow can change any of
    these columns' true value without the stored column reflecting it yet
    -- filter_items (unchanged, and still the sole authority on the actual
    result) re-checks them for real afterward; (2) for a plain column
    match, a stored NULL/empty value is always let through too (rather
    than replicating filter_items' exact "(none)"/empty-bucket-sentinel
    logic here), so this never risks excluding a genuine match to save a
    SQL-injection-shaped case/sentinel-handling reimplementation -- it
    just means unassigned/uncategorized rows ride along as extra
    candidates on those filters, which filter_items then correctly prunes.
    """
    conditions = []
    params: list[Any] = []
    for field, column in _SQL_FILTERABLE_FIELDS.items():
        values = (field_filters or {}).get(field)
        if not values:
            continue
        placeholders = ", ".join("?" for _ in values)
        conditions.append(f"(LOWER({column}) IN ({placeholders}) OR {column} IS NULL OR {column} = '')")
        params.extend(value.strip().lower() for value in values)
    if board:
        conditions.append("EXISTS (SELECT 1 FROM json_each(boards) WHERE json_each.value = ?)")
        params.append(board)
    if not conditions:
        return "1 = 1", []
    return " AND ".join(conditions), params


def list_items(
    jira_dir: Path,
    *,
    field_filters: dict[str, list[str]] | None = None,
    board: str | None = None,
    source_slug: str = DEFAULT_SOURCE_SLUG,
) -> list[dict[str, Any]]:
    """Every item's promoted columns and `extra` flattened back into the
    same flat-dict shape `with_local_index_fields` returns.

    With no `field_filters`/`board`, returns every indexed item, same as
    always. When given, adds a safe-superset WHERE clause (see
    _items_where_clause) instead of fetching and decoding every row just
    to throw most of them away in Python -- callers still run the exact
    same filter_items logic on the (now much smaller) result, so passing
    filters here is purely a performance narrowing, never a correctness
    decision.
    """
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        extra_sql, extra_params = _items_where_clause(field_filters, board)
        rows = conn.execute(
            f"SELECT * FROM items WHERE source_id = ? AND (has_shadow = 1 OR ({extra_sql}))",
            (sid, *extra_params),
        ).fetchall()
        return [_row_to_item(row) for row in rows]
    finally:
        conn.close()


def count_items(jira_dir: Path, *, source_slug: str = DEFAULT_SOURCE_SLUG) -> int:
    """The true unfiltered total of indexed items -- a plain COUNT(*)
    instead of len(list_items(jira_dir)), which would decode every row's
    JSON columns just to throw the data away and keep the number."""
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        return conn.execute("SELECT COUNT(*) FROM items WHERE source_id = ?", (sid,)).fetchone()[0]
    finally:
        conn.close()


_FIELD_COUNT_COLUMNS = frozenset({"project", "status", "component", "assignee", "priority", "resolution", "item_type"})


def field_counts(
    jira_dir: Path,
    column: str,
    *,
    projects: list[str] | None = None,
    board: str | None = None,
    empty_bucket: str = "(none)",
    source_slug: str = DEFAULT_SOURCE_SLUG,
) -> list[tuple[str, int]]:
    """SQL GROUP BY count of one of _FIELD_COUNT_COLUMNS, optionally scoped
    to a set of projects and/or board membership -- backs the Filters
    panel's option lists (view.py's distinct_field_values/component_counts
    did this by materializing and JSON-decoding every enriched item in
    Python first; this answers straight from the index, never decoding a
    row's JSON columns at all).

    Not shadow-corrected: the row-level `has_shadow` union that keeps
    list_items' actual filtering exactly correct doesn't apply here -- a
    shadow-edited item's true value for `column` could differ from what's
    stored until the next reindex, which could make one bucket's count off
    by one out of the ~14 shadow items on a real dataset. Accepted
    trade-off for a purely informational count display; the rows actually
    shown on the page are still exactly correct via list_items/filter_items.
    """
    if column not in _FIELD_COUNT_COLUMNS:
        raise ValueError(f"{column!r} is not in _FIELD_COUNT_COLUMNS")
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        conditions = ["source_id = ?"]
        params: list[Any] = [sid]
        if projects:
            placeholders = ", ".join("?" for _ in projects)
            conditions.append(f"project IN ({placeholders})")
            params.extend(projects)
        if board:
            conditions.append("EXISTS (SELECT 1 FROM json_each(boards) WHERE json_each.value = ?)")
            params.append(board)
        where = " AND ".join(conditions)
        rows = conn.execute(
            f"SELECT {column} AS value, COUNT(*) AS n FROM items WHERE {where} GROUP BY {column}", params
        ).fetchall()
    finally:
        conn.close()
    counts: dict[str, int] = {}
    for row in rows:
        value = row["value"] or empty_bucket
        counts[value] = counts.get(value, 0) + row["n"]
    return sorted(counts.items(), key=lambda pair: pair[0].lower())


def distinct_projects_for_board(jira_dir: Path, board: str, *, source_slug: str = DEFAULT_SOURCE_SLUG) -> set[str]:
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        rows = conn.execute(
            """
            SELECT DISTINCT project FROM items
            WHERE source_id = ? AND EXISTS (SELECT 1 FROM json_each(boards) WHERE json_each.value = ?)
            """,
            (sid, board),
        ).fetchall()
    finally:
        conn.close()
    return {row["project"] for row in rows if row["project"]}


def project_versions(
    jira_dir: Path, project: str, *, source_slug: str = DEFAULT_SOURCE_SLUG
) -> tuple[list[dict[str, Any]], str] | None:
    """The raw version dicts and their shared fetched_at for one project,
    or None if this project has no rows yet (not refreshed since index.db
    existed) -- the caller (metadata.py's load_versions) falls back to the
    JSON file in that case."""
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        rows = conn.execute(
            "SELECT extra, fetched_at FROM versions WHERE source_id = ? AND project = ?", (sid, project)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    versions = [json.loads(row["extra"]) for row in rows]
    fetched_at = max(row["fetched_at"] for row in rows)
    return versions, fetched_at


def project_names_with_versions(jira_dir: Path, *, source_slug: str = DEFAULT_SOURCE_SLUG) -> set[str]:
    conn = connect(jira_dir)
    try:
        sid = source_id(conn, source_slug)
        rows = conn.execute("SELECT DISTINCT project FROM versions WHERE source_id = ?", (sid,)).fetchall()
    finally:
        conn.close()
    return {row["project"] for row in rows}


def _upsert_rows(
    conn: sqlite3.Connection,
    table: str,
    conflict_columns: tuple[str, ...],
    update_columns: tuple[str, ...],
    rows: Iterable[tuple[Any, ...]],
    all_columns: tuple[str, ...],
) -> None:
    placeholders = ", ".join("?" for _ in all_columns)
    columns_sql = ", ".join(all_columns)
    conflict_sql = ", ".join(conflict_columns)
    update_sql = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    sql = (
        f"INSERT INTO {table} ({columns_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_sql}) DO UPDATE SET {update_sql}"
    )
    conn.executemany(sql, list(rows))


def save_versions(
    jira_dir: Path, project: str, versions: list[dict[str, Any]], fetched_at: str, *, source_slug: str = DEFAULT_SOURCE_SLUG
) -> None:
    conn = connect(jira_dir)
    try:
        _upsert_versions(conn, source_id(conn, source_slug), project, versions, fetched_at)
    finally:
        conn.close()


def _upsert_versions(
    conn: sqlite3.Connection, sid: int, project: str, versions: list[dict[str, Any]], fetched_at: str
) -> None:
    """`versions` are the raw (already normalize_versions/sort_versions'd)
    version dicts exactly as Jira's API returns them. The whole dict is
    stored in `extra` so `load_versions` can reconstruct a byte-identical
    cache entry (any field a caller reads -- releaseDate, description,
    whatever -- round-trips even though only name/released/archived are
    promoted to real columns). `fetched_at` is passed in rather than
    computed here so a caller writing both the JSON file and this table in
    the same refresh can use one identical timestamp for both."""
    rows = [
        (
            sid,
            project,
            version.get("id"),
            version.get("name") or version.get("value") or version.get("id") or "",
            1 if version.get("released") else 0,
            1 if version.get("archived") else 0,
            json.dumps(version),
            fetched_at,
        )
        for version in versions
    ]
    _upsert_rows(
        conn,
        "versions",
        ("source_id", "project", "external_id"),
        ("name", "released", "archived", "extra", "fetched_at"),
        rows,
        ("source_id", "project", "external_id", "name", "released", "archived", "extra", "fetched_at"),
    )
    # external_id can be NULL for a version with no id at all (shouldn't
    # happen for real Jira data, but test fixtures sometimes omit it) --
    # SQLite treats every NULL as distinct for "NOT IN", so those rows are
    # deliberately left alone here rather than being (mis-)matched as
    # stale; only rows that HAD a real id and are no longer present get
    # cleaned up.
    seen_ids = [version.get("id") for version in versions if version.get("id") is not None]
    placeholders = ", ".join("?" for _ in seen_ids) or "''"
    conn.execute(
        f"""
        DELETE FROM versions
        WHERE source_id = ? AND project = ? AND external_id IS NOT NULL
          AND external_id NOT IN ({placeholders})
        """,
        (sid, project, *seen_ids),
    )
    conn.commit()


def backup(jira_dir: Path, dest: Path) -> None:
    """Uses SQLite's own online backup API, not a file copy -- with
    journal_mode=WAL, copying only index.db while another process has it
    open can miss recently-committed data still sitting in the -wal file.
    The backup API is WAL-aware and safe to run concurrently with an
    active connection."""
    source_conn = connect(jira_dir)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target_conn = sqlite3.connect(dest)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()
