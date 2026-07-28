from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import jira_workbench.cli
from jira_workbench.cli import (
    filter_versions,
    meta_component_field_options_output,
    main,
    meta_components_output,
    meta_versions_output,
    sync_progress_printer,
    version_identifier,
)
from jira_workbench.config import load_config
from jira_workbench.metadata import remember_field_names
from jira_workbench.shadow import load_shadow
from jira_workbench.sync import write_json


def test_cli_help(capsys) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "jira-wb" in captured.out


def test_cli_version(capsys) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    captured = capsys.readouterr()
    assert "jira-wb 0.5.0" in captured.out


@pytest.mark.skipif(os.name != "posix", reason="file permission bits are POSIX-specific")
def test_main_tightens_overly_permissive_config_file(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text('project = "SAT"\n')
    config_path.chmod(0o644)

    code = main(["--config", str(config_path), "meta", "--jira-dir", str(tmp_path / "jira"), "doctor"])

    captured = capsys.readouterr()
    assert code == 1
    assert f"note: tightened permissions on {config_path} to 0600" in captured.err
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_shadow_without_subcommand_shows_help_without_requiring_config(tmp_path: Path, capsys) -> None:
    code = main(["--config", str(tmp_path / "missing.conf"), "shadow"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Manage local-only Jira changes" in captured.out
    assert "{set,unset,comment,status-change,diff,report,commit,status,push}" in captured.out
    assert "missing required configuration" not in captured.err


def test_sync_missing_api_config_prints_friendly_error(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "sync",
            "--project",
            "SAT",
            "--component-field",
            "customfield_10071",
            "--jira-dir",
            str(tmp_path / "jira"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "error: missing required configuration for Jira API" in captured.err
    assert "jira_url, jira_email, jira_api_token" in captured.err
    assert "Traceback" not in captured.err


def test_sync_requires_config_or_flags(tmp_path: Path, capsys) -> None:
    code = main(["--config", str(tmp_path / "missing.conf"), "sync"])

    captured = capsys.readouterr()
    assert code == 2
    assert "missing required configuration: project, jira_dir" in captured.err


def test_sync_progress_printer_rewrites_issue_progress_on_tty() -> None:
    class Stream:
        def __init__(self) -> None:
            self.output = ""

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> None:
            self.output += text

        def flush(self) -> None:
            pass

    stream = Stream()
    progress = sync_progress_printer(stream)

    progress("[3/5] Syncing changed issues... 1/2 SAT-1 changed=0 unchanged=0")
    progress("[3/5] Syncing changed issues... 2/2 SAT-2 changed=1 unchanged=0")
    progress("[4/5] Building manifest...")

    assert "\r[3/5] Syncing changed issues... 1/2 SAT-1 changed=0 unchanged=0" in stream.output
    assert "\r[3/5] Syncing changed issues... 2/2 SAT-2 changed=1 unchanged=0" in stream.output
    assert "\n[4/5] Building manifest...\n" in stream.output


def test_sync_force_flag_threads_through_to_sync_config(tmp_path: Path, monkeypatch) -> None:
    captured_configs = []

    def fake_sync_project(config, client, progress=None):
        captured_configs.append(config)
        from jira_workbench.sync import SyncResult

        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.cli, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                'component_field = "customfield_10071"',
                f'jira_dir = "{tmp_path / "jira"}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
            ]
        )
    )

    assert main(["--config", str(config_path), "sync", "--force"]) == 0
    assert main(["--config", str(config_path), "sync"]) == 0

    assert captured_configs[0].force is True
    assert captured_configs[1].force is False


def test_sync_reads_config_file_and_flags_override(tmp_path: Path, capsys, monkeypatch) -> None:
    class Client:
        def __init__(self) -> None:
            self.jqls: list[str] = []

        def get_project_versions(self, key: str) -> object:
            return []

        def enhanced_jql_get_list_of_tickets(
            self,
            jql: str,
            fields: str | list[str] = "*all",
            limit: int | None = None,
            expand: str | None = None,
        ) -> list[dict[str, object]]:
            self.jqls.append(jql)
            return []

    client = Client()
    monkeypatch.setattr(jira_workbench.cli, "jira_api_client", lambda _config: client)
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "CFG"',
                'component_field = "customfield_cfg"',
                f'jira_dir = "{tmp_path / "jira"}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
            ]
        )
        + "\n"
    )

    code = main(["--config", str(config_path), "sync", "--project", "FLAG"])

    captured = capsys.readouterr()
    assert code == 0
    assert client.jqls == ["project=FLAG ORDER BY key"]
    assert "Synced 0 work items" in captured.out


def test_shadow_diff_can_export_to_file(tmp_path: Path, capsys) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Remote summary",
                "updated": "2026-07-20T00:00:01.000+0000",
            },
        },
    )
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(f'jira_dir = "{jira_dir}"\n')

    assert main(["--config", str(config_path), "shadow", "set", "SAT-1", "summary", "Local summary"]) == 0
    output_path = tmp_path / "reports/shadow.diff"

    code = main(["--config", str(config_path), "shadow", "diff", "-o", str(output_path)])

    captured = capsys.readouterr()
    assert code == 0
    assert f"wrote shadow diff to {output_path}" in captured.out
    diff = output_path.read_text()
    assert "SAT-1 (working)" in diff
    assert "field: summary" in diff
    assert '-"Remote summary"' in diff
    assert '+"Local summary"' in diff


def test_shadow_report_can_export_to_file(tmp_path: Path, capsys) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "assignee": {"displayName": "Serge Colle"},
                "description": "Original line one\nOriginal line two",
                "summary": "Remote summary",
                "updated": "2026-07-20T00:00:01.000+0000",
            },
        },
    )
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(f'jira_dir = "{jira_dir}"\n')

    assert main(["--config", str(config_path), "shadow", "set", "SAT-1", "assignee", "Marlon Garcia"]) == 0
    assert (
        main(
            [
                "--config",
                str(config_path),
                "shadow",
                "set",
                "SAT-1",
                "description",
                "Original line one\nNew line two",
            ]
        )
        == 0
    )
    output_path = tmp_path / "reports/shadow-report.txt"

    code = main(["--config", str(config_path), "shadow", "report", "-o", str(output_path)])

    captured = capsys.readouterr()
    assert code == 0
    assert f"wrote shadow report to {output_path}" in captured.out
    report = output_path.read_text()
    assert "Local shadow report: 1 item" in report
    assert "SAT-1 (working)" in report
    assert "Summary: Remote summary" in report
    assert "assignee: Serge Colle -> Marlon Garcia" in report
    assert "- description:" in report
    assert "  Before:" in report
    assert "    Original line two" in report
    assert "  After:" in report
    assert "    New line two" in report


def test_shadow_report_shows_custom_field_display_name_not_raw_id(tmp_path: Path, capsys) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Remote summary",
                "customfield_10082": ["acme"],
                "updated": "2026-07-20T00:00:01.000+0000",
            },
        },
    )
    # Locally cached the way DetailScreen's live editmeta fetch would --
    # the CLI report itself never makes an API call.
    remember_field_names(jira_dir, {"customfield_10082": "Customers SAT"})
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(f'jira_dir = "{jira_dir}"\n')

    assert main(["--config", str(config_path), "shadow", "set", "SAT-1", "customfield_10082", "acme, globex"]) == 0

    code = main(["--config", str(config_path), "shadow", "report"])

    captured = capsys.readouterr()
    assert code == 0
    assert "- Customers SAT:" in captured.out
    assert "customfield_10082:" not in captured.out


def test_shadow_set_parses_json_value(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Remote summary",
                "updated": "2026-07-20T00:00:01.000+0000",
            },
        },
    )
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(f'jira_dir = "{jira_dir}"\n')

    code = main(
        [
            "--config",
            str(config_path),
            "shadow",
            "set",
            "SAT-1",
            "customfield_10071",
            '{"value":"iac-fluxcd"}',
        ]
    )

    assert code == 0
    shadow = load_shadow(jira_dir, "SAT-1")
    assert shadow is not None
    assert shadow["fields"]["customfield_10071"] == {"value": "iac-fluxcd"}


def test_shadow_unset_removes_field(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "summary": "Remote summary",
                "updated": "2026-07-20T00:00:01.000+0000",
            },
        },
    )
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(f'jira_dir = "{jira_dir}"\n')

    assert main(["--config", str(config_path), "shadow", "set", "SAT-1", "summary", "Local summary"]) == 0
    assert main(["--config", str(config_path), "shadow", "unset", "SAT-1", "summary"]) == 0

    shadow = load_shadow(jira_dir, "SAT-1")
    assert shadow is not None
    assert "summary" not in shadow["fields"]


def test_shadow_status_change_sets_resolution(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(jira_dir / "components/helm-chart/SAT-1/issue.json", {"key": "SAT-1", "fields": {}})
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(f'jira_dir = "{jira_dir}"\n')

    code = main(
        [
            "--config",
            str(config_path),
            "shadow",
            "status-change",
            "SAT-1",
            "--resolution",
            "Duplicate",
        ]
    )

    assert code == 0
    shadow = load_shadow(jira_dir, "SAT-1")
    assert shadow is not None
    assert shadow["statusChange"] == {"resolution": "Duplicate"}


def test_view_reads_default_component_and_filter_from_config(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "jira-wb.conf"
    jira_dir = tmp_path / "jira"
    config_path.write_text(
        "\n".join(
            [
                f'jira_dir = "{jira_dir}"',
                'component_field = "customfield_10071"',
                "[view]",
                'component = "helm-chart"',
                'fix_version = "2026.07"',
                'assignee = "you@example.com"',
                'board = "SAT board"',
                'board_scope = "active"',
                'filter = "prometheus"',
                'swimlane = "epic"',
                "active = false",
                "preview_lines = 20",
                "nerd_font = true",
                'dev_status_field = "customfield_10099"',
            ]
        )
        + "\n"
    )
    calls = []

    def fake_open_interactive_view(*args, **kwargs) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(jira_workbench.cli, "open_interactive_view", fake_open_interactive_view)

    code = main(["--config", str(config_path), "view"])

    assert code == 0
    assert calls[0][0][0] == jira_dir
    assert calls[0][1]["component_field"] == "customfield_10071"
    assert calls[0][1]["component"] == "helm-chart"
    assert calls[0][1]["fix_version"] == "2026.07"
    assert calls[0][1]["assignee"] == "you@example.com"
    assert calls[0][1]["board"] == "SAT board"
    assert calls[0][1]["board_scope"] == "active"
    assert calls[0][1]["pattern"] == "prometheus"
    assert calls[0][1]["swimlane"] == "epic"
    assert calls[0][1]["active"] is False
    assert calls[0][1]["preview_lines"] == 20
    assert calls[0][1]["config_path"] == config_path
    assert calls[0][1]["nerd_font"] is True
    assert calls[0][1]["dev_status_field"] == "customfield_10099"


def test_view_nerd_font_defaults_to_false_when_unset(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "jira-wb.conf"
    jira_dir = tmp_path / "jira"
    config_path.write_text(f'jira_dir = "{jira_dir}"\n')
    calls = []

    def fake_open_interactive_view(*args, **kwargs) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(jira_workbench.cli, "open_interactive_view", fake_open_interactive_view)

    code = main(["--config", str(config_path), "view"])

    assert code == 0
    assert calls[0][1]["nerd_font"] is False
    assert calls[0][1]["dev_status_field"] is None


def test_view_all_flag_overrides_configured_active_default(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "jira-wb.conf"
    jira_dir = tmp_path / "jira"
    config_path.write_text(
        "\n".join([f'jira_dir = "{jira_dir}"', "[view]", "active = true"]) + "\n"
    )
    calls = []

    def fake_open_interactive_view(*args, **kwargs) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(jira_workbench.cli, "open_interactive_view", fake_open_interactive_view)

    code = main(["--config", str(config_path), "view", "--all"])

    assert code == 0
    assert calls[0][1]["active"] is False


def test_view_flags_override_config_defaults(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "jira-wb.conf"
    jira_dir = tmp_path / "jira"
    config_path.write_text(
        "\n".join(
            [
                f'jira_dir = "{jira_dir}"',
                "[view]",
                'component = "helm-chart"',
                'filter = "prometheus"',
                'swimlane = "epic"',
            ]
        )
        + "\n"
    )
    calls = []

    def fake_open_interactive_view(*args, **kwargs) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(jira_workbench.cli, "open_interactive_view", fake_open_interactive_view)

    code = main(
        [
            "--config",
            str(config_path),
            "view",
            "--component",
            "terraform",
            "--filter",
            "state",
            "--swimlane",
            "component",
        ]
    )

    assert code == 0
    assert calls[0][1]["component"] == "terraform"
    assert calls[0][1]["pattern"] == "state"
    assert calls[0][1]["swimlane"] == "component"


def test_issue_create_dry_run_uses_configured_component_field(tmp_path: Path, capsys, monkeypatch) -> None:
    class Client:
        def issue_createmeta(self, project: str) -> dict[str, object]:
            return {
                "projects": [
                    {
                        "key": project,
                        "issuetypes": [
                            {
                                "name": "Improvement",
                                "fields": {
                                    "summary": {},
                                    "description": {},
                                    "issuetype": {},
                                    "project": {},
                                    "customfield_10071": {},
                                    "reporter": {},
                                },
                            }
                        ],
                    }
                ]
            }

        def myself(self) -> dict[str, str]:
            return {"accountId": "me"}

    monkeypatch.setattr(jira_workbench.cli, "jira_api_client", lambda _config: Client())
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                'jira_dir = "jira"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                'component_field = "customfield_10071"',
            ]
        )
        + "\n"
    )

    code = main(
        [
            "--config",
            str(config_path),
            "issue",
            "create",
            "--summary",
            "Create resource model",
            "--type",
            "Improvement",
            "--component",
            "resource-as-code",
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "customfield_10071: {'value': 'resource-as-code'}" in captured.out
    assert "reporter: {'accountId': 'me'}" in captured.out


def test_issue_create_requires_type_when_no_flag_or_config_default(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(jira_workbench.cli, "jira_api_client", lambda _config: object())
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                'jira_dir = "jira"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
            ]
        )
        + "\n"
    )

    code = main(["--config", str(config_path), "issue", "create", "--summary", "No type given"])

    captured = capsys.readouterr()
    assert code == 1
    assert "issue type is required" in captured.err


def test_issue_create_uses_configured_default_type(tmp_path: Path, capsys, monkeypatch) -> None:
    class Client:
        def issue_createmeta(self, project: str) -> dict[str, object]:
            return {
                "projects": [
                    {
                        "key": project,
                        "issuetypes": [{"name": "Story", "fields": {"summary": {}, "project": {}, "issuetype": {}}}],
                    }
                ]
            }

    monkeypatch.setattr(jira_workbench.cli, "jira_api_client", lambda _config: Client())
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                'jira_dir = "jira"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "[issue]",
                'default_type = "Story"',
            ]
        )
        + "\n"
    )

    code = main(
        ["--config", str(config_path), "issue", "create", "--summary", "Uses configured default", "--dry-run"]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "issuetype: {'name': 'Story'}" in captured.out


def test_issue_create_type_flag_overrides_configured_default(tmp_path: Path, capsys, monkeypatch) -> None:
    class Client:
        def issue_createmeta(self, project: str) -> dict[str, object]:
            return {
                "projects": [
                    {
                        "key": project,
                        "issuetypes": [
                            {"name": "Story", "fields": {"summary": {}, "project": {}, "issuetype": {}}},
                            {"name": "Bug", "fields": {"summary": {}, "project": {}, "issuetype": {}}},
                        ],
                    }
                ]
            }

    monkeypatch.setattr(jira_workbench.cli, "jira_api_client", lambda _config: Client())
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                'jira_dir = "jira"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "[issue]",
                'default_type = "Story"',
            ]
        )
        + "\n"
    )

    code = main(
        [
            "--config",
            str(config_path),
            "issue",
            "create",
            "--summary",
            "Overrides configured default",
            "--type",
            "Bug",
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "issuetype: {'name': 'Bug'}" in captured.out


def test_issue_create_creates_and_refreshes_local_issue(tmp_path: Path, capsys, monkeypatch) -> None:
    class Client:
        def __init__(self) -> None:
            self.created_fields: dict[str, object] | None = None

        def issue_createmeta(self, project: str) -> dict[str, object]:
            return {
                "projects": [
                    {
                        "key": project,
                        "issuetypes": [
                            {
                                "name": "Improvement",
                                "fields": {
                                    "summary": {},
                                    "description": {},
                                    "issuetype": {},
                                    "project": {},
                                    "parent": {},
                                    "priority": {},
                                    "customfield_10071": {},
                                    "reporter": {},
                                },
                            }
                        ],
                    }
                ]
            }

        def myself(self) -> dict[str, str]:
            return {"accountId": "me"}

        def issue_create(self, fields: dict[str, object]) -> dict[str, str]:
            self.created_fields = fields
            return {"key": "SAT-900"}

        def get_issue(self, issue_id_or_key: str, fields: object = None) -> dict[str, object]:
            return {
                "key": issue_id_or_key,
                "fields": {
                    "summary": "Create resource model",
                    "updated": "2026-07-22T00:00:00.000+0000",
                    "customfield_10071": {"value": "resource-as-code"},
                },
            }

    client = Client()
    monkeypatch.setattr(jira_workbench.cli, "jira_api_client", lambda _config: client)
    config_path = tmp_path / "jira-wb.conf"
    jira_dir = tmp_path / "jira"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                f'jira_dir = "{jira_dir}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                'component_field = "customfield_10071"',
            ]
        )
        + "\n"
    )

    code = main(
        [
            "--config",
            str(config_path),
            "issue",
            "create",
            "--summary",
            "Create resource model",
            "--type",
            "Improvement",
            "--component",
            "resource-as-code",
            "--parent",
            "SAT-749",
            "--priority",
            "High",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "created SAT-900" in captured.out
    assert client.created_fields is not None
    assert client.created_fields["parent"] == {"key": "SAT-749"}
    assert client.created_fields["priority"] == {"name": "High"}
    assert (jira_dir / "components/resource-as-code/SAT-900/issue.json").exists()


def test_versions_filter_is_read_from_config(tmp_path: Path) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text("[versions]\nfilter = \"helm-chart-sa 3\\\\.[45]\"\n")

    config = load_config(config_path)

    assert config.versions_filter == "helm-chart-sa 3\\.[45]"


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


def test_meta_versions_can_list_cached_versions(tmp_path: Path, capsys) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/versions.json",
        {
            "project": "SAT",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "versions": [{"name": "helm-chart-sa 3.4.0", "released": False}],
        },
    )

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--jira-dir",
            str(jira_dir),
            "versions",
            "--cached",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "helm-chart-sa 3.4.0" in captured.out


def test_meta_boards_can_list_cached_boards(tmp_path: Path, capsys) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/boards.json",
        {
            "project": "SAT",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "boards": [
                {"name": "SAT board", "type": "simple", "unsupportedReason": None, "backlogKeys": ["SAT-1"]},
                {"name": "PS Tools", "type": "scrum", "unsupportedReason": None, "backlogKeys": None},
            ],
        },
    )

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--jira-dir",
            str(jira_dir),
            "boards",
            "--cached",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "SAT board" in captured.out
    assert "PS Tools" in captured.out


def test_meta_boards_reports_error_when_no_cache_and_cached_requested(tmp_path: Path, capsys) -> None:
    jira_dir = tmp_path / "jira"

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--jira-dir",
            str(jira_dir),
            "boards",
            "--cached",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "no cached boards found" in captured.err


def test_meta_refresh_boards_calls_refresh_boards_api(tmp_path: Path, capsys, monkeypatch) -> None:
    jira_dir = tmp_path / "jira"
    calls = []

    def fake_refresh_boards_api(jira_dir_arg, project, client, component_field):
        calls.append((project, component_field))
        return {"boards": [{"name": "SAT board"}]}

    monkeypatch.setattr(jira_workbench.cli, "refresh_boards_api", fake_refresh_boards_api)
    monkeypatch.setattr(
        jira_workbench.cli,
        "api_client_from_config",
        lambda project, jira_url, jira_email, jira_api_token: (project, object()),
    )

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--project",
            "SAT",
            "--jira-dir",
            str(jira_dir),
            "--jira-url",
            "https://example.atlassian.net",
            "--jira-email",
            "user@example.com",
            "--jira-api-token",
            "token",
            "refresh",
            "--boards",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert calls == [("SAT", "components")]
    assert "refreshed 1 boards" in captured.out


def test_meta_refresh_with_only_boards_flag_does_not_also_refresh_versions(tmp_path: Path, monkeypatch) -> None:
    jira_dir = tmp_path / "jira"
    version_calls = []

    monkeypatch.setattr(jira_workbench.cli, "refresh_versions_api", lambda *a, **k: version_calls.append(1))
    monkeypatch.setattr(jira_workbench.cli, "refresh_boards_api", lambda *a, **k: {"boards": []})
    monkeypatch.setattr(
        jira_workbench.cli,
        "api_client_from_config",
        lambda project, jira_url, jira_email, jira_api_token: (project, object()),
    )

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--project",
            "SAT",
            "--jira-dir",
            str(jira_dir),
            "--jira-url",
            "https://example.atlassian.net",
            "--jira-email",
            "user@example.com",
            "--jira-api-token",
            "token",
            "refresh",
            "--boards",
        ]
    )

    assert code == 0
    assert version_calls == []


def test_meta_components_lists_cached_manifest_components(tmp_path: Path, capsys) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "manifest.json",
        {
            "components": [
                {"component": "helm-chart", "count": 3},
                {"component": "misc", "count": 1},
            ]
        },
    )

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--jira-dir",
            str(jira_dir),
            "components",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "helm-chart" in captured.out
    assert "misc" in captured.out


def test_meta_versions_output_uses_cached_versions(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/versions.json",
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
        jira_dir / "meta/components.json",
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
        jira_dir / "meta/components.json",
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
        jira_dir / "meta/customfield_10071-options.json",
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
        jira_dir / "meta/customfield_10071-options.json",
        {
            "project": "SAT",
            "field": "customfield_10071",
            "fetchedAt": "2026-07-21T06:00:00Z",
            "options": [{"id": "10114", "value": "helm-chart"}],
        },
    )
    write_json(
        jira_dir / "meta/components.json",
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


def test_meta_doctor_reports_missing_config(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--jira-dir",
            str(tmp_path / "jira"),
            "doctor",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "jira api config" in captured.out


def test_meta_version_write_commands_require_api_config(tmp_path: Path, capsys) -> None:
    commands = [
        ["version-rename", "old", "new"],
        ["version-release", "old"],
        ["version-archive", "old"],
        ["version-delete", "old"],
    ]

    for command in commands:
        code = main(
            [
                "--config",
                str(tmp_path / "missing.conf"),
                "meta",
                "--jira-dir",
                str(tmp_path / "jira"),
                *command,
            ]
        )
        assert code == 1

    captured = capsys.readouterr()
    assert "missing required configuration for Jira API" in captured.err


def test_meta_ctrl_c_exits_without_traceback(tmp_path: Path, monkeypatch, capsys) -> None:
    def interrupt(*args, **kwargs) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(jira_workbench.cli, "run_meta", interrupt)

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--jira-dir",
            str(tmp_path / "jira"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 130
    assert "Traceback" not in captured.err

