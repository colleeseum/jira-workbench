from __future__ import annotations

from pathlib import Path

from jira_workbench.service import (
    filter_versions,
    meta_component_field_options_output,
    meta_components_output,
    meta_versions_output,
    version_identifier,
)
from jira_workbench.sync import write_json


def test_filter_versions_uses_regex() -> None:
    versions = [
        {"id": "1", "name": "helm-chart-sa 3.4.0", "released": False},
        {"id": "2", "name": "helm-chart-sa 2.9.0", "released": True},
        {"id": "3", "name": "guidedog 1.0.0", "released": False},
    ]

    filtered, error = filter_versions(versions, r"helm-chart-sa 3\.[45]")

    assert error is None
    assert [version["id"] for version in filtered] == ["1"]


def test_filter_versions_reports_invalid_regex() -> None:
    versions = [{"id": "1", "name": "helm-chart-sa 3.4.0"}]

    filtered, error = filter_versions(versions, "[")

    assert filtered == versions
    assert error is not None


def test_meta_versions_output_uses_cached_versions(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/SAT/versions.json",
        {
            "project": "SAT",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "versions": [{"name": "helm-chart-sa 3.4.0", "released": False}],
        },
    )

    output = meta_versions_output(
        jira_dir,
        "SAT",
        "https://example.atlassian.net",
        "user@example.com",
        "bad-token",
    )

    assert "helm-chart-sa 3.4.0" in output


def test_meta_components_output_uses_cached_component_metadata(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/SAT/components.json",
        {
            "project": "SAT",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "components": [{"id": "10000", "name": "helm-chart"}],
        },
    )

    output = meta_components_output(jira_dir, None, None, None, None, None)

    assert "helm-chart" in output
    assert "10000" in output


def test_meta_components_output_does_not_merge_native_components(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/SAT/components.json",
        {
            "project": "SAT",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "components": [{"id": "10000", "name": "new-component"}],
        },
    )
    write_json(
        jira_dir / "manifest.json",
        {
            "components": [
                {"component": "helm-chart", "count": 3},
                {"component": "terraform", "count": 1},
            ],
            "workItems": [
                {"key": "SAT-1", "component": "helm-chart", "status": "To Do"},
                {"key": "SAT-2", "component": "helm-chart", "status": "Done"},
                {"key": "SAT-3", "component": "helm-chart", "status": "In Progress"},
                {"key": "SAT-4", "component": "terraform", "status": "Open"},
            ],
        },
    )

    output = meta_components_output(jira_dir, None, None, None, None, None)

    assert "new-component" not in output
    assert "helm-chart" in output
    assert "terraform" in output
    assert "active" in output
    assert "total" in output


def test_meta_component_field_options_output_uses_cached_options(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/SAT/customfield_10071-options.json",
        {
            "project": "SAT",
            "field": "customfield_10071",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "options": [{"id": "10114", "value": "helm-chart"}],
        },
    )
    write_json(
        jira_dir / "manifest.json",
        {
            "components": [{"component": "helm-chart", "count": 2}],
            "workItems": [
                {"key": "SAT-1", "component": "helm-chart", "status": "To Do"},
                {"key": "SAT-2", "component": "helm-chart", "status": "Closed"},
            ],
        },
    )

    output = meta_component_field_options_output(
        jira_dir,
        "SAT",
        "customfield_10071",
        None,
        None,
        None,
        cached=True,
    )

    assert "helm-chart" in output
    assert "10114" in output
    assert "active" in output
    assert "total" in output


def test_meta_components_output_uses_component_field_options_when_configured(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/SAT/customfield_10071-options.json",
        {
            "project": "SAT",
            "field": "customfield_10071",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "options": [{"id": "10114", "value": "helm-chart"}],
        },
    )
    write_json(
        jira_dir / "meta/SAT/components.json",
        {
            "project": "SAT",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "components": [{"id": "10584", "name": "iac-fluxcd"}],
        },
    )
    write_json(
        jira_dir / "manifest.json",
        {
            "components": [{"component": "helm-chart", "count": 2}],
            "workItems": [
                {"key": "SAT-1", "component": "helm-chart", "status": "To Do"},
                {"key": "SAT-2", "component": "helm-chart", "status": "Done"},
            ],
        },
    )

    output = meta_components_output(jira_dir, "SAT", "customfield_10071", None, None, None)

    assert "helm-chart" in output
    assert "iac-fluxcd" not in output
    assert "active" in output
    assert "total" in output


def test_version_identifier_prefers_id() -> None:
    version = {"id": "10000", "name": "helm-chart-sa 3.5.0", "released": True, "archived": True}

    assert version_identifier(version) == "10000"
