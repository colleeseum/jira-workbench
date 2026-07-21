from __future__ import annotations

from pathlib import Path

from jira_workbench.cli import main
from jira_workbench.shadow import add_comment, set_field
from jira_workbench.sync import SyncConfig, sync_project
from jira_workbench.view import format_work_item, text_from_adf
from test_sync import FakeRunner


def synced_jira_dir(tmp_path: Path) -> Path:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeRunner(),
        progress=None,
    )
    return tmp_path


def test_format_work_item_defaults_to_shadow_when_present(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")
    add_comment(jira_dir, "SAT-1", "Local comment")

    output = format_work_item(jira_dir, "SAT-1", component_field="customfield_10071")

    assert "Local shadow" in output
    assert "Local description" in output
    assert "- [working] Local comment" in output


def test_format_work_item_original_ignores_shadow(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")

    output = format_work_item(jira_dir, "SAT-1", component_field="customfield_10071", mode="original")

    assert "Local shadow" not in output
    assert "Local description" not in output


def test_format_work_item_diff_shows_shadow_diff(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")

    output = format_work_item(jira_dir, "SAT-1", component_field="customfield_10071", mode="diff")

    assert "SAT-1 (working)" in output
    assert "field: description" in output
    assert "+\"Local description\"" in output


def test_cli_view_key_uses_local_shadow(tmp_path: Path, capsys) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    set_field(jira_dir, "SAT-1", "description", "Local description")

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "view",
            "SAT-1",
            "--jira-dir",
            str(jira_dir),
            "--component-field",
            "customfield_10071",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "Local shadow" in captured.out
    assert "Local description" in captured.out


def test_cli_view_diff_requires_key(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "view",
            "--jira-dir",
            str(tmp_path / "jira"),
            "--diff",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--diff and --original require a work item key" in captured.err


def test_text_from_adf_keeps_inline_paragraph_text_together() -> None:
    value = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Use "},
            {"type": "text", "text": "sd-stack"},
            {"type": "text", "text": " release"},
        ],
    }

    assert text_from_adf(value) == "Use sd-stack release"
