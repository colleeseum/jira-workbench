from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

from jira_workbench.server import WorkbenchHandler, render_index, render_issue
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


def test_render_issue_returns_ok_for_valid_key(tmp_path: Path) -> None:
    issue_dir = tmp_path / "components" / "compA" / "SAT-1"
    issue_dir.mkdir(parents=True)
    (issue_dir / "issue.json").write_text(json.dumps({"key": "SAT-1", "summary": "hi"}))

    status, html = render_issue(tmp_path, "SAT-1")

    assert status == HTTPStatus.OK
    assert "SAT-1" in html


def test_render_issue_rejects_path_traversal_key(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    (jira_dir / "components" / "compA" / "SAT-1").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "issue.json").write_text(json.dumps({"secret": "leaked"}))

    status, html = render_issue(jira_dir, "../../outside")

    assert status == HTTPStatus.NOT_FOUND
    assert "leaked" not in html


def _start_server(jira_dir: Path) -> tuple[ThreadingHTTPServer, int]:
    handler = type("TestWorkbenchHandler", (WorkbenchHandler,), {"jira_dir": jira_dir})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    return server, server.server_address[1]


def test_serve_rejects_path_traversal_over_real_socket(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    (jira_dir / "components" / "compA" / "SAT-1").mkdir(parents=True)
    (jira_dir / "components" / "compA" / "SAT-1" / "issue.json").write_text(json.dumps({"key": "SAT-1"}))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "issue.json").write_text(json.dumps({"secret": "leaked"}))

    server, port = _start_server(jira_dir)
    try:
        response = urllib.request.urlopen(f"http://127.0.0.1:{port}/issue/SAT-1")
        assert response.status == 200
        assert b"SAT-1" in response.read()

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/issue/../../outside")
            raise AssertionError("expected traversal request to be rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            assert b"leaked" not in exc.read()
    finally:
        server.shutdown()
        server.server_close()


def test_serve_api_manifest_missing_returns_clean_404(tmp_path: Path) -> None:
    jira_dir = tmp_path / "jira"
    jira_dir.mkdir()

    server, port = _start_server(jira_dir)
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/manifest")
            raise AssertionError("expected missing manifest to return an error status")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            payload = json.loads(exc.read())
            assert "error" in payload
    finally:
        server.shutdown()
        server.server_close()
