from __future__ import annotations

from pathlib import Path

from jira_workbench.cli import main
from jira_workbench.shadow import add_comment, load_shadow, set_field, set_status_change
from jira_workbench.sync import SyncConfig, sync_project, write_json
from jira_workbench.view import (
    HELP_LINES,
    comments_text,
    component_counts,
    cycle_component,
    cycle_fix_version,
    cycle_swimlane,
    detail_field_rows,
    draw_detail,
    detail_body_line_segments,
    draw_index,
    detail_body_lines,
    draw_push_all_progress,
    draw_push_all_result,
    edit_line_value,
    editable_field_choices,
    editable_detail_fields,
    encode_edit_value,
    filter_items,
    find_item_index,
    find_next_item_index,
    format_issue,
    format_work_item,
    index_header,
    item_index_by_key,
    index_row_lines,
    index_title,
    index_item_parent_key,
    is_open_parent_key,
    is_push_all_key,
    load_manifest_items,
    issue_parent_key,
    modified_issue_keys,
    next_selectable_index,
    parent_options,
    previous_selectable_index,
    push_all_report_lines,
    refresh_index_item,
    refresh_stale_index_items,
    resolution_options,
    selectable_field_options,
    sort_items_for_swimlane,
    swimlane_header,
    swimlane_label,
    textbox_geometry,
    text_from_adf,
    version_options,
)
from test_sync import FakeRunner


class FakeWindow:
    def __init__(self, *, height: int = 6, width: int = 80) -> None:
        self.lines: list[str] = []
        self.height = height
        self.width = width

    def erase(self) -> None:
        self.lines = []

    def getmaxyx(self) -> tuple[int, int]:
        return (self.height, self.width)

    def refresh(self) -> None:
        pass

    def addnstr(self, row: int, col: int, text: str, _limit: int, *_args: object) -> None:
        while len(self.lines) <= row:
            self.lines.append("")
        current = self.lines[row]
        if len(current) < col:
            current += " " * (col - len(current))
        self.lines[row] = current[:col] + text


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
    assert "Comments" in output
    assert "[local working] Local comment" in output


def test_push_all_key_is_shift_p_only() -> None:
    assert is_push_all_key(ord("P"))
    assert not is_push_all_key(ord("p"))
    assert not is_push_all_key(ord("V"))
    assert not is_push_all_key(22)
    assert not is_push_all_key(16)


def test_open_parent_key_is_shift_v_only() -> None:
    assert is_open_parent_key(ord("V"))
    assert not is_open_parent_key(ord("v"))
    assert not is_open_parent_key(ord("P"))
    assert not is_open_parent_key(22)


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
            "--read-only",
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


def test_cli_view_key_opens_interactive_detail_by_default(tmp_path: Path, monkeypatch) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    calls = []

    def fake_interactive_view(*args, **kwargs) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr("jira_workbench.cli.interactive_view", fake_interactive_view)

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

    assert code == 0
    assert calls[0][0][0] == jira_dir
    assert calls[0][1]["initial_key"] == "SAT-1"
    assert calls[0][1]["initial_mode"] == "shadow"


def test_cli_view_passes_swimlane(tmp_path: Path, monkeypatch) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    calls = []

    def fake_interactive_view(*args, **kwargs) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr("jira_workbench.cli.interactive_view", fake_interactive_view)

    code = main(
        [
            "--config",
            str(tmp_path / "missing.conf"),
            "view",
            "--jira-dir",
            str(jira_dir),
            "--component-field",
            "customfield_10071",
            "--swimlane",
            "version",
        ]
    )

    assert code == 0
    assert calls[0][1]["swimlane"] == "version"


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


def test_format_issue_uses_native_components_by_default() -> None:
    issue = {
        "key": "SAT-717",
        "fields": {
            "summary": "Restart on unrelated helm upgrade",
            "issuetype": {"name": "Bug"},
            "status": {"name": "In Progress"},
            "fixVersions": [{"name": "helm-chart-sa 3.5.0"}],
            "components": [{"name": "platform"}],
            "customfield_10071": {"value": "helm-chart"},
            "assignee": {"displayName": "Serge Colle"},
            "reporter": {"displayName": "Serge Colle"},
            "updated": "2026-06-16T12:47:46.582-0400",
            "created": "2026-06-16T11:26:25.043-0400",
            "description": "Body",
        },
    }

    output = format_issue(issue)
    metadata = output.split("Description", 1)[0]

    assert "Components" in metadata
    assert "platform" in metadata


def test_format_issue_can_override_component_display_field() -> None:
    issue = {
        "key": "SAT-717",
        "fields": {
            "summary": "Restart on unrelated helm upgrade",
            "issuetype": {"name": "Bug"},
            "status": {"name": "In Progress"},
            "fixVersions": [{"name": "helm-chart-sa 3.5.0"}],
            "components": [{"name": "platform"}],
            "parent": {
                "key": "SAT-689",
                "fields": {
                    "summary": (
                        "RELEASE_V1.3.0 with a long parent summary that should span "
                        "the full metadata width"
                    )
                },
            },
            "assignee": {"displayName": "Serge Colle"},
            "reporter": {"displayName": "Serge Colle"},
            "customfield_10071": {"value": "helm-chart"},
            "updated": "2026-06-16T12:47:46.582-0400",
            "created": "2026-06-16T11:26:25.043-0400",
            "description": "Body",
        },
    }

    output = format_issue(issue, component_field="customfield_10071")
    metadata = output.split("Description", 1)[0]

    assert "Fix versions" in metadata
    assert "helm-chart-sa 3.5.0" in metadata
    assert "Components" in metadata
    assert "helm-chart" in metadata
    assert "platform" not in metadata
    assert metadata.index("Fix versions") < metadata.index("Assignee")
    assert "Other fields" not in output


def test_editable_field_choices_replace_native_components_with_override_field() -> None:
    choices = editable_field_choices("customfield_10071")

    assert ("Summary", "summary") in choices
    assert ("Components", "customfield_10071") in choices
    assert ("Components", "components") not in choices
    assert ("Parent", "parent") in choices


def test_detail_field_rows_include_description_and_global_component_override() -> None:
    issue = {
        "key": "SAT-1",
        "fields": {
            "summary": "Editable title",
            "issuetype": {"name": "Task"},
            "status": {"name": "Open"},
            "priority": {"name": "Medium"},
            "customfield_10071": {"value": "helm-chart"},
            "description": "Body",
        },
    }

    rows = detail_field_rows(issue, "customfield_10071")

    assert ("Components", "customfield_10071", "helm-chart") in rows
    assert ("Description", "description", "Body") in rows
    assert ("Summary", "summary", "Editable title") in rows
    assert ("Priority", "priority", "Medium") in rows
    assert "description" in editable_detail_fields("customfield_10071")
    assert "summary" in editable_detail_fields("customfield_10071")
    assert "priority" in editable_detail_fields("customfield_10071")


def test_detail_field_rows_include_editable_summary() -> None:
    issue = {
        "key": "SAT-1",
        "fields": {
            "summary": "Editable title",
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
        },
    }

    rows = detail_field_rows(issue, "components")

    assert ("Summary", "summary", "Editable title") in rows


def test_detail_field_rows_show_parent_none_when_unset() -> None:
    issue = {
        "key": "SAT-1",
        "fields": {
            "summary": "No parent",
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
        },
    }

    rows = detail_field_rows(issue, "components")

    assert ("Parent", "parent", "(none)") in rows


def test_selectable_index_navigation_wraps_over_editable_rows() -> None:
    selectable = [0, 2, 5]

    assert next_selectable_index(selectable, 0) == 2
    assert next_selectable_index(selectable, 5) == 0
    assert previous_selectable_index(selectable, 5) == 2
    assert previous_selectable_index(selectable, 0) == 5


def test_edit_line_value_edits_existing_text_in_place() -> None:
    value, cursor, done = edit_line_value("Azure Blobbackup", 10, ord(" "))

    assert value == "Azure Blob backup"
    assert cursor == 11
    assert done is None


def test_edit_line_value_handles_navigation_delete_save_and_cancel() -> None:
    value, cursor, done = edit_line_value("Azure backup", 5, 2)
    assert (value, cursor, done) == ("Azure backup", 4, None)

    value, cursor, done = edit_line_value("Azure backup", 5, 127)
    assert (value, cursor, done) == ("Azur backup", 4, None)

    value, cursor, done = edit_line_value("Azure backup", 5, 10)
    assert (value, cursor, done) == ("Azure backup", 5, "save")

    value, cursor, done = edit_line_value("Azure backup", 5, 27)
    assert (value, cursor, done) == ("Azure backup", 5, "cancel")


def test_draw_detail_keeps_parent_hierarchy_above_editable_fields(tmp_path: Path) -> None:
    parent = {
        "key": "SAT-740",
        "fields": {"summary": "RELEASE_V1.2.0", "issuetype": {"name": "Epic"}},
    }
    child = {
        "key": "SAT-741",
        "fields": {
            "summary": "Chainguard Zookeeper",
            "issuetype": {"name": "Improvement"},
            "status": {"name": "In Progress"},
            "parent": {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0"}},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-740/issue.json", parent)
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", child)
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-741", None, "original", 0, 0, 20, 100)

    output = "\n".join(window.lines)
    assert "Epic: SAT-740 RELEASE_V1.2.0" in output
    assert "|- SAT-741 Chainguard Zookeeper" in output
    assert "> Summary" not in output
    assert "Status" in output
    child_index = window.lines.index("|- SAT-741 Chainguard Zookeeper")
    assert window.lines[child_index + 1] == ""


def test_issue_parent_key_reads_parent_field() -> None:
    issue = {"key": "SAT-741", "fields": {"parent": {"key": "SAT-740"}}}

    assert issue_parent_key(issue) == "SAT-740"
    assert issue_parent_key({"key": "SAT-740", "fields": {}}) == ""


def test_draw_detail_renders_description_block_and_other_fields(tmp_path: Path) -> None:
    issue = {
        "key": "SAT-741",
        "fields": {
            "summary": "Chainguard Zookeeper",
            "description": "First line\nSecond line",
            "customfield_99999": {"value": "static"},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", issue)
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-741", None, "original", 0, 0, 20, 100)

    output = "\n".join(window.lines)
    assert "> Description" in output
    assert "First line" in output
    assert "Second line" in output
    assert output.index("> Description") < output.index("Other fields")
    assert "Other fields (1 hidden)" in output
    assert "Press O to expand" in output
    assert "customfield_99999: static" not in output
    other_index = window.lines.index("Other fields (1 hidden)")
    assert window.lines[other_index - 1] == ""

    expanded_window = FakeWindow()
    draw_detail(expanded_window, tmp_path, "SAT-741", None, "original", 0, 0, 20, 100, show_other_fields=True)
    expanded_output = "\n".join(expanded_window.lines)
    assert "Other fields" in expanded_output
    assert "customfield_99999: static" in expanded_output
    assert window.lines[other_index - 1] == ""


def test_draw_detail_truncates_long_description_before_fields(tmp_path: Path) -> None:
    issue = {
        "key": "SAT-68",
        "fields": {
            "summary": "Document and validate OpenShift support",
            "description": "\n".join(f"Line {index}" for index in range(1, 25)),
            "issuetype": {"name": "Improvement"},
            "status": {"name": "To Do"},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-68/issue.json", issue)
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-68", None, "shadow", 0, 0, 20, 100)

    output = "\n".join(window.lines)
    assert "... " in output
    assert "more lines" in output
    assert "> Type" in output
    assert "> Status" in output


def test_draw_detail_can_expand_long_description(tmp_path: Path) -> None:
    issue = {
        "key": "SAT-68",
        "fields": {
            "summary": "Document and validate OpenShift support",
            "description": "\n".join(f"Line {index}" for index in range(1, 13)),
            "issuetype": {"name": "Improvement"},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-68/issue.json", issue)
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-68", None, "shadow", 0, 0, 30, 100, expanded_text_fields={"description"})

    output = "\n".join(window.lines)
    assert "Line 12" in output
    assert "more lines" not in output


def test_draw_detail_scrolls_expanded_description_by_rendered_lines(tmp_path: Path) -> None:
    issue = {
        "key": "SAT-68",
        "fields": {
            "summary": "Document and validate OpenShift support",
            "description": "\n".join(f"Line {index}" for index in range(1, 25)),
            "issuetype": {"name": "Improvement"},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-68/issue.json", issue)
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-68", None, "shadow", 10, 1, 12, 100, expanded_text_fields={"description"})

    output = "\n".join(window.lines)
    assert "    Line 1" not in window.lines
    assert "Line 10" in output


def test_detail_body_lines_include_full_expanded_description() -> None:
    issue = {
        "key": "SAT-68",
        "fields": {
            "summary": "Document and validate OpenShift support",
            "description": "\n".join(f"Line {index}" for index in range(1, 25)),
            "issuetype": {"name": "Improvement"},
        },
    }
    rows = detail_field_rows(issue, None)

    lines = detail_body_lines(
        issue,
        None,
        rows,
        1,
        editable_detail_fields(None),
        set(),
        12,
        100,
        False,
        {"description"},
    )

    text = "\n".join(line for line, _attr in lines)
    assert "Line 24" in text
    assert "more lines" not in text


def test_detail_body_segments_highlight_only_selected_two_column_cell() -> None:
    issue = {
        "key": "SAT-1",
        "fields": {
            "summary": "Example",
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "priority": {"name": "Medium"},
        },
    }
    rows = detail_field_rows(issue, None)
    status_index = next(index for index, (_label, field, _value) in enumerate(rows) if field == "status")

    lines = detail_body_line_segments(
        issue,
        None,
        rows,
        status_index,
        editable_detail_fields(None),
        set(),
        20,
        100,
        False,
    )

    selected_lines = [line for line in lines if any(attr for _text, attr in line)]
    assert len(selected_lines) == 1
    selected_line = selected_lines[0]
    assert len(selected_line) == 3
    assert selected_line[0][1] == 0
    assert selected_line[1][1] == 0
    assert selected_line[2][1] != 0


def test_comments_text_shows_shadow_then_latest_synced_comments(tmp_path: Path) -> None:
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", {"key": "SAT-741", "fields": {}})
    write_json(
        tmp_path / "components/helm-chart/SAT-741/comments.json",
        [
            {
                "created": "2026-01-01T00:00:00.000+0000",
                "author": {"displayName": "Older User"},
                "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "older"}]}]},
            },
            {
                "created": "2026-02-01T00:00:00.000+0000",
                "author": {"displayName": "Newer User"},
                "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "newer"}]}]},
            },
        ],
    )
    add_comment(tmp_path, "SAT-741", "local")

    text = comments_text(tmp_path, "SAT-741", load_shadow(tmp_path, "SAT-741"))

    assert text.index("[local working] local") < text.index("newer")
    assert text.index("newer") < text.index("older")


def test_draw_detail_renders_comments_as_main_block(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-741/issue.json",
        {"key": "SAT-741", "fields": {"summary": "Chainguard Zookeeper"}},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-741/comments.json",
        [{"created": "2026-02-01T00:00:00.000+0000", "body": "synced comment"}],
    )
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-741", None, "original", 0, 0, 20, 100)

    output = "\n".join(window.lines)
    assert "Comments" in output
    assert "synced comment" in output


def test_draw_detail_uses_two_columns_for_compact_fields(tmp_path: Path) -> None:
    issue = {
        "key": "SAT-741",
        "fields": {
            "summary": "Chainguard Zookeeper",
            "issuetype": {"name": "Improvement"},
            "status": {"name": "In Progress"},
            "assignee": {"displayName": "Serge Colle"},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", issue)
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-741", None, "original", 0, 0, 20, 120)

    assert any("Type" in line and "Status" in line for line in window.lines)
    description_index = window.lines.index("> Description")
    assert window.lines[description_index + 2] == ""
    assert "Type" in window.lines[description_index + 3]


def test_draw_detail_marks_shadow_modified_fields(tmp_path: Path) -> None:
    issue = {
        "key": "SAT-741",
        "fields": {
            "summary": "Chainguard Zookeeper",
            "description": "Original",
            "status": {"name": "In Progress"},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", issue)
    set_field(tmp_path, "SAT-741", "description", "Changed")
    set_field(tmp_path, "SAT-741", "status", "Done")
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-741", None, "shadow", 0, 0, 20, 120)

    output = "\n".join(window.lines)
    assert "SAT-741 [shadow modified]" in output
    assert "> Description*" in output
    assert "> Status*" in output


def test_draw_detail_title_mentions_open_parent_action(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-741/issue.json",
        {"key": "SAT-741", "fields": {"summary": "Chainguard Zookeeper"}},
    )
    window = FakeWindow()

    draw_detail(window, tmp_path, "SAT-741", None, "shadow", 0, 0, 20, 120)

    assert "V parent" in window.lines[0]
    assert "x expand" in window.lines[0]


def test_encode_edit_value_preserves_structured_jira_fields() -> None:
    assert encode_edit_value("fixVersions", "helm-chart-sa 3.4.2, helm-chart-sa 3.4.4") == [
        {"name": "helm-chart-sa 3.4.2"},
        {"name": "helm-chart-sa 3.4.4"},
    ]
    assert encode_edit_value("components", "helm-chart") == [{"name": "helm-chart"}]
    assert encode_edit_value("labels", "one, two") == ["one", "two"]
    assert encode_edit_value("parent", "SAT-740") == {"key": "SAT-740"}
    assert encode_edit_value("parent", "SAT-740 RELEASE_V1.2.0") == {"key": "SAT-740"}
    assert encode_edit_value("parent", "(none)") is None
    assert encode_edit_value("priority", "High") == {"name": "High"}
    assert encode_edit_value("customfield_10071", "helm-chart", component_field="customfield_10071") == {
        "value": "helm-chart"
    }
    assert encode_edit_value("assignee", "(unassigned)") == ""
    assert encode_edit_value("fixVersions", "(none)") == []


def test_selectable_field_options_use_local_issue_values(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)

    assert selectable_field_options(jira_dir, "status") == ["Open"]
    assert selectable_field_options(jira_dir, "priority") == ["Medium"]
    assert selectable_field_options(jira_dir, "assignee") == ["(unassigned)", "Serge Colle"]
    assert selectable_field_options(jira_dir, "customfield_10071", "customfield_10071") == ["API Team"]


def test_selectable_field_options_use_cached_components_when_available(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    write_json(
        jira_dir / "meta/components.json",
        {"components": [{"name": "zeta"}, {"name": "helm-chart"}]},
    )

    assert selectable_field_options(jira_dir, "components") == ["helm-chart", "zeta"]


def test_fix_version_options_use_cached_versions(tmp_path: Path) -> None:
    write_json(
        tmp_path / "meta/versions.json",
        {
            "versions": [
                {"id": "10001", "name": "helm-chart-sa 3.4.2", "released": True},
                {"id": "10002", "name": "helm-chart-sa 3.4.4", "archived": True},
                {"id": "10003", "name": "helm-chart-sa 3.5.0", "released": False, "archived": False},
            ]
        },
    )

    assert version_options(tmp_path) == ["(none)", "helm-chart-sa 3.5.0"]
    assert version_options(tmp_path, include_inactive=True) == [
        "(none)",
        "helm-chart-sa 3.4.2",
        "helm-chart-sa 3.4.4",
        "helm-chart-sa 3.5.0",
    ]
    assert selectable_field_options(tmp_path, "fixVersions") == [
        "(none)",
        "helm-chart-sa 3.5.0",
    ]
    assert selectable_field_options(tmp_path, "fixVersions", include_inactive_versions=True) == [
        "(none)",
        "helm-chart-sa 3.4.2",
        "helm-chart-sa 3.4.4",
        "helm-chart-sa 3.5.0",
    ]


def test_resolution_options_are_local_and_immediate() -> None:
    assert resolution_options("https://example.atlassian.net", "user@example.com", "token") == [
        "Done",
        "Won't Do",
        "Duplicate",
        "Cannot Reproduce",
    ]


def test_parent_options_use_local_issue_keys(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-740/issue.json",
        {
            "key": "SAT-740",
            "fields": {
                "summary": "RELEASE_V1.2.0",
                "issuetype": {"name": "Epic", "hierarchyLevel": 1},
                "status": {"name": "To Do"},
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-741/issue.json",
        {
            "key": "SAT-741",
            "fields": {
                "summary": "Chainguard Zookeeper",
                "issuetype": {"name": "Story", "hierarchyLevel": 0},
                "status": {"name": "To Do"},
            },
        },
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-742/issue.json",
        {
            "key": "SAT-742",
            "fields": {
                "summary": "Closed Epic",
                "issuetype": {"name": "Epic", "hierarchyLevel": 1},
                "status": {"name": "Close"},
            },
        },
    )

    assert parent_options(tmp_path) == [
        "(none)",
        "Epic: SAT-740 RELEASE_V1.2.0",
        "Story: SAT-741 Chainguard Zookeeper",
    ]
    assert parent_options(tmp_path, active=False) == [
        "(none)",
        "Epic: SAT-740 RELEASE_V1.2.0",
        "Story: SAT-741 Chainguard Zookeeper",
        "Epic: SAT-742 Closed Epic",
    ]


def test_parent_options_for_story_only_show_active_epics(tmp_path: Path) -> None:
    story = {
        "key": "SAT-741",
        "fields": {
            "summary": "Chainguard Zookeeper",
            "issuetype": {"name": "Story", "hierarchyLevel": 0},
            "status": {"name": "To Do"},
        },
    }
    write_json(
        tmp_path / "components/helm-chart/SAT-740/issue.json",
        {
            "key": "SAT-740",
            "fields": {
                "summary": "RELEASE_V1.2.0",
                "issuetype": {"name": "Epic", "hierarchyLevel": 1},
                "status": {"name": "To Do"},
            },
        },
    )
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", story)
    write_json(
        tmp_path / "components/helm-chart/SAT-742/issue.json",
        {
            "key": "SAT-742",
            "fields": {
                "summary": "Closed Epic",
                "issuetype": {"name": "Epic", "hierarchyLevel": 1},
                "status": {"name": "Close"},
            },
        },
    )

    assert selectable_field_options(tmp_path, "parent", current_issue=story) == [
        "(none)",
        "Epic: SAT-740 RELEASE_V1.2.0",
    ]
    assert selectable_field_options(tmp_path, "parent", current_issue=story, include_inactive_parents=True) == [
        "(none)",
        "Epic: SAT-740 RELEASE_V1.2.0",
        "Epic: SAT-742 Closed Epic",
    ]


def test_encode_parent_value_accepts_prefixed_parent_label() -> None:
    assert encode_edit_value("parent", "Epic: SAT-740 RELEASE_V1.2.0") == {"key": "SAT-740"}


def test_format_work_item_shows_child_under_parent_hierarchy(tmp_path: Path) -> None:
    parent = {
        "key": "SAT-740",
        "fields": {
            "summary": "RELEASE_V1.2.0",
            "issuetype": {"name": "Epic"},
            "status": {"name": "To Do"},
        },
    }
    child = {
        "key": "SAT-741",
        "fields": {
            "summary": "Chainguard Zookeeper",
            "issuetype": {"name": "Improvement"},
            "status": {"name": "In Progress"},
            "parent": {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0"}},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-740/issue.json", parent)
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", child)

    output = format_work_item(tmp_path, "SAT-741")

    assert output.startswith("Epic: SAT-740 RELEASE_V1.2.0\n|- SAT-741 Chainguard Zookeeper\n\n")
    assert "Parent:" not in output


def test_format_work_item_shows_epic_children_hierarchy(tmp_path: Path) -> None:
    parent = {
        "key": "SAT-740",
        "fields": {
            "summary": "RELEASE_V1.2.0",
            "issuetype": {"name": "Epic"},
            "status": {"name": "To Do"},
        },
    }
    first_child = {
        "key": "SAT-9",
        "fields": {
            "summary": "First numeric child",
            "parent": {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0"}},
        },
    }
    second_child = {
        "key": "SAT-100",
        "fields": {
            "summary": "Last numeric child",
            "parent": {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0"}},
        },
    }
    middle_child = {
        "key": "SAT-10",
        "fields": {
            "summary": "Middle numeric child",
            "parent": {"key": "SAT-740", "fields": {"summary": "RELEASE_V1.2.0"}},
        },
    }
    write_json(tmp_path / "components/helm-chart/SAT-740/issue.json", parent)
    write_json(tmp_path / "components/helm-chart/SAT-9/issue.json", first_child)
    write_json(tmp_path / "components/helm-chart/SAT-10/issue.json", middle_child)
    write_json(tmp_path / "components/helm-chart/SAT-100/issue.json", second_child)

    output = format_work_item(tmp_path, "SAT-740")

    assert output.startswith(
        "Epic: SAT-740 RELEASE_V1.2.0\n"
        "|- SAT-9 First numeric child\n"
        "|- SAT-10 Middle numeric child\n"
        "|- SAT-100 Last numeric child\n\n"
    )


def test_format_work_item_epic_children_show_component(tmp_path: Path) -> None:
    parent = {
        "key": "SAT-14",
        "fields": {
            "summary": "Manage Passwords in a secure manner",
            "issuetype": {"name": "Epic"},
        },
    }
    child = {
        "key": "SAT-19",
        "fields": {
            "summary": "Link with Azure Key Vault",
            "customfield_10071": {"value": "helm-chart"},
            "parent": {
                "key": "SAT-14",
                "fields": {"summary": "Manage Passwords in a secure manner"},
            },
        },
    }
    write_json(tmp_path / "components/_unassigned/SAT-14/issue.json", parent)
    write_json(tmp_path / "components/helm-chart/SAT-19/issue.json", child)

    output = format_work_item(tmp_path, "SAT-14", component_field="customfield_10071")

    assert "|- SAT-19 [helm-chart] Link with Azure Key Vault" in output


def test_format_work_item_epic_children_use_shadow_summary(tmp_path: Path) -> None:
    parent = {
        "key": "SAT-14",
        "fields": {
            "summary": "Manage Passwords in a secure manner",
            "issuetype": {"name": "Epic"},
        },
    }
    child = {
        "key": "SAT-19",
        "fields": {
            "summary": "Link with Azure Key Vault",
            "customfield_10071": {"value": "helm-chart"},
            "parent": {
                "key": "SAT-14",
                "fields": {"summary": "Manage Passwords in a secure manner"},
            },
        },
    }
    write_json(tmp_path / "components/_unassigned/SAT-14/issue.json", parent)
    write_json(tmp_path / "components/helm-chart/SAT-19/issue.json", child)
    set_field(tmp_path, "SAT-19", "summary", "Support CSI-injected secrets from Azure Key Vault")

    output = format_work_item(tmp_path, "SAT-14", component_field="customfield_10071")

    assert "|- SAT-19 [helm-chart] Support CSI-injected secrets from Azure Key Vault" in output
    assert "Link with Azure Key Vault" not in output.split("\n\n", 1)[0]


def test_format_work_item_epic_children_use_shadow_parent(tmp_path: Path) -> None:
    parent = {
        "key": "SAT-522",
        "fields": {
            "summary": "Helm chart SSO/OIDC provider integrations",
            "issuetype": {"name": "Epic"},
        },
    }
    google = {
        "key": "SAT-368",
        "fields": {
            "summary": "Google SSO",
            "customfield_10071": {"value": "helm-chart"},
            "parent": {
                "key": "SAT-522",
                "fields": {"summary": "Helm chart SSO/OIDC provider integrations"},
            },
        },
    }
    rollout = {
        "key": "SAT-386",
        "fields": {
            "summary": "Add rollout capability",
            "customfield_10071": {"value": "helm-chart"},
            "parent": {
                "key": "SAT-522",
                "fields": {"summary": "Helm chart SSO/OIDC provider integrations"},
            },
        },
    }
    write_json(tmp_path / "components/_unassigned/SAT-522/issue.json", parent)
    write_json(tmp_path / "components/helm-chart/SAT-368/issue.json", google)
    write_json(tmp_path / "components/helm-chart/SAT-386/issue.json", rollout)
    set_field(tmp_path, "SAT-386", "parent", None)

    output = format_work_item(tmp_path, "SAT-522", component_field="customfield_10071")

    assert "|- SAT-368 [helm-chart] Google SSO" in output
    assert "SAT-386" not in output.split("\n\n", 1)[0]


def test_filter_items_filters_component_and_active_status() -> None:
    items = [
        {"key": "SAT-1", "component": "helm-chart", "status": "In Progress"},
        {"key": "SAT-2", "component": "helm-chart", "status": "Done"},
        {"key": "SAT-3", "component": "terraform", "status": "Open"},
        {"key": "SAT-4", "component": "helm-chart", "status": "Closed"},
        {"key": "SAT-5", "component": "helm-chart", "status": "Close"},
    ]

    filtered = filter_items(items, component="helm-chart")

    assert [item["key"] for item in filtered] == ["SAT-1"]


def test_filter_items_can_include_closed_items() -> None:
    items = [
        {"key": "SAT-1", "component": "helm-chart", "status": "In Progress"},
        {"key": "SAT-2", "component": "helm-chart", "status": "Done"},
    ]

    filtered = filter_items(items, component="helm-chart", active=False)

    assert [item["key"] for item in filtered] == ["SAT-1", "SAT-2"]


def test_filter_items_matches_text_pattern() -> None:
    items = [
        {"key": "SAT-1", "summary": "Prometheus support", "component": "helm-chart", "status": "Open"},
        {"key": "SAT-2", "summary": "Terraform support", "component": "terraform", "status": "Open"},
        {"key": "SAT-3", "summary": "Gateway docs", "component": "helm-chart", "status": "Done"},
    ]

    filtered = filter_items(items, component="helm-chart", pattern="prom")

    assert [item["key"] for item in filtered] == ["SAT-1"]


def test_filter_items_matches_fix_version_including_none() -> None:
    items = [
        {"key": "SAT-1", "fixVersion": "helm-chart-sa 3.4.4", "status": "Open"},
        {"key": "SAT-2", "fixVersion": "", "status": "Open"},
        {"key": "SAT-3", "status": "Open"},
    ]

    assert [item["key"] for item in filter_items(items, fix_version="helm-chart-sa 3.4.4")] == ["SAT-1"]
    assert [item["key"] for item in filter_items(items, fix_version="(none)")] == ["SAT-2", "SAT-3"]


def test_filter_items_can_show_only_modified_items() -> None:
    items = [
        {"key": "SAT-1", "summary": "Modified", "component": "helm-chart", "status": "Open"},
        {"key": "SAT-2", "summary": "Plain", "component": "helm-chart", "status": "Open"},
    ]

    filtered = filter_items(items, modified_keys={"SAT-1"}, modified_only=True)

    assert [item["key"] for item in filtered] == ["SAT-1"]


def test_find_item_index_prefers_exact_key_then_text_match() -> None:
    items = [
        {"key": "SAT-61", "summary": "Old item", "component": "helm-chart"},
        {"key": "SAT-612", "summary": "Create k8s-centric okta documentation", "component": "helm-chart"},
        {"key": "SAT-700", "summary": "Gateway docs", "component": "helm-chart"},
    ]

    assert find_item_index(items, "SAT-612") == 1
    assert find_item_index(items, "gateway") == 2
    assert find_item_index(items, "missing") is None


def test_item_index_by_key_matches_exact_key_only() -> None:
    items = [
        {"key": "SAT-61", "summary": "mentions SAT-612"},
        {"key": "SAT-612", "summary": "Create k8s-centric okta documentation"},
    ]

    assert item_index_by_key(items, "sat-612") == 1
    assert item_index_by_key(items, "SAT-999") is None


def test_find_next_item_index_wraps_forward_and_backward() -> None:
    items = [
        {"key": "SAT-1", "summary": "Gateway first"},
        {"key": "SAT-2", "summary": "Other"},
        {"key": "SAT-3", "summary": "Gateway second"},
    ]

    assert find_next_item_index(items, "gateway", 0) == 2
    assert find_next_item_index(items, "gateway", 2) == 0
    assert find_next_item_index(items, "gateway", 0, direction=-1) == 2


def test_component_counts_and_cycle_component() -> None:
    items = [
        {"key": "SAT-1", "component": "helm-chart"},
        {"key": "SAT-2", "component": "terraform"},
        {"key": "SAT-3", "component": "helm-chart"},
    ]

    assert component_counts(items) == [("helm-chart", 2), ("terraform", 1)]
    assert cycle_component(items, None, 1) == "helm-chart"
    assert cycle_component(items, "helm-chart", 1) == "terraform"
    assert cycle_component(items, "terraform", 1) is None


def test_cycle_fix_version_includes_none() -> None:
    items = [
        {"key": "SAT-1", "fixVersion": "helm-chart-sa 3.4.4"},
        {"key": "SAT-2"},
    ]

    assert cycle_fix_version(items, None, 1) == "(none)"
    assert cycle_fix_version(items, "(none)", 1) == "helm-chart-sa 3.4.4"


def test_swimlane_label_uses_virtual_none() -> None:
    item = {"key": "SAT-1", "epic": "", "fixVersion": "", "component": "_unassigned"}

    assert swimlane_label(item, "epic") == "(none)"
    assert swimlane_label(item, "version") == "(none)"
    assert swimlane_label(item, "component") == "(none)"
    assert swimlane_label(item, "none") is None


def test_cycle_swimlane() -> None:
    assert cycle_swimlane("none") == "epic"
    assert cycle_swimlane("epic") == "version"
    assert cycle_swimlane("version") == "component"
    assert cycle_swimlane("component") == "none"


def test_sort_items_for_swimlane_groups_virtual_none_first() -> None:
    items = [
        {"key": "SAT-12", "epic": "SAT-10", "epicSummary": "Parent"},
        {"key": "SAT-2", "epic": "", "epicSummary": ""},
        {"key": "SAT-11", "epic": "SAT-10", "epicSummary": "Parent"},
    ]

    ordered = sort_items_for_swimlane(items, "epic")

    assert [item["key"] for item in ordered] == ["SAT-2", "SAT-11", "SAT-12"]


def test_swimlane_header_names_mode() -> None:
    assert swimlane_header("SAT-10 Parent", "epic") == "== Epic: SAT-10 Parent =="
    assert swimlane_header("(none)", "version") == "== Version: (none) =="


def test_index_rows_hide_grouped_swimlane_values() -> None:
    item = {
        "key": "SAT-741",
        "status": "In Progress",
        "priority": "High",
        "assignee": "Marlon Garcia",
        "component": "helm-chart",
        "epic": "SAT-740",
        "fixVersion": "kube-stardog-stack 1.2.0",
        "summary": "A title",
    }

    epic_rows = index_row_lines(item, width=110, wrap=False, swimlane="epic")
    version_rows = index_row_lines(item, width=110, wrap=False, swimlane="version")
    component_rows = index_row_lines(item, width=110, wrap=False, swimlane="component")

    assert "-> SAT-740" not in epic_rows[1]
    assert "kube-stardog-stack" not in version_rows[1]
    assert "helm-chart" not in component_rows[0]


def test_index_rows_use_stable_columns_and_truncate_by_default() -> None:
    item = {
        "key": "SAT-741",
        "status": "In Progress",
        "priority": "High",
        "assignee": "Marlon Garcia",
        "component": "helm-chart",
        "epic": "SAT-740",
        "fixVersion": "kube-stardog-stack 1.2.0",
        "summary": "A title that is too long for a narrow terminal",
    }

    header = index_header(110)
    row = index_row_lines(item, width=110, wrap=False)

    assert header.startswith("ID         State")
    assert "Component" in header.splitlines()[0]
    assert "Parent" in header.splitlines()[1]
    assert "Priority" in header.splitlines()[1]
    assert "Assignee" in header.splitlines()[1]
    assert "Version" in header.splitlines()[1]
    assert len(row) == 2
    assert row[0].startswith("SAT-741    In Progress   helm-chart")
    assert "A title" in row[0]
    assert row[1].startswith("-> SAT-740")
    assert "High" in row[1]
    assert "Marlon Garcia" in row[1]
    assert "kube-stardog-stack" in row[1]
    narrow = index_row_lines(item, width=58, wrap=False)
    assert "Marlon Garcia" in narrow[1]


def test_index_header_hides_grouped_swimlane_column_label() -> None:
    version_header = index_header(110, swimlane="version")
    epic_header = index_header(110, swimlane="epic")
    component_header = index_header(110, swimlane="component")

    assert "Version" not in version_header.splitlines()[1]
    assert "Parent" not in epic_header.splitlines()[1]
    assert "Component" not in component_header.splitlines()[0]
    assert "Summary" in component_header.splitlines()[0]


def test_index_title_prioritizes_help_and_fits_width() -> None:
    title = index_title(51, ["component=helm-chart", "active"], 80)

    assert len(title) <= 79
    assert "h help" in title
    assert "P push all" in title
    assert "q quit" not in title


def test_index_title_truncates_filters_before_hiding_help_marker() -> None:
    title = index_title(51, ["component=helm-chart", "fixVersion=kube-stardog-stack 1.2.0", "active"], 46)

    assert len(title) <= 45
    assert "h" in title


def test_index_rows_mark_modified_items() -> None:
    item = {
        "key": "SAT-741",
        "status": "In Progress",
        "component": "helm-chart",
        "summary": "A title",
    }

    row = index_row_lines(item, width=80, wrap=False, modified_keys={"SAT-741"})

    assert row[0].startswith("SAT-741*")


def test_index_item_parent_key_reads_cached_epic() -> None:
    assert index_item_parent_key({"key": "SAT-741", "epic": "SAT-740"}) == "SAT-740"
    assert index_item_parent_key({"key": "SAT-740"}) == ""


def test_modified_issue_keys_reads_local_shadows(tmp_path: Path) -> None:
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", {"key": "SAT-741", "fields": {}})
    set_field(tmp_path, "SAT-741", "summary", "Changed")

    assert modified_issue_keys(tmp_path) == {"SAT-741"}


def test_push_all_report_lists_shadow_changes(tmp_path: Path) -> None:
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", {"key": "SAT-741", "fields": {}})
    set_field(tmp_path, "SAT-741", "summary", "Changed")
    add_comment(tmp_path, "SAT-741", "Local comment")

    report = "\n".join(push_all_report_lines(tmp_path, ["SAT-741"]))

    assert "Push all local shadow changes: 1 item" in report
    assert "SAT-741" in report
    assert "summary" in report
    assert "Comments" in report
    assert "Press y to confirm" in report


def test_push_all_report_shows_short_field_before_after(tmp_path: Path) -> None:
    write_json(
        tmp_path / "components/helm-chart/SAT-741/issue.json",
        {
            "key": "SAT-741",
            "fields": {
                "assignee": {"displayName": "Serge Colle"},
                "description": "Original description",
                "fixVersions": [{"name": "helm-chart 3.4.2"}],
                "resolution": None,
                "status": {"name": "To Do"},
            },
        },
    )
    set_field(tmp_path, "SAT-741", "assignee", {"displayName": "Marlon Garcia"})
    set_field(tmp_path, "SAT-741", "description", "New description")
    set_field(tmp_path, "SAT-741", "fixVersions", [{"name": "helm-chart 3.4.4"}])
    set_field(tmp_path, "SAT-741", "status", "Close")
    add_comment(tmp_path, "SAT-741", "No longer strategic")
    set_status_change(tmp_path, "SAT-741", resolution="Won't Do")

    report = "\n".join(push_all_report_lines(tmp_path, ["SAT-741"]))

    assert "assignee: Serge Colle -> Marlon Garcia" in report
    assert "version: helm-chart 3.4.2 -> helm-chart 3.4.4" in report
    assert "status: To Do -> Close" in report
    assert "resolution: (none) -> Won't Do" in report
    assert "description" in report
    assert "Original description -> New description" not in report


def test_draw_push_all_progress_shows_recent_lines() -> None:
    window = FakeWindow()

    draw_push_all_progress(window, ["one", "two", "three", "four", "five"])

    output = "\n".join(window.lines)
    assert "Push All" in window.lines[0]
    assert "one" not in output
    assert "two" in output
    assert "three" in output
    assert "five" in output


def test_draw_push_all_result_shows_error_message() -> None:
    window = FakeWindow()

    draw_push_all_result(window, "Push All Failed", ["SAT-1: pushing (1/1)", "", "push all failed: boom"])

    output = "\n".join(window.lines)
    assert "Push All Failed" in window.lines[0]
    assert "push all failed: boom" in output


def test_draw_push_all_result_wraps_long_error_message() -> None:
    window = FakeWindow(height=12, width=42)

    draw_push_all_result(
        window,
        "Push All Completed With Errors",
        [
            "SAT-593: failed: SAT-593: Jira update failed for fields [description, parent, priority, summary] "
            "(description=\"Implement and document restore from an existing Stardog server backup stored in S3\", "
            "parent=SAT-371): missing version",
        ],
    )

    output = "\n".join(window.lines)
    assert "description, parent," in output
    assert "priority, summary]" in output
    assert "existing Stardog server" in output
    assert "backup stored in S3" in output
    assert "missing version" in output


def test_draw_push_all_progress_wraps_long_error_message() -> None:
    window = FakeWindow(height=8, width=42)

    draw_push_all_progress(
        window,
        [
            "SAT-593: failed: SAT-593: Jira update failed for fields [description, parent, priority, summary] "
            "(description=\"Implement and document restore from an existing Stardog server backup stored in S3\")",
        ],
    )

    output = "\n".join(window.lines)
    assert "description, parent," in output
    assert "priority, summary]" in output
    assert "existing Stardog server" in output
    assert "backup stored in S3" in output


def test_draw_index_shows_message_on_empty_list() -> None:
    window = FakeWindow()

    draw_index(
        window,
        [],
        0,
        0,
        6,
        80,
        component="helm-chart",
        pattern=None,
        fix_version=None,
        active=True,
        wrap=False,
        message="push all failed: boom",
    )

    assert "push all failed: boom" in "\n".join(window.lines)


def test_draw_index_shows_swimlane_headers() -> None:
    window = FakeWindow(height=12, width=100)

    draw_index(
        window,
        [
            {
                "key": "SAT-2",
                "status": "To Do",
                "component": "helm-chart",
                "summary": "Without parent",
            },
            {
                "key": "SAT-11",
                "status": "To Do",
                "component": "helm-chart",
                "summary": "With parent",
                "epic": "SAT-10",
                "epicSummary": "Parent",
            },
        ],
        0,
        0,
        12,
        100,
        component=None,
        pattern=None,
        fix_version=None,
        active=True,
        wrap=False,
        swimlane="epic",
    )

    output = "\n".join(window.lines)
    assert "swimlane=epic" in output
    assert "== Epic: (none) ==" in output
    assert "== Epic: SAT-10 Parent ==" in output


def test_textbox_geometry_keeps_rectangle_inside_screen() -> None:
    _, _, box_height, box_width, rectangle_y2, rectangle_x2 = textbox_geometry(24, 80)

    assert rectangle_y2 == 22
    assert rectangle_x2 == 78
    assert box_height == 20
    assert box_width == 76


def test_index_rows_can_wrap_long_summaries() -> None:
    item = {
        "key": "SAT-741",
        "status": "In Progress",
        "priority": "High",
        "assignee": "Marlon Garcia",
        "component": "helm-chart",
        "epic": "SAT-740",
        "fixVersion": "kube-stardog-stack 1.2.0",
        "summary": "A title that is too long for a narrow terminal",
    }

    rows = index_row_lines(item, width=58, wrap=True)

    assert len(rows) > 1
    assert rows[0].startswith("SAT-741    In Progress   helm-chart")
    assert rows[-1].startswith("-> SAT-740")
    assert "High" in rows[-1]
    assert "Marlon Garcia" in rows[-1]


def test_load_manifest_items_sorts_existing_manifest_by_issue_number(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {
            "workItems": [
                {"key": "SAT-595"},
                {"key": "SAT-612"},
                {"key": "SAT-62"},
            ]
        },
    )

    assert [item["key"] for item in load_manifest_items(tmp_path)] == ["SAT-62", "SAT-595", "SAT-612"]


def test_load_manifest_items_enriches_missing_fix_version_from_issue_json(tmp_path: Path) -> None:
    write_json(tmp_path / "manifest.json", {"workItems": [{"key": "SAT-1"}]})
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {"key": "SAT-1", "fields": {"fixVersions": [{"name": "helm-chart-sa 3.4.4"}]}},
    )

    assert load_manifest_items(tmp_path)[0]["fixVersion"] == "helm-chart-sa 3.4.4"


def test_refresh_index_item_applies_shadow_fix_version(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {"workItems": [{"key": "SAT-1", "fixVersion": "old-version"}]},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {
            "key": "SAT-1",
            "fields": {
                "assignee": {"displayName": "Serge Colle"},
                "fixVersions": [{"name": "old-version"}],
            },
        },
    )
    items = load_manifest_items(tmp_path)

    set_field(tmp_path, "SAT-1", "fixVersions", [{"name": "new-version"}])
    refresh_index_item(tmp_path, items, "SAT-1")

    assert items[0]["fixVersion"] == "new-version"
    assert items[0]["assignee"] == "Serge Colle"


def test_refresh_stale_index_items_updates_changed_fix_version(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {"workItems": [{"key": "SAT-1", "fixVersion": "old-version"}]},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-1/issue.json",
        {"key": "SAT-1", "fields": {"fixVersions": [{"name": "old-version"}]}},
    )
    items = load_manifest_items(tmp_path)
    stale_keys = {"SAT-1"}

    set_field(tmp_path, "SAT-1", "fixVersions", [{"name": "new-version"}])
    refresh_stale_index_items(tmp_path, items, stale_keys)

    assert items[0]["fixVersion"] == "new-version"
    assert stale_keys == set()


def test_load_manifest_items_applies_shadow_summary_to_list(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {"workItems": [{"key": "SAT-19", "summary": "Link with Azure Key Vault"}]},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-19/issue.json",
        {"key": "SAT-19", "fields": {"summary": "Link with Azure Key Vault"}},
    )
    set_field(tmp_path, "SAT-19", "summary", "Support CSI-injected secrets from Azure Key Vault")

    item = load_manifest_items(tmp_path)[0]

    assert item["summary"] == "Support CSI-injected secrets from Azure Key Vault"


def test_load_manifest_items_applies_shadow_component_override_to_list(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {"workItems": [{"key": "SAT-19", "component": "helm-chart"}]},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-19/issue.json",
        {
            "key": "SAT-19",
            "fields": {
                "summary": "Support CSI-injected secrets from Azure Key Vault",
                "customfield_10071": {"value": "helm-chart"},
            },
        },
    )
    set_field(tmp_path, "SAT-19", "customfield_10071", {"value": "security"})

    item = load_manifest_items(tmp_path, component_field="customfield_10071")[0]

    assert item["component"] == "security"


def test_refresh_index_item_applies_shadow_component_override(tmp_path: Path) -> None:
    write_json(
        tmp_path / "manifest.json",
        {"workItems": [{"key": "SAT-19", "component": "helm-chart"}]},
    )
    write_json(
        tmp_path / "components/helm-chart/SAT-19/issue.json",
        {
            "key": "SAT-19",
            "fields": {
                "summary": "Support CSI-injected secrets from Azure Key Vault",
                "customfield_10071": {"value": "helm-chart"},
            },
        },
    )
    items = load_manifest_items(tmp_path, component_field="customfield_10071")

    set_field(tmp_path, "SAT-19", "customfield_10071", {"value": "security"})
    refresh_index_item(tmp_path, items, "SAT-19", component_field="customfield_10071")

    assert items[0]["component"] == "security"


def test_help_lines_document_interactive_actions() -> None:
    help_text = "\n".join(HELP_LINES)

    assert "h" in help_text
    assert "w" in help_text
    assert "--components" in help_text
    assert "--component" in help_text
    assert "--all" in help_text
    assert "Active hides statuses" in help_text
    assert "P" in help_text
    assert "V" in help_text
    assert "x" in help_text
    assert "R" in help_text


def test_cli_view_help_mentions_filters(capsys) -> None:
    try:
        main(["view", "--help"])
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()
    assert "--component" in captured.out
    assert "--components" in captured.out
    assert "--all" in captured.out
