from __future__ import annotations

from pathlib import Path

from jira_workbench.metadata import (
    DoctorCheck,
    JiraApiConfig,
    MetadataError,
    add_component_api,
    add_component_field_option_api,
    add_local_board,
    add_version_api,
    archive_version_api,
    cache_is_fresh,
    check_jira_api_config,
    delete_local_board,
    delete_version_api,
    ensure_versions,
    field_clause_names,
    format_boards,
    format_component_field_option_cache,
    format_doctor_checks,
    format_components,
    format_component_cache,
    format_versions,
    is_project_read_only,
    load_all_boards_with_settings,
    load_board_settings,
    load_boards,
    load_component_field_options,
    load_component_summary,
    load_components,
    load_field_names,
    load_project_registry,
    load_versions,
    normalize_boards,
    normalize_components,
    normalize_versions,
    refresh_boards_api,
    refresh_component_field_options_api,
    release_version_api,
    rename_local_board,
    rename_version_api,
    refresh_components_api,
    refresh_versions_api,
    remember_field_names,
    resolve_version_id,
    set_board_active,
    set_local_board_filters,
    write_board_settings,
    write_project_registry,
)
from jira_workbench.config import ProjectSettings
from jira_workbench.sync import write_json


class ApiClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.versions = [{"id": "10000", "name": "helm-chart-sa 3.4.0", "released": False, "archived": False}]
        self.components = [{"id": "10000", "name": "helm-chart"}]
        self.component_field_options = [{"id": "10114", "value": "helm-chart"}]

    def project(self, key: str) -> dict[str, str]:
        self.calls.append(("project", key))
        return {"id": "10001", "key": key}

    def get_project_versions(self, key: str) -> object:
        self.calls.append(("get_project_versions", key))
        return self.versions

    def add_version(
        self,
        key: str,
        project_id: str,
        version: str,
        *,
        is_archived: bool = False,
        is_released: bool = False,
    ) -> object:
        self.calls.append(("add_version", (key, project_id, version, is_archived, is_released)))
        self.versions.append({"name": version, "released": is_released, "archived": is_archived})
        return {"name": version}

    def update_version(
        self,
        version: str,
        name: str | None = None,
        description: str | None = None,
        is_archived: bool | None = None,
        is_released: bool | None = None,
        start_date: str | None = None,
        release_date: str | None = None,
    ) -> object:
        self.calls.append(
            (
                "update_version",
                (version, name, description, is_archived, is_released, start_date, release_date),
            )
        )
        for item in self.versions:
            if item["id"] == version:
                if name is not None:
                    item["name"] = name
                if is_archived is not None:
                    item["archived"] = is_archived
                if is_released is not None:
                    item["released"] = is_released
        return {"id": version}

    def delete_version(
        self,
        version: str,
        moved_fixed: str | None = None,
        move_affected: str | None = None,
    ) -> object:
        self.calls.append(("delete_version", (version, moved_fixed, move_affected)))
        self.versions = [item for item in self.versions if item["id"] != version]
        return {}

    def get_project_components(self, key: str) -> object:
        self.calls.append(("get_project_components", key))
        return self.components

    def create_component(self, component: dict[str, object]) -> object:
        self.calls.append(("create_component", component))
        self.components.append({"id": "10001", "name": str(component["name"])})
        return component

    def issue_createmeta(self, project: str, expand: str = "projects.issuetypes.fields") -> dict[str, object]:
        self.calls.append(("issue_createmeta", (project, expand)))
        return {
            "projects": [
                {
                    "key": project,
                    "issuetypes": [
                        {
                            "name": "Story",
                            "fields": {
                                "customfield_10071": {
                                    "allowedValues": self.component_field_options,
                                }
                            },
                        }
                    ],
                }
            ]
        }

    def add_custom_field_option(self, field_id: str | int, context_id: str | int, options: list[str]) -> object:
        self.calls.append(("add_custom_field_option", (str(field_id), str(context_id), options)))
        for option in options:
            self.component_field_options.append({"id": f"new-{option}", "value": option})
        return {"options": options}

    def get_all_agile_boards(self, project_key: str | None = None) -> object:
        self.calls.append(("get_all_agile_boards", project_key))
        return {
            "values": [
                {"id": 32, "name": "SAT board", "type": "simple"},
                {"id": 36, "name": "PS Tools", "type": "scrum"},
            ]
        }

    def get_agile_board_configuration(self, board_id: object) -> object:
        self.calls.append(("get_agile_board_configuration", board_id))
        filter_ids = {32: "10089", 36: "10166"}
        return {"filter": {"id": filter_ids[board_id]}}

    def get(self, path: str, params: dict[str, object] | None = None) -> object:
        self.calls.append(("get", path, params))
        if path == "rest/api/2/field":
            # Regression fixture: Jira's field "name" ("Components") can differ
            # from its actual JQL clause name(s) ("Components[Dropdown]"),
            # which live under "clauseNames" -- this must be matched, not "name".
            return [
                {
                    "id": "customfield_10071",
                    "name": "Components",
                    "clauseNames": ["Components[Dropdown]", "cf[10071]", "Components"],
                }
            ]
        if path == "rest/api/2/filter/10089":
            return {"jql": "project = SAT ORDER BY Rank ASC"}
        if path == "rest/api/2/filter/10166":
            return {
                "jql": (
                    'project = SAT AND ( "Components[Dropdown]" = helm-chart OR '
                    '"Components[Dropdown]" = puppy )  OR  labels=k8s_sprints \n'
                )
            }
        if path == "rest/agile/1.0/board/32/backlog":
            start = params["startAt"] if params else 0
            if start == 0:
                return {"total": 150, "issues": [{"key": f"SAT-{i}"} for i in range(100)]}
            return {"total": 150, "issues": [{"key": f"SAT-{i}"} for i in range(100, 150)]}
        if path == "rest/agile/1.0/board/36/backlog":
            return {"total": 2, "issues": [{"key": "SAT-881"}, {"key": "SAT-882"}]}
        raise AssertionError(f"unexpected get() path in test: {path}")


class FailingApiClient(ApiClient):
    def get_project_versions(self, key: str) -> object:
        raise RuntimeError("unauthorized")


def test_normalize_versions_accepts_common_shapes() -> None:
    assert normalize_versions([{"name": "1.0"}]) == [{"name": "1.0"}]
    assert normalize_versions({"values": [{"name": "1.0"}]}) == [{"name": "1.0"}]
    assert normalize_versions({"versions": [{"name": "1.0"}]}) == [{"name": "1.0"}]


def test_normalize_components_accepts_common_shapes() -> None:
    assert normalize_components([{"name": "helm-chart"}]) == [{"name": "helm-chart"}]
    assert normalize_components({"values": [{"name": "helm-chart"}]}) == [{"name": "helm-chart"}]
    assert normalize_components({"components": [{"name": "helm-chart"}]}) == [{"name": "helm-chart"}]


def test_load_field_names_returns_empty_when_missing(tmp_path: Path) -> None:
    assert load_field_names(tmp_path) == {}


def test_remember_field_names_merges_across_calls(tmp_path: Path) -> None:
    remember_field_names(tmp_path, {"customfield_10082": "Customers SAT"})
    remember_field_names(tmp_path, {"customfield_10071": "Component Team"})

    assert load_field_names(tmp_path) == {
        "customfield_10082": "Customers SAT",
        "customfield_10071": "Component Team",
    }


def test_remember_field_names_ignores_an_empty_dict(tmp_path: Path) -> None:
    remember_field_names(tmp_path, {})

    assert load_field_names(tmp_path) == {}


def test_load_project_registry_returns_empty_when_missing(tmp_path: Path) -> None:
    assert load_project_registry(tmp_path) == {}


def test_write_project_registry_round_trips(tmp_path: Path) -> None:
    write_project_registry(
        tmp_path,
        (
            ProjectSettings(key="SAT", default=True),
            ProjectSettings(key="OTHERPROJ", read_only=True),
        ),
    )

    assert load_project_registry(tmp_path) == {
        "SAT": {"readOnly": False, "default": True},
        "OTHERPROJ": {"readOnly": True, "default": False},
    }


def test_is_project_read_only_true_for_a_registered_read_only_project(tmp_path: Path) -> None:
    write_project_registry(tmp_path, (ProjectSettings(key="OTHERPROJ", read_only=True),))

    assert is_project_read_only(tmp_path, "OTHERPROJ") is True
    assert is_project_read_only(tmp_path, "SAT") is False


def test_is_project_read_only_permissive_when_registry_missing_or_project_unknown(tmp_path: Path) -> None:
    assert is_project_read_only(tmp_path, "SAT") is False
    assert is_project_read_only(tmp_path, None) is False


def test_load_board_settings_permissive_when_missing(tmp_path: Path) -> None:
    assert load_board_settings(tmp_path) == {"localBoards": [], "disabledBoardIds": []}


def test_write_board_settings_round_trips(tmp_path: Path) -> None:
    settings = {
        "localBoards": [{"name": "My Filter", "active": True, "fieldFilters": {"project": "PLAT"}, "pattern": None}],
        "disabledBoardIds": ["10032"],
    }

    write_board_settings(tmp_path, settings)

    assert load_board_settings(tmp_path) == settings


def test_add_local_board_then_load_all_boards_with_settings_tags_it_local(tmp_path: Path) -> None:
    add_local_board(tmp_path, "My PLAT Filter", {"project": "PLAT"}, "urgent")

    boards = load_all_boards_with_settings(tmp_path)

    assert boards == [
        {
            "name": "My PLAT Filter",
            "active": True,
            "fieldFilters": {"project": "PLAT"},
            "pattern": "urgent",
            "activeFilter": None,
            "kind": "local",
        }
    ]


def test_add_local_board_rejects_empty_name(tmp_path: Path) -> None:
    try:
        add_local_board(tmp_path, "   ", {}, None)
    except MetadataError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("expected MetadataError")


def test_add_local_board_rejects_name_collision_with_existing_local_board(tmp_path: Path) -> None:
    add_local_board(tmp_path, "My Filter", {}, None)

    try:
        add_local_board(tmp_path, "my filter", {}, None)
    except MetadataError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected MetadataError")


def test_add_local_board_rejects_name_collision_with_a_jira_board(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {"project": "SAT", "fetchedAt": "now", "boards": [{"id": 32, "name": "SAT board", "type": "simple"}]},
    )

    try:
        add_local_board(tmp_path, "SAT board", {}, None)
    except MetadataError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected MetadataError")


def test_rename_local_board_updates_name(tmp_path: Path) -> None:
    add_local_board(tmp_path, "Old Name", {"project": "PLAT"}, None)

    rename_local_board(tmp_path, "Old Name", "New Name")

    boards = load_board_settings(tmp_path)["localBoards"]
    assert boards[0]["name"] == "New Name"
    assert boards[0]["fieldFilters"] == {"project": "PLAT"}


def test_rename_local_board_missing_raises(tmp_path: Path) -> None:
    try:
        rename_local_board(tmp_path, "Nope", "New Name")
    except MetadataError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected MetadataError")


def test_set_local_board_filters_updates_filters_and_pattern(tmp_path: Path) -> None:
    add_local_board(tmp_path, "My Filter", {"project": "SAT"}, None)

    set_local_board_filters(tmp_path, "My Filter", {"project": "PLAT", "assignee": "Alex Epic"}, "urgent")

    board = load_board_settings(tmp_path)["localBoards"][0]
    assert board["fieldFilters"] == {"project": "PLAT", "assignee": "Alex Epic"}
    assert board["pattern"] == "urgent"


def test_add_local_board_stores_active_filter(tmp_path: Path) -> None:
    active_filter = {"fieldFilters": {"status": ["To Do", "In Progress"]}, "pattern": None}
    add_local_board(tmp_path, "My Filter", {"project": ["PLAT"]}, None, active_filter)

    board = load_board_settings(tmp_path)["localBoards"][0]
    assert board["activeFilter"] == active_filter


def test_set_local_board_filters_updates_active_filter(tmp_path: Path) -> None:
    add_local_board(tmp_path, "My Filter", {"project": ["SAT"]}, None)

    active_filter = {"fieldFilters": {"status": ["Done"]}, "pattern": None}
    set_local_board_filters(tmp_path, "My Filter", {"project": ["SAT"]}, None, active_filter)

    board = load_board_settings(tmp_path)["localBoards"][0]
    assert board["activeFilter"] == active_filter

    set_local_board_filters(tmp_path, "My Filter", {"project": ["SAT"]}, None, None)
    board = load_board_settings(tmp_path)["localBoards"][0]
    assert board["activeFilter"] is None


def test_delete_local_board_removes_it(tmp_path: Path) -> None:
    add_local_board(tmp_path, "My Filter", {}, None)

    delete_local_board(tmp_path, "My Filter")

    assert load_board_settings(tmp_path)["localBoards"] == []


def test_delete_local_board_missing_raises(tmp_path: Path) -> None:
    try:
        delete_local_board(tmp_path, "Nope")
    except MetadataError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected MetadataError")


def test_set_board_active_toggles_local_board(tmp_path: Path) -> None:
    add_local_board(tmp_path, "My Filter", {}, None)

    set_board_active(tmp_path, "local", "My Filter", False)

    assert load_board_settings(tmp_path)["localBoards"][0]["active"] is False


def test_set_board_active_toggles_jira_board_via_disabled_ids(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {"project": "SAT", "fetchedAt": "now", "boards": [{"id": 32, "name": "SAT board", "type": "simple"}]},
    )

    set_board_active(tmp_path, "jira", 32, False)
    boards = load_all_boards_with_settings(tmp_path)
    assert next(b for b in boards if b["name"] == "SAT board")["active"] is False

    set_board_active(tmp_path, "jira", 32, True)
    boards = load_all_boards_with_settings(tmp_path)
    assert next(b for b in boards if b["name"] == "SAT board")["active"] is True


def test_load_all_boards_with_settings_merges_jira_and_local(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/boards.json",
        {"project": "SAT", "fetchedAt": "now", "boards": [{"id": 32, "name": "SAT board", "type": "simple"}]},
    )
    add_local_board(tmp_path, "My PLAT Filter", {"project": "PLAT"}, None)

    boards = load_all_boards_with_settings(tmp_path)

    by_name = {board["name"]: board for board in boards}
    assert by_name["SAT board"]["kind"] == "jira"
    assert by_name["SAT board"]["active"] is True
    assert by_name["My PLAT Filter"]["kind"] == "local"
    assert by_name["My PLAT Filter"]["active"] is True


def test_refresh_versions_api_writes_cache(tmp_path: Path) -> None:
    client = ApiClient()

    cache = refresh_versions_api(tmp_path, "SAT", client)

    assert cache["versions"][0]["name"] == "helm-chart-sa 3.4.0"
    assert load_versions(tmp_path, "SAT") == cache
    assert ("get_project_versions", "SAT") in client.calls


def test_refresh_versions_api_wraps_client_errors(tmp_path: Path) -> None:
    client = FailingApiClient()

    try:
        refresh_versions_api(tmp_path, "SAT", client)
    except MetadataError as exc:
        assert "could not refresh Jira versions for SAT" in str(exc)
    else:
        raise AssertionError("expected MetadataError")


def test_check_jira_api_config_detects_wrong_project() -> None:
    class WrongProjectClient(ApiClient):
        def project(self, key: str) -> dict[str, str]:
            return {"id": "10001", "key": "SAT"}

    client = WrongProjectClient()

    checks = check_jira_api_config(
        "BAD",
        JiraApiConfig(
            url="https://example.atlassian.net",
            email="user@example.com",
            api_token="token",
        ),
        client,
    )

    assert any(check.name == "jira api project" and not check.ok for check in checks)


def test_format_doctor_checks() -> None:
    output = format_doctor_checks(
        [
            DoctorCheck("jira api project", True, "project=SAT"),
            DoctorCheck("jira api versions", False, "unauthorized"),
        ]
    )

    assert "ok" in output
    assert "fail" in output
    assert "unauthorized" in output


def test_refresh_components_api_writes_cache(tmp_path: Path) -> None:
    client = ApiClient()

    cache = refresh_components_api(tmp_path, "SAT", client)

    assert cache["components"][0]["name"] == "helm-chart"
    assert load_components(tmp_path, "SAT") == cache
    assert ("get_project_components", "SAT") in client.calls


def test_normalize_boards_accepts_common_shapes() -> None:
    assert normalize_boards([{"name": "SAT board"}]) == [{"name": "SAT board"}]
    assert normalize_boards({"values": [{"name": "SAT board"}]}) == [{"name": "SAT board"}]
    assert normalize_boards({"boards": [{"name": "SAT board"}]}) == [{"name": "SAT board"}]


def test_field_clause_names_native_components_field() -> None:
    assert field_clause_names(ApiClient(), "components") == ["component"]


def test_field_clause_names_custom_field_uses_clause_names_not_display_name() -> None:
    assert field_clause_names(ApiClient(), "customfield_10071") == [
        "Components[Dropdown]",
        "cf[10071]",
        "Components",
    ]


def test_field_clause_names_falls_back_to_field_id_when_unknown() -> None:
    assert field_clause_names(ApiClient(), "customfield_99999") == ["customfield_99999"]


def test_refresh_boards_api_writes_cache_with_compiled_predicates_and_backlog(tmp_path: Path) -> None:
    client = ApiClient()

    cache = refresh_boards_api(tmp_path, "SAT", client, "customfield_10071")

    assert load_boards(tmp_path, "SAT") == cache
    boards = {board["name"]: board for board in cache["boards"]}

    sat_board = boards["SAT board"]
    assert sat_board["type"] == "simple"
    assert sat_board["unsupportedReason"] is None
    assert sat_board["predicate"] == {"op": "eq", "field": "project", "value": "SAT"}
    assert sat_board["backlogKeys"] == [f"SAT-{i}" for i in range(150)]

    ps_tools = boards["PS Tools"]
    assert ps_tools["type"] == "scrum"
    assert ps_tools["unsupportedReason"] is None
    assert ps_tools["predicate"] is not None
    # scrum boards have a real backlog too (unassigned-to-sprint issues) --
    # the Agile backlog endpoint works for them same as any other board type.
    assert ps_tools["backlogKeys"] == ["SAT-881", "SAT-882"]


def test_refresh_boards_api_implicitly_scopes_a_project_less_filter_to_its_own_project(tmp_path: Path) -> None:
    # A board's saved filter doesn't have to mention "project" at all --
    # Jira itself still only ever shows that board's own project's issues,
    # so the compiled predicate must be scoped the same way even though
    # the JQL text alone never says so.
    class ProjectLessFilterClient(ApiClient):
        def get(self, path: str, params: dict[str, object] | None = None) -> object:
            if path == "rest/api/2/filter/10089":
                return {"jql": '"Components[Dropdown]" = helm-chart ORDER BY Rank ASC'}
            return super().get(path, params)

    cache = refresh_boards_api(tmp_path, "SAT", ProjectLessFilterClient(), "customfield_10071")

    sat_board = next(board for board in cache["boards"] if board["name"] == "SAT board")
    assert sat_board["unsupportedReason"] is None
    assert sat_board["predicate"] == {
        "op": "and",
        "clauses": [
            {"op": "eq", "field": "project", "value": "SAT"},
            {"op": "eq", "field": "component", "value": "helm-chart"},
        ],
    }


def test_refresh_boards_api_wraps_client_errors(tmp_path: Path) -> None:
    class FailingBoardsClient(ApiClient):
        def get_all_agile_boards(self, project_key: str | None = None) -> object:
            raise RuntimeError("boom")

    try:
        refresh_boards_api(tmp_path, "SAT", FailingBoardsClient(), "customfield_10071")
    except MetadataError as exc:
        assert "could not refresh Jira boards for SAT" in str(exc)
    else:
        raise AssertionError("expected MetadataError")


def test_refresh_boards_api_marks_board_unsupported_on_bad_jql(tmp_path: Path) -> None:
    class UnsupportedFilterClient(ApiClient):
        def get(self, path: str, params: dict[str, object] | None = None) -> object:
            if path == "rest/api/2/filter/10089":
                return {"jql": "assignee = currentUser()"}
            return super().get(path, params)

    cache = refresh_boards_api(tmp_path, "SAT", UnsupportedFilterClient(), "customfield_10071")

    sat_board = next(board for board in cache["boards"] if board["name"] == "SAT board")
    assert sat_board["predicate"] is None
    assert sat_board["unsupportedReason"]


def test_format_boards_reports_membership_and_backlog_counts() -> None:
    output = format_boards(
        {
            "boards": [
                {"name": "SAT board", "type": "simple", "unsupportedReason": None, "backlogKeys": ["SAT-1"]},
                {"name": "PS Tools", "type": "scrum", "unsupportedReason": None, "backlogKeys": None},
                {"name": "Weird", "type": "simple", "unsupportedReason": "unsupported field: assignee", "backlogKeys": None},
            ]
        }
    )

    assert "SAT board" in output
    assert "1 issues" in output
    assert "n/a" in output
    assert "unsupported: unsupported field: assignee" in output


def test_format_boards_includes_jql_alongside_membership() -> None:
    output = format_boards(
        {
            "boards": [
                {
                    "name": "SAT board",
                    "type": "simple",
                    "unsupportedReason": None,
                    "backlogKeys": None,
                    "jql": "project = SAT ORDER BY Rank ASC",
                },
                {
                    "name": "Weird",
                    "type": "simple",
                    "unsupportedReason": "unsupported field: assignee",
                    "backlogKeys": None,
                    "jql": "assignee = currentUser()",
                },
            ]
        }
    )

    assert "ok (jql: project = SAT ORDER BY Rank ASC)" in output
    assert "unsupported: unsupported field: assignee (jql: assignee = currentUser())" in output


def test_format_boards_reports_no_boards() -> None:
    assert format_boards({"boards": []}) == "no boards found\n"


def test_refresh_component_field_options_api_writes_cache(tmp_path: Path) -> None:
    client = ApiClient()

    cache = refresh_component_field_options_api(tmp_path, "SAT", "customfield_10071", client)

    assert cache["field"] == "customfield_10071"
    assert cache["options"][0]["value"] == "helm-chart"
    assert load_component_field_options(tmp_path, "SAT", "customfield_10071") == cache
    assert ("issue_createmeta", ("SAT", "projects.issuetypes.fields")) in client.calls


def test_add_version_api_creates_and_refreshes(tmp_path: Path) -> None:
    client = ApiClient()

    cache = add_version_api(tmp_path, "SAT", "helm-chart-sa 3.5.0", client)

    assert ("project", "SAT") in client.calls
    assert ("add_version", ("SAT", "10001", "helm-chart-sa 3.5.0", False, False)) in client.calls
    assert any(version["name"] == "helm-chart-sa 3.5.0" for version in cache["versions"])


def test_resolve_version_id_accepts_name_or_id() -> None:
    client = ApiClient()

    assert resolve_version_id(client, "SAT", "helm-chart-sa 3.4.0") == "10000"
    assert resolve_version_id(client, "SAT", "10000") == "10000"


def test_rename_version_api_updates_and_refreshes(tmp_path: Path) -> None:
    client = ApiClient()

    cache = rename_version_api(tmp_path, "SAT", "helm-chart-sa 3.4.0", "helm-chart-sa 3.4.1", client)

    assert ("update_version", ("10000", "helm-chart-sa 3.4.1", None, None, None, None, None)) in client.calls
    assert any(version["name"] == "helm-chart-sa 3.4.1" for version in cache["versions"])


def test_release_version_api_updates_and_refreshes(tmp_path: Path) -> None:
    client = ApiClient()

    release_version_api(tmp_path, "SAT", "helm-chart-sa 3.4.0", client, release_date="2026-07-21")

    assert ("update_version", ("10000", None, None, None, True, None, "2026-07-21")) in client.calls


def test_release_version_api_noops_when_already_released(tmp_path: Path) -> None:
    client = ApiClient()
    client.versions[0]["released"] = True

    cache = release_version_api(tmp_path, "SAT", "helm-chart-sa 3.4.0", client)

    assert getattr(cache, "changed") is False
    assert getattr(cache, "message") == "version helm-chart-sa 3.4.0 is already released"
    assert not any(call[0] == "update_version" for call in client.calls)


def test_archive_version_api_updates_and_refreshes(tmp_path: Path) -> None:
    client = ApiClient()

    archive_version_api(tmp_path, "SAT", "helm-chart-sa 3.4.0", client)

    assert ("update_version", ("10000", None, None, True, None, None, None)) in client.calls


def test_archive_version_api_noops_when_already_archived(tmp_path: Path) -> None:
    client = ApiClient()
    client.versions[0]["archived"] = True

    cache = archive_version_api(tmp_path, "SAT", "helm-chart-sa 3.4.0", client)

    assert getattr(cache, "changed") is False
    assert getattr(cache, "message") == "version helm-chart-sa 3.4.0 is already archived"
    assert not any(call[0] == "update_version" for call in client.calls)


def test_delete_version_api_deletes_and_refreshes(tmp_path: Path) -> None:
    client = ApiClient()
    client.versions.append({"id": "10001", "name": "next", "released": False, "archived": False})

    cache = delete_version_api(tmp_path, "SAT", "helm-chart-sa 3.4.0", client, move_fix_to="next")

    assert ("delete_version", ("10000", "10001", None)) in client.calls
    assert all(version["name"] != "helm-chart-sa 3.4.0" for version in cache["versions"])


def test_add_component_api_creates_and_refreshes(tmp_path: Path) -> None:
    client = ApiClient()

    cache = add_component_api(tmp_path, "SAT", "voicebox", client)

    assert ("create_component", {"name": "voicebox", "project": "SAT"}) in client.calls
    assert any(component["name"] == "voicebox" for component in cache["components"])


def test_add_component_field_option_api_requires_context_for_missing_option(tmp_path: Path) -> None:
    client = ApiClient()

    try:
        add_component_field_option_api(tmp_path, "SAT", "customfield_10071", None, "iac-fluxcd", client)
    except MetadataError as exc:
        assert "missing context id" in str(exc)
    else:
        raise AssertionError("expected MetadataError")

    assert not any(call[0] == "add_custom_field_option" for call in client.calls)


def test_add_component_field_option_api_adds_and_refreshes(tmp_path: Path) -> None:
    client = ApiClient()

    cache = add_component_field_option_api(tmp_path, "SAT", "customfield_10071", "10001", "iac-fluxcd", client)

    assert ("add_custom_field_option", ("customfield_10071", "10001", ["iac-fluxcd"])) in client.calls
    assert any(option["value"] == "iac-fluxcd" for option in cache["options"])


def test_add_component_field_option_api_fails_when_option_not_available_after_add(tmp_path: Path) -> None:
    class WrongContextClient(ApiClient):
        def add_custom_field_option(self, field_id: str | int, context_id: str | int, options: list[str]) -> object:
            self.calls.append(("add_custom_field_option", (str(field_id), str(context_id), options)))
            return {"options": options}

    client = WrongContextClient()

    try:
        add_component_field_option_api(tmp_path, "SAT", "customfield_10071", "wrong", "iac-fluxcd", client)
    except MetadataError as exc:
        assert "not available for project SAT" in str(exc)
    else:
        raise AssertionError("expected MetadataError")


def test_ensure_versions_uses_fresh_cache(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {
            "project": "SAT",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "versions": [{"name": "cached"}],
        },
    )
    client = ApiClient()

    result = ensure_versions(tmp_path, "SAT", client, max_age_seconds=999999999)

    assert not result.refreshed
    assert result.cache["versions"][0]["name"] == "cached"
    assert client.calls == []


def test_ensure_versions_falls_back_to_stale_cache(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/SAT/versions.json",
        {
            "project": "SAT",
            "fetchedAt": "2020-01-01T00:00:00Z",
            "versions": [{"name": "cached"}],
        },
    )
    client = FailingApiClient()

    result = ensure_versions(tmp_path, "SAT", client, max_age_seconds=1)

    assert result.stale
    assert result.error is not None
    assert result.cache["versions"][0]["name"] == "cached"


def test_cache_is_fresh_rejects_wrong_project() -> None:
    cache = {"project": "OTHER", "fetchedAt": "2026-07-21T06:00:00Z"}

    assert not cache_is_fresh(cache, "SAT", 999999999)


def test_format_versions_prints_status_columns() -> None:
    output = format_versions(
        {
            "versions": [
                {"name": "helm-chart-sa 3.4.0", "released": True},
                {"name": "helm-chart-sa 3.5.0", "released": False, "archived": True},
            ]
        }
    )

    assert "helm-chart-sa 3.4.0" in output
    assert "released" in output
    assert "archived" in output


def test_format_component_cache_prints_ids() -> None:
    output = format_component_cache(
        {"components": [{"id": "10000", "name": "helm-chart"}, {"id": "10001", "name": "misc"}]}
    )

    assert "helm-chart" in output
    assert "10000" in output


def test_format_component_field_option_cache_prints_values_and_ids() -> None:
    output = format_component_field_option_cache({"options": [{"id": "10114", "value": "helm-chart"}]})

    assert "helm-chart" in output
    assert "10114" in output


def test_load_and_format_component_summary(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {
            "components": [
                {"component": "helm-chart", "count": 4},
                {"component": "_unassigned", "count": 1},
            ],
            "workItems": [
                {"key": "SAT-1", "component": "helm-chart", "status": "To Do"},
                {"key": "SAT-2", "component": "helm-chart", "status": "Done"},
                {"key": "SAT-3", "component": "helm-chart", "status": "Closed"},
                {"key": "SAT-4", "component": "helm-chart", "status": "In Progress"},
                {"key": "SAT-5", "component": "_unassigned", "status": "Open"},
            ],
        },
    )

    components = load_component_summary(tmp_path)
    output = format_components(components)

    assert components[0]["component"] == "helm-chart"
    assert components[0]["active"] == 2
    assert components[0]["total"] == 4
    assert "component" in output
    assert "active" in output
    assert "total" in output
    assert "helm-chart" in output
    assert "_unassigned" in output


def test_load_component_summary_uses_real_status_category_not_hardcoded_names(tmp_path: Path) -> None:
    # Regression: a custom workflow status ("Solved") that Jira classifies
    # as Done must count as done here too, even though the old code's
    # hardcoded name list never recognized it.
    write_json(
        tmp_path / "manifest.json",
        {
            "components": [{"component": "helm-chart", "count": 2}],
            "workItems": [
                {"key": "SAT-1", "component": "helm-chart", "status": "Solved", "statusCategory": "done"},
                {"key": "SAT-2", "component": "helm-chart", "status": "In Review", "statusCategory": "indeterminate"},
            ],
        },
    )

    components = load_component_summary(tmp_path)

    assert components[0]["active"] == 1
    assert components[0]["total"] == 2
