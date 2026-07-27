from __future__ import annotations

from pathlib import Path

from jira_workbench.cli import main
from jira_workbench.shadow import add_comment, delete_comment, edit_comment, load_shadow, set_field
from jira_workbench.sync import SyncConfig, sync_project, write_json
from jira_workbench.view import (
    comment_body_text,
    comments_text,
    component_counts,
    cycle_component,
    cycle_fix_version,
    cycle_swimlane,
    detail_field_rows,
    editable_field_choices,
    editable_detail_fields,
    effective_comments_text,
    encode_edit_value,
    filter_items,
    filter_items_for_swimlane,
    find_item_index,
    find_next_item_index,
    format_issue,
    format_work_item,
    full_diff_texts,
    item_index_by_key,
    index_item_parent_key,
    load_manifest_items,
    issue_parent_key,
    modified_issue_keys,
    parent_options,
    refresh_index_item,
    refresh_stale_index_items,
    selectable_field_options,
    side_by_side_diff_lines,
    sort_items_for_swimlane,
    swimlane_label,
    text_from_adf,
    version_options,
    wrap_preview_lines,
)
from test_sync import FakeJiraClient


def synced_jira_dir(tmp_path: Path) -> Path:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
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


def test_wrap_preview_lines_keeps_long_urls_intact() -> None:
    url = "https://knowledge.digicert.com/solution/configure-cert-manager-and-digicert-acme-service-with-kubernetes"
    lines = wrap_preview_lines(f"See {url} for details.", 100)

    assert url in lines


def test_side_by_side_diff_lines_aligns_equal_changed_added_removed() -> None:
    before = "one\ntwo\nthree\nfour"
    after = "one\nTWO\nthree\nfive"

    rows = side_by_side_diff_lines(before, after)

    assert ("equal", "one", "one") in rows
    assert ("equal", "three", "three") in rows
    assert ("changed", "two", "TWO") in rows
    assert ("changed", "four", "five") in rows


def test_side_by_side_diff_lines_pads_pure_inserts_and_deletes() -> None:
    before = "keep\nremoved"
    after = "keep\nadded\nadded again"

    rows = side_by_side_diff_lines(before, after)

    assert ("equal", "keep", "keep") in rows
    # "removed" and "added"/"added again" don't align 1:1 (2 after lines vs
    # 1 before line) so difflib treats this block as replace+insert -- the
    # important invariant is every removed-only line has a blank right side
    # and every added-only line has a blank left side.
    tags = {tag for tag, _, _ in rows}
    assert "changed" in tags or "removed" in tags
    for tag, left, right in rows:
        if tag == "removed":
            assert right == ""
        if tag == "added":
            assert left == ""


def test_effective_comments_text_reflects_edits_and_deletes(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    write_json(
        jira_dir / "components/api-team/SAT-1/comments.json",
        {
            "comments": [
                {"id": "10001", "created": "2026-01-01T00:00:00.000+0000", "body": "original one"},
                {"id": "10002", "created": "2026-01-02T00:00:00.000+0000", "body": "original two"},
            ]
        },
    )

    edit_comment(jira_dir, "SAT-1", "10001", "edited one")
    delete_comment(jira_dir, "SAT-1", "10002")
    add_comment(jira_dir, "SAT-1", "brand new comment")

    shadow = load_shadow(jira_dir, "SAT-1")
    before = comments_text(jira_dir, "SAT-1", None)
    after = effective_comments_text(jira_dir, "SAT-1", shadow)

    assert "original one" in before
    assert "original two" in before

    assert "edited one" in after
    assert "original one" not in after  # superseded by the edit
    assert "original two" not in after  # queued for deletion
    assert "brand new comment" in after


def test_full_diff_texts_shows_field_description_and_comment_changes(tmp_path: Path) -> None:
    jira_dir = synced_jira_dir(tmp_path)
    write_json(
        jira_dir / "components/api-team/SAT-1/comments.json",
        {"comments": [{"id": "10001", "created": "2026-01-01T00:00:00.000+0000", "body": "original comment"}]},
    )
    set_field(jira_dir, "SAT-1", "description", "New description")
    edit_comment(jira_dir, "SAT-1", "10001", "edited comment")

    before, after = full_diff_texts(jira_dir, "SAT-1", component_field="customfield_10071")

    assert "original comment" in before
    assert "edited comment" not in before

    assert "New description" in after
    assert "edited comment" in after
    assert "original comment" not in after


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

    monkeypatch.setattr("jira_workbench.cli.open_interactive_view", fake_interactive_view)

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

    monkeypatch.setattr("jira_workbench.cli.open_interactive_view", fake_interactive_view)

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


def test_text_from_adf_simplifies_smart_link_wiki_markup() -> None:
    url = "https://knowledge.digicert.com/solution/configure-cert-manager"
    assert text_from_adf(f"See [{url}|{url}|smart-link] for details.") == f"See {url} for details."
    assert text_from_adf(f"See [{url}|smart-link] for details.") == f"See {url} for details."
    assert (
        text_from_adf(f"See [the docs|{url}|smart-link] for details.")
        == f"See the docs ({url}) for details."
    )


def test_comment_body_text_simplifies_smart_link_wiki_markup() -> None:
    url = "https://knowledge.digicert.com/solution/configure-cert-manager"
    comment = {"body": f"See [{url}|{url}|smart-link] for details."}
    assert comment_body_text(comment) == f"See {url} for details."


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


def test_issue_parent_key_reads_parent_field() -> None:
    issue = {"key": "SAT-741", "fields": {"parent": {"key": "SAT-740"}}}

    assert issue_parent_key(issue) == "SAT-740"
    assert issue_parent_key({"key": "SAT-740", "fields": {}}) == ""


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

    filtered = filter_items(items, field_filters={"component": "helm-chart"})

    assert [item["key"] for item in filtered] == ["SAT-1"]


def test_filter_items_can_include_closed_items() -> None:
    items = [
        {"key": "SAT-1", "component": "helm-chart", "status": "In Progress"},
        {"key": "SAT-2", "component": "helm-chart", "status": "Done"},
    ]

    filtered = filter_items(items, field_filters={"component": "helm-chart"}, active=False)

    assert [item["key"] for item in filtered] == ["SAT-1", "SAT-2"]


def test_filter_items_matches_text_pattern() -> None:
    items = [
        {"key": "SAT-1", "summary": "Prometheus support", "component": "helm-chart", "status": "Open"},
        {"key": "SAT-2", "summary": "Terraform support", "component": "terraform", "status": "Open"},
        {"key": "SAT-3", "summary": "Gateway docs", "component": "helm-chart", "status": "Done"},
    ]

    filtered = filter_items(items, field_filters={"component": "helm-chart"}, pattern="prom")

    assert [item["key"] for item in filtered] == ["SAT-1"]


def test_filter_items_matches_fix_version_including_none() -> None:
    items = [
        {"key": "SAT-1", "fixVersion": "helm-chart-sa 3.4.4", "status": "Open"},
        {"key": "SAT-2", "fixVersion": "", "status": "Open"},
        {"key": "SAT-3", "status": "Open"},
    ]

    assert [item["key"] for item in filter_items(items, field_filters={"fixVersion": "helm-chart-sa 3.4.4"})] == [
        "SAT-1"
    ]
    assert [item["key"] for item in filter_items(items, field_filters={"fixVersion": "(none)"})] == [
        "SAT-2",
        "SAT-3",
    ]


def test_matches_field_and_field_value_work_generically() -> None:
    from jira_workbench.view import field_value, matches_field

    item_with_assignee = {"assignee": "Jane Doe"}
    item_unassigned = {"assignee": ""}

    assert field_value(item_with_assignee, "assignee") == "Jane Doe"
    assert matches_field(item_with_assignee, "assignee", "jane doe") is True
    assert matches_field(item_with_assignee, "assignee", "Someone Else") is False
    assert matches_field(item_with_assignee, "assignee", None) is True
    assert matches_field(item_unassigned, "assignee", "(none)") is True
    assert matches_field(item_with_assignee, "assignee", "(none)") is False


def test_distinct_field_values_counts_and_buckets_missing() -> None:
    from jira_workbench.view import distinct_field_values

    items = [
        {"assignee": "Jane Doe"},
        {"assignee": "Jane Doe"},
        {"assignee": ""},
        {"assignee": "Bob Roe"},
    ]

    assert distinct_field_values(items, "assignee") == [
        ("(none)", 1),
        ("Bob Roe", 1),
        ("Jane Doe", 2),
    ]
    assert distinct_field_values(items, "assignee", empty_bucket="_unassigned") == [
        ("_unassigned", 1),
        ("Bob Roe", 1),
        ("Jane Doe", 2),
    ]


def test_filter_items_can_show_only_modified_items() -> None:
    items = [
        {"key": "SAT-1", "summary": "Modified", "component": "helm-chart", "status": "Open"},
        {"key": "SAT-2", "summary": "Plain", "component": "helm-chart", "status": "Open"},
    ]

    filtered = filter_items(items, modified_keys={"SAT-1"}, modified_only=True)

    assert [item["key"] for item in filtered] == ["SAT-1"]


def test_filter_items_modified_only_combines_with_component_filter() -> None:
    # Modified is just another filter dimension: picking a specific
    # component while Modified is on narrows to that component's modified
    # items; an empty/"(any)" component filter (no entry in field_filters)
    # shows every modified item, since matches_field is a no-op when unset.
    items = [
        {"key": "SAT-1", "component": "helm-chart", "status": "Open"},
        {"key": "SAT-2", "component": "terraform", "status": "Open"},
        {"key": "SAT-3", "component": "helm-chart", "status": "Open"},
    ]
    modified_keys = {"SAT-1", "SAT-2"}

    narrowed = filter_items(
        items, field_filters={"component": "helm-chart"}, modified_keys=modified_keys, modified_only=True
    )
    assert [item["key"] for item in narrowed] == ["SAT-1"]

    unfiltered = filter_items(items, field_filters={}, modified_keys=modified_keys, modified_only=True)
    assert [item["key"] for item in unfiltered] == ["SAT-1", "SAT-2"]


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


def test_sort_items_for_swimlane_by_column_click_when_ungrouped() -> None:
    items = [
        {"key": "SAT-1", "priority": "Medium", "assignee": "Bob"},
        {"key": "SAT-2", "priority": "Highest", "assignee": "Alice"},
        {"key": "SAT-3", "priority": "Lowest", "assignee": "Carol"},
    ]

    by_priority = sort_items_for_swimlane(items, "none", sort_field="priority")
    assert [item["key"] for item in by_priority] == ["SAT-2", "SAT-1", "SAT-3"]

    by_priority_reversed = sort_items_for_swimlane(items, "none", sort_field="priority", reverse=True)
    assert [item["key"] for item in by_priority_reversed] == ["SAT-3", "SAT-1", "SAT-2"]

    by_assignee = sort_items_for_swimlane(items, "none", sort_field="assignee")
    assert [item["key"] for item in by_assignee] == ["SAT-2", "SAT-1", "SAT-3"]


def test_sort_items_for_swimlane_by_column_click_within_epic_lane() -> None:
    # Sorting by a column must not scatter the epic-swimlane grouping --
    # the epic itself still heads its lane, only its children reorder.
    items = [
        {"key": "SAT-100", "type": "Epic", "summary": "Epic"},
        {"key": "SAT-1", "type": "Task", "epic": "SAT-100", "priority": "Low"},
        {"key": "SAT-2", "type": "Task", "epic": "SAT-100", "priority": "Highest"},
    ]

    ordered = sort_items_for_swimlane(items, "epic", sort_field="priority")

    assert [item["key"] for item in ordered] == ["SAT-100", "SAT-2", "SAT-1"]


def test_epic_swimlane_keeps_epic_items_as_lane_headers() -> None:
    items = [
        {"key": "SAT-742", "type": "Epic", "summary": "FluxCD Starter Foundation"},
        {"key": "SAT-743", "type": "Improvement", "epic": "SAT-742"},
        {"key": "SAT-999", "type": "Improvement", "summary": "No parent"},
    ]

    filtered = filter_items_for_swimlane(items, "epic")
    ordered = sort_items_for_swimlane(filtered, "epic")

    assert [item["key"] for item in filtered] == ["SAT-742", "SAT-743", "SAT-999"]
    assert [item["key"] for item in ordered] == ["SAT-999", "SAT-742", "SAT-743"]
    assert swimlane_label(items[0], "epic") == "SAT-742 FluxCD Starter Foundation"


def test_non_epic_swimlane_keeps_epic_items() -> None:
    items = [
        {"key": "SAT-742", "type": "Epic", "component": "iac-fluxcd"},
        {"key": "SAT-743", "type": "Improvement", "component": "iac-fluxcd"},
    ]

    assert filter_items_for_swimlane(items, "component") == items
    assert filter_items_for_swimlane(items, "version") == items


def test_index_item_parent_key_reads_cached_epic() -> None:
    assert index_item_parent_key({"key": "SAT-741", "epic": "SAT-740"}) == "SAT-740"
    assert index_item_parent_key({"key": "SAT-740"}) == ""


def test_modified_issue_keys_reads_local_shadows(tmp_path: Path) -> None:
    write_json(tmp_path / "components/helm-chart/SAT-741/issue.json", {"key": "SAT-741", "fields": {}})
    set_field(tmp_path, "SAT-741", "summary", "Changed")

    assert modified_issue_keys(tmp_path) == {"SAT-741"}


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


def test_cli_view_help_mentions_filters(capsys) -> None:
    try:
        main(["view", "--help"])
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()
    assert "--component" in captured.out
    assert "--components" in captured.out
    assert "--all" in captured.out
