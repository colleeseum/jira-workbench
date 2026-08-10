from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import jira_workbench.cli
import jira_workbench.service
from jira_workbench.cli import (
    main,
    sync_progress_printer,
)
from jira_workbench.config import ConfigError, load_config
from jira_workbench.metadata import remember_field_names
from jira_workbench.shadow import load_shadow
from jira_workbench.sync import write_json


def test_cli_help(tmp_path: Path, capsys) -> None:
    # Explicit --config keeps this isolated from whatever real config.toml
    # (if any) exists on the machine running the tests -- main([]) with no
    # subcommand still loads config before printing help, so an invalid
    # real config would otherwise fail this test for a reason that has
    # nothing to do with what it's actually checking.
    assert main(["--config", str(tmp_path / "missing.conf")]) == 0
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


def test_sync_requires_project(tmp_path: Path, capsys) -> None:
    code = main(["--config", str(tmp_path / "missing.conf"), "sync"])

    captured = capsys.readouterr()
    assert code == 2
    assert "missing required configuration: project" in captured.err


def test_sync_uses_default_jira_dir_when_unset(tmp_path: Path, monkeypatch) -> None:
    from jira_workbench.config import DEFAULT_JIRA_DIR

    captured_configs = []

    def fake_sync_project(config, client, progress=None):
        captured_configs.append(config)
        from jira_workbench.sync import SyncResult

        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
            ]
        )
    )

    assert main(["--config", str(config_path), "sync"]) == 0

    assert captured_configs[0].jira_dir == DEFAULT_JIRA_DIR


class _FakeStream:
    def __init__(self) -> None:
        self.output = ""

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> None:
        self.output += text

    def flush(self) -> None:
        pass


def test_sync_progress_printer_rewrites_issue_progress_on_tty() -> None:
    stream = _FakeStream()
    progress = sync_progress_printer(stream)

    progress("[3/6] Syncing changed issues... 1/2 SAT-1 changed=0 unchanged=0")
    progress("[3/6] Syncing changed issues... 2/2 SAT-2 changed=1 unchanged=0")
    progress("[4/6] Building manifest...")

    assert "\r[3/6] Syncing changed issues... 1/2 SAT-1 changed=0 unchanged=0" in stream.output
    assert "\r[3/6] Syncing changed issues... 2/2 SAT-2 changed=1 unchanged=0" in stream.output
    assert "\n[4/6] Building manifest...\n" in stream.output


def test_sync_progress_printer_prefixes_every_line_with_the_project() -> None:
    stream = _FakeStream()
    progress = sync_progress_printer(stream, project="PLAT")

    progress("[1/6] Refreshing project metadata...")

    assert stream.output == "PLAT [1/6] Refreshing project metadata...\n"


def test_sync_progress_printer_project_prefix_also_applies_to_tty_rewrite() -> None:
    stream = _FakeStream()
    progress = sync_progress_printer(stream, project="PLAT")

    progress("[3/6] Syncing changed issues... 1/2 PLAT-1 changed=0 unchanged=0")

    assert "\rPLAT [3/6] Syncing changed issues... 1/2 PLAT-1 changed=0 unchanged=0" in stream.output


def test_sync_force_flag_threads_through_to_sync_config(tmp_path: Path, monkeypatch) -> None:
    captured_configs = []

    def fake_sync_project(config, client, progress=None):
        captured_configs.append(config)
        from jira_workbench.sync import SyncResult

        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
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
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: client)
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


def test_sync_with_no_explicit_project_syncs_every_configured_project(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from jira_workbench.metadata import load_project_registry
    from jira_workbench.sync import SyncResult

    synced_projects: list[str] = []

    def fake_sync_project(config, client, progress=None):
        synced_projects.append(config.project)
        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    jira_dir = tmp_path / "jira"
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                f'jira_dir = "{jira_dir}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "[[projects]]",
                'key = "OTHERPROJ"',
                "read_only = true",
            ]
        )
    )

    code = main(["--config", str(config_path), "sync"])

    captured = capsys.readouterr()
    assert code == 0
    assert synced_projects == ["SAT", "OTHERPROJ"]
    assert "Synced SAT: 0 work items" in captured.out
    assert "Synced OTHERPROJ: 0 work items" in captured.out
    assert load_project_registry(jira_dir) == {
        "SAT": {"readOnly": False, "default": True},
        "OTHERPROJ": {"readOnly": True, "default": False},
    }


def test_sync_multi_project_run_prefixes_progress_lines_with_the_project(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from jira_workbench.sync import SyncResult

    def fake_sync_project(config, client, progress=None):
        if progress:
            progress("[1/6] Refreshing project metadata...")
        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                f'jira_dir = "{tmp_path / "jira"}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "[[projects]]",
                'key = "OTHERPROJ"',
            ]
        )
    )

    code = main(["--config", str(config_path), "sync"])

    captured = capsys.readouterr()
    assert code == 0
    assert "SAT [1/6] Refreshing project metadata..." in captured.err
    assert "OTHERPROJ [1/6] Refreshing project metadata..." in captured.err


def test_sync_single_project_run_does_not_prefix_progress_lines(tmp_path: Path, capsys, monkeypatch) -> None:
    from jira_workbench.sync import SyncResult

    def fake_sync_project(config, client, progress=None):
        if progress:
            progress("[1/6] Refreshing project metadata...")
        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                f'jira_dir = "{tmp_path / "jira"}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
            ]
        )
    )

    code = main(["--config", str(config_path), "sync"])

    captured = capsys.readouterr()
    assert code == 0
    assert "[1/6] Refreshing project metadata...\n" in captured.err
    assert "SAT [1/6]" not in captured.err


def test_sync_writes_project_registry_even_for_a_single_project_sync(
    tmp_path: Path, monkeypatch
) -> None:
    from jira_workbench.metadata import load_project_registry
    from jira_workbench.sync import SyncResult

    def fake_sync_project(config, client, progress=None):
        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    jira_dir = tmp_path / "jira"
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                f'jira_dir = "{jira_dir}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
            ]
        )
    )

    assert main(["--config", str(config_path), "sync"]) == 0

    assert load_project_registry(jira_dir) == {"SAT": {"readOnly": False, "default": True}}


def test_sync_uses_per_project_history_months_falling_back_to_the_global_default(
    tmp_path: Path, monkeypatch
) -> None:
    from jira_workbench.sync import SyncResult

    captured_configs = []

    def fake_sync_project(config, client, progress=None):
        captured_configs.append(config)
        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    jira_dir = tmp_path / "jira"
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                f'jira_dir = "{jira_dir}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "sync_history_months = 24",
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "[[projects]]",
                'key = "HUGEPROJ"',
                "history_months = 6",
            ]
        )
    )

    assert main(["--config", str(config_path), "sync"]) == 0

    by_project = {config.project: config for config in captured_configs}
    assert by_project["SAT"].history_months == 24
    assert by_project["HUGEPROJ"].history_months == 6


def test_sync_uses_per_project_component_field_without_leaking_across_projects(
    tmp_path: Path, monkeypatch
) -> None:
    from jira_workbench.sync import SyncResult

    captured_configs = []

    def fake_sync_project(config, client, progress=None):
        captured_configs.append(config)
        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    jira_dir = tmp_path / "jira"
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                f'jira_dir = "{jira_dir}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                'component_field = "customfield_10071"',
                "[[projects]]",
                'key = "PLAT"',
                "read_only = true",
            ]
        )
    )

    assert main(["--config", str(config_path), "sync"]) == 0

    by_project = {config.project: config for config in captured_configs}
    assert by_project["SAT"].component_field == "customfield_10071"
    assert by_project["PLAT"].component_field == "components"


def test_sync_history_months_flag_overrides_config_for_every_project(tmp_path: Path, monkeypatch) -> None:
    from jira_workbench.sync import SyncResult

    captured_configs = []

    def fake_sync_project(config, client, progress=None):
        captured_configs.append(config)
        return SyncResult(work_item_count=0, changed_count=0, skipped_count=0, version_count=0)

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
    monkeypatch.setattr(jira_workbench.cli, "sync_project", fake_sync_project)
    jira_dir = tmp_path / "jira"
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                f'jira_dir = "{jira_dir}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "sync_history_months = 24",
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "[[projects]]",
                'key = "HUGEPROJ"',
                "history_months = 6",
            ]
        )
    )

    assert main(["--config", str(config_path), "sync", "--history-months", "3"]) == 0

    assert all(config.history_months == 3 for config in captured_configs)


def test_sync_rejects_non_positive_history_months_flag(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "sync",
            "--project",
            "SAT",
            "--jira-dir",
            str(tmp_path / "jira"),
            "--history-months",
            "0",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--history-months must be a positive integer" in captured.err


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
    assert calls[0][1]["component"] == ("helm-chart",)
    assert calls[0][1]["fix_version"] == ("2026.07",)
    assert calls[0][1]["assignee"] == ("you@example.com",)
    assert calls[0][1]["board"] == "SAT board"
    assert calls[0][1]["board_scope"] == "active"
    assert calls[0][1]["pattern"] == "prometheus"
    assert calls[0][1]["swimlane"] == "epic"
    assert calls[0][1]["active"] is False
    assert calls[0][1]["preview_lines"] == 20
    assert calls[0][1]["config_path"] == config_path
    assert calls[0][1]["nerd_font"] is True
    assert calls[0][1]["dev_status_field"] == "customfield_10099"


def test_view_resolves_default_project_from_projects_array(tmp_path: Path, monkeypatch) -> None:
    # A [[projects]] config (no legacy top-level `project = "..."` key) must
    # still resolve a project for the TUI -- otherwise self.app.project stays
    # None and Meta > Boards/Versions wrongly report "no cached boards found
    # and live metadata access is not configured" even though a default
    # project is clearly configured and already synced.
    config_path = tmp_path / "jira-wb.conf"
    jira_dir = tmp_path / "jira"
    config_path.write_text(
        "\n".join(
            [
                f'jira_dir = "{jira_dir}"',
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "[[projects]]",
                'key = "OTHERPROJ"',
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
    assert calls[0][1]["project"] == "SAT"


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

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: Client())
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


def test_issue_create_blocked_for_a_read_only_project(tmp_path: Path, capsys, monkeypatch) -> None:
    from jira_workbench.config import ProjectSettings
    from jira_workbench.metadata import write_project_registry

    class Client:
        def issue_createmeta(self, project: str) -> dict[str, object]:
            raise AssertionError("should not fetch createmeta for a read-only project")

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: Client())
    jira_dir = tmp_path / "jira"
    write_project_registry(jira_dir, (ProjectSettings(key="SAT", read_only=True),))
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                f'jira_dir = "{jira_dir}"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
            ]
        )
    )

    code = main(
        ["--config", str(config_path), "issue", "create", "--summary", "New issue", "--type", "Task"]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "project SAT is read-only" in captured.err


def test_issue_create_requires_type_when_no_flag_or_config_default(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: object())
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

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: Client())
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

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: Client())
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
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: client)
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


def test_version_filters_by_component_is_read_from_config(tmp_path: Path) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                "[versions.by_component.SAT]",
                'helm-chart = "helm-chart-sa"',
                'terraform = "infra-\\\\d+"',
                "[versions.by_component.PLAT]",
                'helm-chart = "plat-helm"',
            ]
        )
        + "\n"
    )

    config = load_config(config_path)

    assert config.version_filters_by_component == {
        "SAT": {"helm-chart": "helm-chart-sa", "terraform": "infra-\\d+"},
        "PLAT": {"helm-chart": "plat-helm"},
    }


def test_version_filters_by_component_defaults_to_empty(tmp_path: Path) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text("")

    config = load_config(config_path)

    assert config.version_filters_by_component == {}


def test_version_filters_by_component_rejects_non_table(tmp_path: Path) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text('[versions]\nby_component = "not a table"\n')

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_version_filters_by_component_rejects_non_table_project_entry(tmp_path: Path) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text("[versions.by_component]\nSAT = 3\n")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_version_filters_by_component_rejects_non_string_value(tmp_path: Path) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text("[versions.by_component.SAT]\nhelm-chart = 3\n")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_version_filters_by_component_rejects_invalid_regex(tmp_path: Path) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text('[versions.by_component.SAT]\nhelm-chart = "["\n')

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_meta_versions_can_list_cached_versions(tmp_path: Path, capsys) -> None:
    jira_dir = tmp_path / "jira"
    write_json(
        jira_dir / "meta/SAT/versions.json",
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
        jira_dir / "meta/SAT/boards.json",
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


def test_meta_refresh_with_no_flags_refreshes_everything(tmp_path: Path, capsys, monkeypatch) -> None:
    # Regression: a bare `meta refresh` used to silently refresh versions
    # only -- components/boards/assignees looked untouched with no
    # indication anything had been skipped. No flags now means "refresh
    # everything"; naming specific flags still means "only those" (see the
    # "only X flag" tests below/above).
    jira_dir = tmp_path / "jira"
    calls = []

    monkeypatch.setattr(jira_workbench.cli, "refresh_versions_api", lambda *a, **k: calls.append("versions") or {"versions": []})
    monkeypatch.setattr(jira_workbench.cli, "refresh_components_api", lambda *a, **k: calls.append("components") or {"components": []})
    monkeypatch.setattr(jira_workbench.cli, "refresh_boards_api", lambda *a, **k: calls.append("boards") or {"boards": []})
    monkeypatch.setattr(jira_workbench.cli, "refresh_assignees_api", lambda *a, **k: calls.append("assignees") or {"assignees": []})
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
        ]
    )

    assert code == 0
    assert set(calls) == {"versions", "components", "boards", "assignees"}


def test_meta_refresh_assignees_calls_refresh_assignees_api(tmp_path: Path, capsys, monkeypatch) -> None:
    jira_dir = tmp_path / "jira"
    calls = []

    def fake_refresh_assignees_api(jira_dir_arg, project, client):
        calls.append(project)
        return {"assignees": [{"displayName": "Serge Colle"}]}

    monkeypatch.setattr(jira_workbench.cli, "refresh_assignees_api", fake_refresh_assignees_api)
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
            "--assignees",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert calls == ["SAT"]
    assert "refreshed 1 assignable users" in captured.out


def test_meta_refresh_assignees_warns_on_stderr_when_degraded_to_the_fallback_api(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    jira_dir = tmp_path / "jira"

    monkeypatch.setattr(
        jira_workbench.cli,
        "refresh_assignees_api",
        lambda *a, **k: {"assignees": [{"displayName": "Serge Colle"}], "source": "role-api-fallback"},
    )
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
            "--assignees",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "refreshed 1 assignable users" in captured.out
    assert "warning" in captured.err.lower()
    assert "fell back" in captured.err.lower()


def test_meta_refresh_assignees_no_warning_when_the_internal_api_succeeds(tmp_path: Path, capsys, monkeypatch) -> None:
    jira_dir = tmp_path / "jira"

    monkeypatch.setattr(
        jira_workbench.cli,
        "refresh_assignees_api",
        lambda *a, **k: {"assignees": [{"displayName": "Serge Colle"}], "source": "internal-access-api"},
    )
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
            "--assignees",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""


def test_meta_refresh_with_only_assignees_flag_does_not_also_refresh_versions(tmp_path: Path, monkeypatch) -> None:
    jira_dir = tmp_path / "jira"
    version_calls = []

    monkeypatch.setattr(jira_workbench.cli, "refresh_versions_api", lambda *a, **k: version_calls.append(1))
    monkeypatch.setattr(jira_workbench.cli, "refresh_assignees_api", lambda *a, **k: {"assignees": []})
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
            "--assignees",
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


def test_meta_version_add_blocked_for_a_read_only_project(tmp_path: Path, capsys) -> None:
    from jira_workbench.config import ProjectSettings
    from jira_workbench.metadata import write_project_registry

    jira_dir = tmp_path / "jira"
    write_project_registry(jira_dir, (ProjectSettings(key="SAT", read_only=True),))

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "meta",
            "--project",
            "SAT",
            "--jira-dir",
            str(jira_dir),
            "version-add",
            "v1",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "project SAT is read-only" in captured.err


class _FakeVersionClient:
    """Just enough of a Jira client for version-add/rename/release/archive
    to succeed end to end -- proves `meta version-*` genuinely reaches the
    extracted service.py functions via main()'s real argparse dispatch, not
    just via direct unit calls or the TUI's own separate test coverage."""

    def __init__(self) -> None:
        self.versions: list[dict[str, object]] = [{"id": "10000", "name": "v1", "released": False, "archived": False}]

    def project(self, key: str) -> dict[str, str]:
        return {"id": "1", "key": key}

    def get_project_versions(self, key: str) -> object:
        return self.versions

    def add_version(self, key: str, project_id: str, version: str, **kwargs: object) -> object:
        self.versions.append({"id": "10001", "name": version, "released": False, "archived": False})
        return {"name": version}

    def update_version(self, version: str, **kwargs: object) -> object:
        renamed = {"is_released": "released", "is_archived": "archived"}
        for item in self.versions:
            if item["id"] == version:
                for key, value in kwargs.items():
                    if value is not None:
                        item[renamed.get(key, key)] = value
        return {"id": version}


def test_meta_version_add_rename_release_archive_reach_the_service_layer(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    client = _FakeVersionClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: client)
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "SAT"',
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
            ]
        )
    )
    common = ["--config", str(config_path), "meta", "--jira-dir", str(tmp_path / "jira")]

    assert main([*common, "version-add", "v2"]) == 0
    assert "created version v2" in capsys.readouterr().out

    assert main([*common, "version-rename", "10001", "v2-renamed"]) == 0
    assert "renamed version 10001 to v2-renamed" in capsys.readouterr().out

    assert main([*common, "version-release", "10001"]) == 0
    assert "released version 10001" in capsys.readouterr().out

    assert main([*common, "version-archive", "10001"]) == 0
    assert "archived version 10001" in capsys.readouterr().out


def test_meta_refresh_resolves_default_project_from_projects_array(tmp_path: Path, monkeypatch, capsys) -> None:
    # Regression: meta (like issue) used to resolve its project only from
    # the legacy flat `project = "..."` config key, so a [[projects]]-only
    # config (no flat key at all) failed with "missing required
    # configuration for Jira API: project" even with a clear default
    # project configured -- same bug the view command already had fixed.
    client = _FakeVersionClient()
    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: client)
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "[[projects]]",
                'key = "OTHERPROJ"',
            ]
        )
    )

    code = main(["--config", str(config_path), "meta", "--jira-dir", str(tmp_path / "jira"), "refresh", "--versions"])

    assert code == 0
    assert "refreshed 1 versions" in capsys.readouterr().out


def test_issue_create_resolves_default_project_from_projects_array(tmp_path: Path, monkeypatch, capsys) -> None:
    class Client:
        def issue_createmeta(self, project: str) -> dict[str, object]:
            return {
                "projects": [
                    {
                        "key": project,
                        "issuetypes": [
                            {"name": "Task", "fields": {"summary": {}, "description": {}, "issuetype": {}}}
                        ],
                    }
                ]
            }

        def myself(self) -> dict[str, str]:
            return {"accountId": "me"}

    monkeypatch.setattr(jira_workbench.service, "jira_api_client", lambda _config: Client())
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'jira_url = "https://example.atlassian.net"',
                'jira_email = "user@example.com"',
                'jira_api_token = "token"',
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "[[projects]]",
                'key = "OTHERPROJ"',
            ]
        )
    )

    code = main(
        [
            "--config",
            str(config_path),
            "issue",
            "--jira-dir",
            str(tmp_path / "jira"),
            "create",
            "--type",
            "Task",
            "--summary",
            "hi",
            "--description",
            "hi",
            "--dry-run",
        ]
    )

    assert code == 0


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

