from __future__ import annotations

from pathlib import Path
from typing import Any

from jira_workbench.metadata import (
    DoctorCheck,
    JiraApiConfig,
    MetadataError,
    add_component_api,
    add_component_field_option_api,
    add_version_api,
    archive_version_api,
    cache_is_fresh,
    check_acli_config,
    check_jira_api_config,
    delete_version_api,
    ensure_versions,
    format_component_field_option_cache,
    format_doctor_checks,
    format_components,
    format_component_cache,
    format_versions,
    load_component_field_options,
    load_component_summary,
    load_components,
    load_versions,
    normalize_components,
    normalize_versions,
    refresh_component_field_options_api,
    release_version_api,
    rename_version_api,
    refresh_components_api,
    refresh_versions_api,
    refresh_versions,
    resolve_version_id,
)
from jira_workbench.sync import write_json


class VersionRunner:
    def __init__(self, payload: Any | None = None, error: Exception | None = None) -> None:
        self.payload = payload if payload is not None else [{"name": "helm-chart-sa 3.4.0"}]
        self.error = error
        self.calls: list[list[str]] = []

    def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.payload

    def run(self, args: list[str], *, allow_failure: bool = False) -> str:
        self.calls.append(args)
        return "✓ Authenticated\n  Site: example.atlassian.net"


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


def test_refresh_versions_writes_cache(tmp_path: Path) -> None:
    runner = VersionRunner({"versions": [{"name": "helm-chart-sa 3.4.0", "released": False}]})

    cache = refresh_versions(tmp_path, "SAT", runner)

    assert cache["project"] == "SAT"
    assert cache["versions"][0]["name"] == "helm-chart-sa 3.4.0"
    assert (tmp_path / "meta/versions.json").exists()
    assert runner.calls == [["jira", "project", "view", "--key", "SAT", "--json"]]


def test_refresh_versions_api_writes_cache(tmp_path: Path) -> None:
    client = ApiClient()

    cache = refresh_versions_api(tmp_path, "SAT", client)

    assert cache["versions"][0]["name"] == "helm-chart-sa 3.4.0"
    assert load_versions(tmp_path) == cache
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


def test_check_acli_config_checks_project() -> None:
    class Runner(VersionRunner):
        def json(self, args: list[str], *, allow_failure: bool = False) -> Any:
            self.calls.append(args)
            return {"key": "SAT"}

    checks = check_acli_config("SAT", Runner())

    assert all(check.ok for check in checks)


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
    assert load_components(tmp_path) == cache
    assert ("get_project_components", "SAT") in client.calls


def test_refresh_component_field_options_api_writes_cache(tmp_path: Path) -> None:
    client = ApiClient()

    cache = refresh_component_field_options_api(tmp_path, "SAT", "customfield_10071", client)

    assert cache["field"] == "customfield_10071"
    assert cache["options"][0]["value"] == "helm-chart"
    assert load_component_field_options(tmp_path, "customfield_10071") == cache
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
        tmp_path / "meta/versions.json",
        {
            "project": "SAT",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "versions": [{"name": "cached"}],
        },
    )
    runner = VersionRunner()

    result = ensure_versions(tmp_path, "SAT", runner, max_age_seconds=999999999)

    assert not result.refreshed
    assert result.cache["versions"][0]["name"] == "cached"
    assert runner.calls == []


def test_ensure_versions_falls_back_to_stale_cache(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/versions.json",
        {
            "project": "SAT",
            "fetchedAt": "2020-01-01T00:00:00Z",
            "versions": [{"name": "cached"}],
        },
    )
    runner = VersionRunner(error=RuntimeError("offline"))

    result = ensure_versions(tmp_path, "SAT", runner, max_age_seconds=1)

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
