from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jira_workbench.config import ConfigError, load_config, save_view_defaults, secure_config_permissions


@pytest.mark.skipif(os.name != "posix", reason="file permission bits are POSIX-specific")
def test_secure_config_permissions_tightens_group_and_other_readable(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('jira_api_token = "secret"\n')
    path.chmod(0o644)

    changed = secure_config_permissions(path)

    assert changed is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="file permission bits are POSIX-specific")
def test_secure_config_permissions_noop_when_already_restrictive(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('jira_api_token = "secret"\n')
    path.chmod(0o600)

    changed = secure_config_permissions(path)

    assert changed is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_config_reads_new_view_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[view]\n"
        'fix_version = "2026.07"\n'
        'assignee = "you@example.com"\n'
        'board = "SAT board"\n'
        'board_scope = "active"\n'
        "active = false\n"
    )

    config = load_config(path)

    assert config.view_fix_version == "2026.07"
    assert config.view_assignee == "you@example.com"
    assert config.view_board == "SAT board"
    assert config.view_board_scope == "active"
    assert config.view_active is False


def test_load_config_rejects_non_boolean_active(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[view]\nactive = "yes"\n')

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_reads_nerd_font_true(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[view]\nnerd_font = true\n")

    config = load_config(path)

    assert config.view_nerd_font is True


def test_load_config_nerd_font_defaults_to_none(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("")

    config = load_config(path)

    assert config.view_nerd_font is None


def test_load_config_rejects_non_boolean_nerd_font(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[view]\nnerd_font = "auto"\n')

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_reads_issue_default_type(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[issue]\ndefault_type = "Story"\n')

    config = load_config(path)

    assert config.issue_default_type == "Story"


def test_load_config_issue_default_type_is_none_when_unset(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("")

    config = load_config(path)

    assert config.issue_default_type is None


def test_save_view_defaults_creates_file_with_restrictive_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"

    save_view_defaults(path, {"component": "helm-chart", "active": False})

    config = load_config(path)
    assert config.view_component == "helm-chart"
    assert config.view_active is False
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_view_defaults_preserves_unrelated_content_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'jira_api_token = "secret"\n'
        "\n"
        "[view]\n"
        "# a comment worth keeping\n"
        'component = "helm-chart"\n'
        "preview_lines = 10\n"
    )

    save_view_defaults(path, {"board": "SAT board", "filter": "prometheus"})

    text = path.read_text()
    assert "# a comment worth keeping" in text
    assert 'jira_api_token = "secret"' in text

    config = load_config(path)
    assert config.jira_api_token == "secret"
    assert config.view_component == "helm-chart"
    assert config.view_preview_lines == 10
    assert config.view_board == "SAT board"
    assert config.view_filter == "prometheus"


def test_save_view_defaults_clears_key_on_none(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[view]\nboard = "SAT board"\nboard_scope = "active"\n')

    save_view_defaults(path, {"board": None, "board_scope": None})

    config = load_config(path)
    assert config.view_board is None
    assert config.view_board_scope is None


def test_save_view_defaults_rejects_unparseable_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("this is not [ valid toml")

    with pytest.raises(ConfigError):
        save_view_defaults(path, {"component": "helm-chart"})
