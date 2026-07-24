from __future__ import annotations

from pathlib import Path

from jira_workbench.server import render_index
from jira_workbench.sync import SyncConfig, sync_project
from test_sync import FakeJiraClient


def test_render_index_filters_manifest(tmp_path: Path) -> None:
    sync_project(
        SyncConfig(project="SAT", component_field="customfield_10071", jira_dir=tmp_path),
        FakeJiraClient(),
        progress=None,
    )

    html = render_index(tmp_path, "SAT-1")

    assert "SAT-1" in html
    assert "SAT-2" not in html
