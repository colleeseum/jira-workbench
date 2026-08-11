from __future__ import annotations

import sqlite3
from pathlib import Path

from jira_workbench.db import (
    MIGRATIONS,
    backup,
    connect,
    count_items,
    db_path,
    distinct_item_values,
    distinct_projects_for_board,
    field_counts,
    item_id,
    list_items,
    reindex_items,
    set_item_has_shadow,
    source_id,
)
from jira_workbench.sync import write_json


def _write_issue(jira_dir: Path, key: str, component: str, **fields: object) -> None:
    write_json(
        jira_dir / f"components/{component}/{key}/issue.json",
        {
            "key": key,
            "fields": {
                "summary": f"summary for {key}",
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                **fields,
            },
        },
    )


def test_connect_creates_schema_and_bootstraps_default_rows(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"sources", "users", "items", "versions", "components", "assignable_users", "boards"} <= tables

        sources = conn.execute("SELECT slug FROM sources").fetchall()
        assert [row["slug"] for row in sources] == ["jira"]

        users = conn.execute("SELECT slug FROM users").fetchall()
        assert len(users) == 1

        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        conn.close()

    assert db_path(tmp_path) == tmp_path / "index.db"


def test_connect_is_idempotent_and_never_duplicates_bootstrap_rows(tmp_path: Path) -> None:
    connect(tmp_path).close()
    connect(tmp_path).close()
    conn = connect(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        conn.close()


def test_connect_recovers_from_a_corrupt_database_file(tmp_path: Path) -> None:
    path = db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")

    conn = connect(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    finally:
        conn.close()

    corrupt_copies = list(tmp_path.glob("index.db.corrupt-*"))
    assert len(corrupt_copies) == 1


def test_reindex_items_populates_items_from_disk(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart", assignee={"displayName": "Serge Colle"})
    _write_issue(tmp_path, "SAT-2", "helm-chart")

    count = reindex_items(tmp_path, "customfield_10071")

    assert count == 2
    items = {item["key"]: item for item in list_items(tmp_path)}
    assert set(items) == {"SAT-1", "SAT-2"}
    assert items["SAT-1"]["assignee"] == "Serge Colle"
    assert items["SAT-1"]["component"] == "helm-chart"
    assert items["SAT-1"]["status"] == "To Do"


def test_reindex_items_preserves_id_for_an_unchanged_row_across_repeated_runs(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-123", "helm-chart")
    reindex_items(tmp_path)

    first_id = item_id(tmp_path, "SAT-123")
    assert first_id is not None

    reindex_items(tmp_path)

    assert item_id(tmp_path, "SAT-123") == first_id


def test_reindex_items_updates_changed_fields_in_place_without_a_new_id(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    reindex_items(tmp_path)
    first_id = item_id(tmp_path, "SAT-1")

    _write_issue(tmp_path, "SAT-1", "helm-chart", summary="a new summary")
    reindex_items(tmp_path)

    assert item_id(tmp_path, "SAT-1") == first_id
    items = {item["key"]: item for item in list_items(tmp_path)}
    assert items["SAT-1"]["summary"] == "a new summary"


def test_reindex_items_deletes_rows_for_issues_removed_from_disk(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "SAT-2", "helm-chart")
    reindex_items(tmp_path)
    assert item_id(tmp_path, "SAT-2") is not None

    import shutil

    shutil.rmtree(tmp_path / "components/helm-chart/SAT-2")
    reindex_items(tmp_path)

    assert item_id(tmp_path, "SAT-2") is None
    assert item_id(tmp_path, "SAT-1") is not None


def test_reindex_items_leaves_the_bootstrapped_users_row_untouched(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    conn = connect(tmp_path)
    try:
        before = conn.execute("SELECT id, slug, created_at FROM users").fetchall()
    finally:
        conn.close()

    reindex_items(tmp_path)
    reindex_items(tmp_path)

    conn = connect(tmp_path)
    try:
        after = conn.execute("SELECT id, slug, created_at FROM users").fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in before] == [tuple(row) for row in after]


def test_source_id_raises_for_unknown_slug(tmp_path: Path) -> None:
    conn = connect(tmp_path)
    try:
        assert source_id(conn, "jira") >= 1
        try:
            source_id(conn, "does-not-exist")
        except LookupError:
            pass
        else:
            raise AssertionError("expected LookupError")
    finally:
        conn.close()


def test_backup_produces_a_readable_independent_copy(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    reindex_items(tmp_path)

    dest = tmp_path / "backups" / "index-copy.db"
    backup(tmp_path, dest)

    assert dest.exists()
    conn = sqlite3.connect(dest)
    try:
        rows = conn.execute("SELECT external_key FROM items").fetchall()
    finally:
        conn.close()
    assert rows == [("SAT-1",)]


def test_reindex_items_sets_has_shadow_from_the_shadow_file_on_disk(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "SAT-2", "helm-chart")
    write_json(tmp_path / "components/helm-chart/SAT-1/shadow.json", {"key": "SAT-1", "fields": {}})

    reindex_items(tmp_path)

    items = {item["key"]: item for item in list_items(tmp_path)}
    assert items["SAT-1"]["hasShadow"] is True
    assert items["SAT-2"]["hasShadow"] is False


def test_reindex_items_clears_has_shadow_once_the_shadow_file_is_gone(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    shadow_path = tmp_path / "components/helm-chart/SAT-1/shadow.json"
    write_json(shadow_path, {"key": "SAT-1", "fields": {}})
    reindex_items(tmp_path)
    assert {item["key"]: item for item in list_items(tmp_path)}["SAT-1"]["hasShadow"] is True

    shadow_path.unlink()
    reindex_items(tmp_path)

    assert {item["key"]: item for item in list_items(tmp_path)}["SAT-1"]["hasShadow"] is False


def test_set_item_has_shadow_updates_an_already_indexed_row_without_a_reindex(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    reindex_items(tmp_path)
    assert {item["key"]: item for item in list_items(tmp_path)}["SAT-1"]["hasShadow"] is False

    set_item_has_shadow(tmp_path, "SAT-1", True)

    assert {item["key"]: item for item in list_items(tmp_path)}["SAT-1"]["hasShadow"] is True

    set_item_has_shadow(tmp_path, "SAT-1", False)

    assert {item["key"]: item for item in list_items(tmp_path)}["SAT-1"]["hasShadow"] is False


def test_set_item_has_shadow_is_a_no_op_for_an_unindexed_item(tmp_path: Path) -> None:
    # No row to update yet -- must not raise, the next reindex is what
    # actually picks this item up (and computes has_shadow fresh from disk
    # at that point).
    set_item_has_shadow(tmp_path, "SAT-999", True)


def test_distinct_item_values_returns_distinct_non_empty_values(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart", priority={"name": "High"})
    _write_issue(tmp_path, "SAT-2", "helm-chart", priority={"name": "High"})
    _write_issue(tmp_path, "SAT-3", "helm-chart", priority={"name": "Low"})
    _write_issue(tmp_path, "SAT-4", "helm-chart")  # no priority at all

    reindex_items(tmp_path)

    assert set(distinct_item_values(tmp_path, "priority")) == {"High", "Low"}


def test_distinct_item_values_returns_none_when_source_has_no_indexed_items(tmp_path: Path) -> None:
    assert distinct_item_values(tmp_path, "priority") is None


def test_distinct_item_values_rejects_a_column_outside_the_allow_list(tmp_path: Path) -> None:
    try:
        distinct_item_values(tmp_path, "extra")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_reindex_items_stores_resolution(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart", resolution={"name": "Fixed"})
    _write_issue(tmp_path, "SAT-2", "helm-chart")  # unresolved

    reindex_items(tmp_path)

    assert distinct_item_values(tmp_path, "resolution") == ["Fixed"]


def _write_boards_cache(tmp_path: Path, boards: list[dict]) -> None:
    write_json(tmp_path / "meta/SAT/boards.json", {"project": "SAT", "fetchedAt": "now", "boards": boards})


def test_reindex_items_populates_board_membership(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "SAT-2", "other")
    _write_boards_cache(
        tmp_path,
        [{"id": 1, "name": "Helm board", "predicate": {"op": "eq", "field": "component", "value": "helm-chart"}}],
    )

    reindex_items(tmp_path, "customfield_10071")

    items = {item["key"]: item for item in list_items(tmp_path)}
    assert items["SAT-1"]["boards"] == ["Helm board"]
    assert items["SAT-2"]["boards"] == []


def test_recompute_board_membership_picks_up_a_boards_cache_change_without_a_reindex(tmp_path: Path) -> None:
    # This is the whole point: board data (boards.json/board_settings.json)
    # can change independently of a resync -- recompute_board_membership
    # is the choke point every one of those mutations calls (see
    # refresh_boards_api/write_board_settings in metadata.py) so stored
    # board membership never goes stale between reindexes.
    from jira_workbench.db import recompute_board_membership

    _write_issue(tmp_path, "SAT-1", "helm-chart")
    reindex_items(tmp_path, "customfield_10071")
    assert {item["key"]: item for item in list_items(tmp_path)}["SAT-1"]["boards"] == []

    _write_boards_cache(
        tmp_path,
        [{"id": 1, "name": "Helm board", "predicate": {"op": "eq", "field": "component", "value": "helm-chart"}}],
    )
    recompute_board_membership(tmp_path)

    assert {item["key"]: item for item in list_items(tmp_path)}["SAT-1"]["boards"] == ["Helm board"]


def test_recompute_board_membership_stores_board_status(tmp_path: Path) -> None:
    from jira_workbench.db import recompute_board_membership

    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "SAT-2", "helm-chart")
    reindex_items(tmp_path, "customfield_10071")
    _write_boards_cache(
        tmp_path,
        [
            {
                "id": 1,
                "name": "Helm board",
                "predicate": {"op": "eq", "field": "component", "value": "helm-chart"},
                "backlogKeys": ["SAT-2"],
            }
        ],
    )

    recompute_board_membership(tmp_path)

    items = {item["key"]: item for item in list_items(tmp_path)}
    assert items["SAT-1"]["boardStatus"] == {"Helm board": "active"}
    assert items["SAT-2"]["boardStatus"] == {"Helm board": "backlog"}


def test_list_items_field_filters_narrows_by_project(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "PLAT-1", "other")
    reindex_items(tmp_path)

    keys = {item["key"] for item in list_items(tmp_path, field_filters={"project": ["SAT"]})}

    assert keys == {"SAT-1"}


def test_list_items_field_filters_is_case_insensitive(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    reindex_items(tmp_path)

    keys = {item["key"] for item in list_items(tmp_path, field_filters={"project": ["sat"]})}

    assert keys == {"SAT-1"}


def test_list_items_field_filters_always_includes_shadow_bearing_rows(tmp_path: Path) -> None:
    # A shadow-edited item might not match on its *stored* column value but
    # could match on its true (shadow-resolved) one, or vice versa --
    # list_items always includes it as a candidate regardless of the SQL
    # condition, leaving the actual accept/reject decision to filter_items.
    from jira_workbench.db import set_item_has_shadow

    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "PLAT-1", "other")
    reindex_items(tmp_path)
    set_item_has_shadow(tmp_path, "PLAT-1", True)

    keys = {item["key"] for item in list_items(tmp_path, field_filters={"project": ["SAT"]})}

    assert keys == {"SAT-1", "PLAT-1"}


def test_list_items_board_filter_uses_json_membership(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "SAT-2", "other")
    _write_boards_cache(
        tmp_path,
        [{"id": 1, "name": "Helm board", "predicate": {"op": "eq", "field": "component", "value": "helm-chart"}}],
    )
    reindex_items(tmp_path, "customfield_10071")

    keys = {item["key"] for item in list_items(tmp_path, board="Helm board")}

    assert keys == {"SAT-1"}


def test_list_items_unmatched_field_value_gets_no_null_rows_pulled_in(tmp_path: Path) -> None:
    # A filter value that matches nothing real shouldn't accidentally sweep
    # in every row that simply has no value for that column.
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    reindex_items(tmp_path)

    keys = {item["key"] for item in list_items(tmp_path, field_filters={"project": ["NOPE"]})}

    assert keys == set()


def test_field_counts_scopes_by_project(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart", priority={"name": "High"})
    _write_issue(tmp_path, "SAT-2", "helm-chart", priority={"name": "Low"})
    _write_issue(tmp_path, "PLAT-1", "other", priority={"name": "High"})
    reindex_items(tmp_path)

    assert dict(field_counts(tmp_path, "priority", projects=["SAT"])) == {"High": 1, "Low": 1}
    assert dict(field_counts(tmp_path, "priority")) == {"High": 2, "Low": 1}


def test_field_counts_scopes_by_board(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "SAT-2", "other")
    _write_boards_cache(
        tmp_path,
        [{"id": 1, "name": "Helm board", "predicate": {"op": "eq", "field": "component", "value": "helm-chart"}}],
    )
    reindex_items(tmp_path, "customfield_10071")

    assert dict(field_counts(tmp_path, "component", board="Helm board")) == {"helm-chart": 1}


def test_field_counts_rejects_a_column_outside_the_allow_list(tmp_path: Path) -> None:
    try:
        field_counts(tmp_path, "extra")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_distinct_projects_for_board(tmp_path: Path) -> None:
    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "PLAT-1", "other")
    _write_boards_cache(
        tmp_path,
        [{"id": 1, "name": "Helm board", "predicate": {"op": "eq", "field": "component", "value": "helm-chart"}}],
    )
    reindex_items(tmp_path, "customfield_10071")

    assert distinct_projects_for_board(tmp_path, "Helm board") == {"SAT"}


def test_count_items(tmp_path: Path) -> None:
    assert count_items(tmp_path) == 0

    _write_issue(tmp_path, "SAT-1", "helm-chart")
    _write_issue(tmp_path, "SAT-2", "helm-chart")
    reindex_items(tmp_path)

    assert count_items(tmp_path) == 2
