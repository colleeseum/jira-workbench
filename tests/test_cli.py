from __future__ import annotations

from pathlib import Path

from jira_workbench.cli import main


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
    assert "jira-wb 0.1.0" in captured.out


def test_sync_missing_acli_prints_friendly_error(tmp_path: Path, capsys) -> None:
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
            "--acli",
            "definitely-missing-acli",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "error: Atlassian CLI executable 'definitely-missing-acli' was not found" in captured.err
    assert "Traceback" not in captured.err


def test_sync_requires_config_or_flags(tmp_path: Path, capsys) -> None:
    code = main(["--config", str(tmp_path / "missing.conf"), "sync"])

    captured = capsys.readouterr()
    assert code == 2
    assert "missing required configuration: project, component_field, jira_dir, acli" in captured.err


def test_sync_reads_config_file_and_flags_override(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "jira-wb.conf"
    config_path.write_text(
        "\n".join(
            [
                'project = "CFG"',
                'component_field = "customfield_cfg"',
                f'jira_dir = "{tmp_path / "jira"}"',
                'acli = "definitely-missing-acli"',
            ]
        )
        + "\n"
    )

    code = main(["--config", str(config_path), "sync", "--project", "FLAG"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Atlassian CLI executable 'definitely-missing-acli' was not found" in captured.err
    assert "missing required configuration" not in captured.err
