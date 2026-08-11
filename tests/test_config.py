from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jira_workbench.config import (
    DEFAULT_JIRA_DIR,
    ConfigError,
    ProjectSettings,
    WorkbenchConfig,
    load_config,
    resolve_jira_dir,
    save_view_defaults,
    secure_config_permissions,
)


def test_load_config_reads_projects_array(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "",
                "[[projects]]",
                'key = "OTHERPROJ"',
                "read_only = true",
            ]
        )
    )

    config = load_config(path)

    assert config.projects == (
        ProjectSettings(key="SAT", default=True, read_only=False),
        ProjectSettings(key="OTHERPROJ", default=False, read_only=True),
    )


def test_load_config_rejects_two_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "",
                "[[projects]]",
                'key = "OTHERPROJ"',
                "default = true",
            ]
        )
    )

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_project_entry_without_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[projects]]\nread_only = true\n')

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_reads_history_months(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "sync_history_months = 12",
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                "",
                "[[projects]]",
                'key = "HUGEPROJ"',
                "history_months = 6",
            ]
        )
    )

    config = load_config(path)

    assert config.sync_history_months == 12
    assert config.projects == (
        ProjectSettings(key="SAT", default=True),
        ProjectSettings(key="HUGEPROJ", history_months=6),
    )


def test_load_config_rejects_non_positive_top_level_history_months(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("sync_history_months = 0\n")

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_non_positive_project_history_months(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[projects]]\nkey = "SAT"\nhistory_months = -1\n')

    with pytest.raises(ConfigError):
        load_config(path)


def test_effective_history_months_prefers_project_override_over_global_default() -> None:
    config = WorkbenchConfig(
        sync_history_months=24,
        projects=(ProjectSettings(key="SAT"), ProjectSettings(key="HUGEPROJ", history_months=6)),
    )

    assert config.effective_history_months("HUGEPROJ") == 6
    assert config.effective_history_months("SAT") == 24


def test_effective_history_months_falls_back_to_global_default_for_unknown_project() -> None:
    config = WorkbenchConfig(sync_history_months=24)

    assert config.effective_history_months("SOMETHING") == 24


def test_effective_history_months_none_when_nothing_configured() -> None:
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT"),))

    assert config.effective_history_months("SAT") is None


def test_load_config_reads_project_exclude_assignees(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[[projects]]",
                'key = "SAT"',
                'exclude_assignees = ["Matt Petrillo", "Rafael Campo"]',
            ]
        )
    )

    config = load_config(path)

    assert config.projects == (ProjectSettings(key="SAT", exclude_assignees=("Matt Petrillo", "Rafael Campo")),)


def test_load_config_rejects_non_string_exclude_assignees(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[projects]]\nkey = "SAT"\nexclude_assignees = [1, 2]\n')

    with pytest.raises(ConfigError):
        load_config(path)


def test_excluded_assignees_is_case_insensitive() -> None:
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT", exclude_assignees=("Matt Petrillo",)),))

    assert config.excluded_assignees("SAT") == {"matt petrillo"}


def test_excluded_assignees_empty_for_unknown_project_or_unconfigured() -> None:
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT"),))

    assert config.excluded_assignees("SAT") == frozenset()


def test_load_config_reads_project_fix_version_single_select(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[projects]]\nkey = "SAT"\nfix_version_single_select = true\n')

    config = load_config(path)

    assert config.projects == (ProjectSettings(key="SAT", fix_version_single_select=True),)


def test_load_config_rejects_non_bool_fix_version_single_select(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[projects]]\nkey = "SAT"\nfix_version_single_select = "yes"\n')

    with pytest.raises(ConfigError):
        load_config(path)


def test_fix_version_single_select_project_defaults_to_false() -> None:
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT"),))

    assert config.fix_version_single_select_project("SAT") is False
    assert config.fix_version_single_select_project("UNKNOWN") is False


def test_fix_version_single_select_project_reads_the_matching_project() -> None:
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT", fix_version_single_select=True), ProjectSettings(key="PLAT"))
    )

    assert config.fix_version_single_select_project("SAT") is True
    assert config.fix_version_single_select_project("PLAT") is False
    assert config.excluded_assignees("SOMETHING") == frozenset()


def test_load_config_reads_project_component_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                'component_field = "customfield_10071"',
                "[[projects]]",
                'key = "SAT"',
                "default = true",
                'component_field = "customfield_10071"',
                "",
                "[[projects]]",
                'key = "PLAT"',
                "read_only = true",
            ]
        )
    )

    config = load_config(path)

    assert config.projects == (
        ProjectSettings(key="SAT", default=True, component_field="customfield_10071"),
        ProjectSettings(key="PLAT", read_only=True),
    )


def test_load_config_rejects_blank_project_component_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[projects]]\nkey = "SAT"\ncomponent_field = ""\n')

    with pytest.raises(ConfigError):
        load_config(path)


def test_effective_component_field_uses_project_override_with_no_cross_project_fallback() -> None:
    config = WorkbenchConfig(
        component_field="customfield_10071",
        projects=(
            ProjectSettings(key="SAT", component_field="customfield_10071"),
            ProjectSettings(key="PLAT"),
        ),
    )

    assert config.effective_component_field("SAT") == "customfield_10071"
    assert config.effective_component_field("PLAT") is None
    assert config.effective_component_field("UNKNOWN") is None


def test_effective_component_field_falls_back_to_flat_value_in_legacy_single_project_config() -> None:
    config = WorkbenchConfig(project="SAT", component_field="customfield_10071")

    assert config.effective_component_field("SAT") == "customfield_10071"


def test_resolved_projects_falls_back_to_flat_project_key() -> None:
    config = WorkbenchConfig(project="SAT")

    assert config.resolved_projects() == (ProjectSettings(key="SAT", default=True),)


def test_resolved_projects_empty_when_nothing_configured() -> None:
    assert WorkbenchConfig().resolved_projects() == ()


def test_default_project_key_prefers_explicit_default() -> None:
    config = WorkbenchConfig(
        projects=(ProjectSettings(key="SAT"), ProjectSettings(key="OTHERPROJ", default=True))
    )

    assert config.default_project_key() == "OTHERPROJ"


def test_default_project_key_is_the_sole_project_when_unmarked() -> None:
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT"),))

    assert config.default_project_key() == "SAT"


def test_default_project_key_is_none_when_ambiguous() -> None:
    config = WorkbenchConfig(projects=(ProjectSettings(key="SAT"), ProjectSettings(key="OTHERPROJ")))

    assert config.default_project_key() is None


def test_read_only_project_keys_collects_only_read_only_entries() -> None:
    config = WorkbenchConfig(
        projects=(
            ProjectSettings(key="SAT", default=True),
            ProjectSettings(key="OTHERPROJ", read_only=True),
        )
    )

    assert config.read_only_project_keys() == frozenset({"OTHERPROJ"})


def test_resolve_jira_dir_prefers_explicit_flag_over_config() -> None:
    assert resolve_jira_dir("/from/flag", "/from/config") == Path("/from/flag")


def test_resolve_jira_dir_falls_back_to_config_value() -> None:
    assert resolve_jira_dir(None, "/from/config") == Path("/from/config")


def test_resolve_jira_dir_defaults_to_shared_data_dir_when_unset() -> None:
    assert resolve_jira_dir(None, None) == DEFAULT_JIRA_DIR


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

    assert config.view_fix_version == ("2026.07",)
    assert config.view_assignee == ("you@example.com",)
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


def test_load_config_reads_dev_status_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[view]\ndev_status_field = "customfield_10099"\n')

    config = load_config(path)

    assert config.view_dev_status_field == "customfield_10099"


def test_load_config_dev_status_field_defaults_to_none(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("")

    config = load_config(path)

    assert config.view_dev_status_field is None


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
    assert config.view_component == ("helm-chart",)
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
    assert config.view_component == ("helm-chart",)
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


def test_load_config_reads_array_view_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[view]\n"
        'project = ["SAT", "PLAT"]\n'
        'status = ["To Do", "In Progress"]\n'
        'component = ["helm-chart", "docs"]\n'
    )

    config = load_config(path)

    assert config.view_project == ("SAT", "PLAT")
    assert config.view_status == ("To Do", "In Progress")
    assert config.view_component == ("helm-chart", "docs")


def test_load_config_view_project_and_status_default_to_none(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("")

    config = load_config(path)

    assert config.view_project is None
    assert config.view_status is None


def test_load_config_rejects_empty_array_view_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[view]\nproject = []\n")

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_non_string_array_entries(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[view]\nproject = [1, 2]\n")

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_non_string_non_array_view_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[view]\nproject = 42\n")

    with pytest.raises(ConfigError):
        load_config(path)


def test_save_view_defaults_round_trips_list_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    save_view_defaults(path, {"project": ["SAT", "PLAT"], "status": ["Done"]})

    config = load_config(path)
    assert config.view_project == ("SAT", "PLAT")
    assert config.view_status == ("Done",)


def test_save_view_defaults_clears_key_on_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[view]\nproject = ["SAT"]\n')

    save_view_defaults(path, {"project": []})

    config = load_config(path)
    assert config.view_project is None


def test_save_view_defaults_rejects_unparseable_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("this is not [ valid toml")

    with pytest.raises(ConfigError):
        save_view_defaults(path, {"component": "helm-chart"})
