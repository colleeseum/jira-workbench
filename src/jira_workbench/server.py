from __future__ import annotations

import html
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ISSUE_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def load_json(path: Path) -> object:
    return json.loads(path.read_text())


def match_query(item: dict[str, object], query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(str(item.get(field, "")) for field in ("key", "summary", "status", "type", "component"))
    return query.lower() in haystack.lower()


def render_index(jira_dir: Path, query: str = "") -> str:
    manifest_path = jira_dir / "manifest.json"
    if not manifest_path.exists():
        body = "<h1>Jira Workbench</h1><p>No manifest found. Run <code>jira-wb sync</code>.</p>"
        return page(body)

    manifest = load_json(manifest_path)
    items = manifest.get("workItems", []) if isinstance(manifest, dict) else []
    if not isinstance(items, list):
        items = []
    filtered = [item for item in items if isinstance(item, dict) and match_query(item, query)]

    rows = []
    for item in filtered:
        key = html.escape(str(item.get("key", "")))
        component = html.escape(str(item.get("component", "")))
        status = html.escape(str(item.get("status", "")))
        issue_type = html.escape(str(item.get("type", "")))
        updated = html.escape(str(item.get("updated", "")))
        summary = html.escape(str(item.get("summary", "")))
        rows.append(
            "<tr>"
            f"<td><a href=\"/issue/{key}\">{key}</a></td>"
            f"<td>{summary}</td>"
            f"<td>{component}</td>"
            f"<td>{status}</td>"
            f"<td>{issue_type}</td>"
            f"<td>{updated}</td>"
            "</tr>"
        )

    generated = html.escape(str(manifest.get("generatedAt", ""))) if isinstance(manifest, dict) else ""
    body = f"""
    <header>
      <h1>Jira Workbench</h1>
      <p>{len(filtered)} of {len(items)} work items. Generated {generated}.</p>
      <form method="get">
        <input name="q" value="{html.escape(query)}" placeholder="Search key, summary, status, type, component" autofocus>
        <button type="submit">Search</button>
      </form>
    </header>
    <table>
      <thead>
        <tr><th>Key</th><th>Summary</th><th>Component</th><th>Status</th><th>Type</th><th>Updated</th></tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """
    return page(body)


def render_issue(jira_dir: Path, key: str) -> tuple[int, str]:
    if not ISSUE_KEY_PATTERN.match(key):
        return HTTPStatus.NOT_FOUND, page(f"<h1>{html.escape(key)}</h1><p>Issue not found.</p>")
    matches = list((jira_dir / "components").glob(f"*/{key}/issue.json"))
    if not matches:
        return HTTPStatus.NOT_FOUND, page(f"<h1>{html.escape(key)}</h1><p>Issue not found.</p>")
    issue_path = matches[0]
    issue = load_json(issue_path)
    comments_path = issue_path.parent / "comments.json"
    attachments_path = issue_path.parent / "attachments.json"
    comments = load_json(comments_path) if comments_path.exists() else []
    attachments = load_json(attachments_path) if attachments_path.exists() else []
    body = f"""
    <p><a href="/">Back</a></p>
    <h1>{html.escape(key)}</h1>
    <h2>Issue</h2>
    <pre>{html.escape(json.dumps(issue, indent=2, sort_keys=True))}</pre>
    <h2>Comments</h2>
    <pre>{html.escape(json.dumps(comments, indent=2, sort_keys=True))}</pre>
    <h2>Attachments</h2>
    <pre>{html.escape(json.dumps(attachments, indent=2, sort_keys=True))}</pre>
    """
    return HTTPStatus.OK, page(body)


def page(body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jira Workbench</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; padding: 24px; line-height: 1.4; }}
    header {{ margin-bottom: 24px; }}
    input {{ width: min(720px, 100%); padding: 8px 10px; }}
    button {{ padding: 8px 12px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #9995; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: Canvas; }}
    pre {{ overflow: auto; padding: 12px; background: #8882; }}
    a {{ color: LinkText; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


class WorkbenchHandler(BaseHTTPRequestHandler):
    jira_dir: Path

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        status = HTTPStatus.OK
        if parsed.path == "/":
            query = parse_qs(parsed.query).get("q", [""])[0]
            content = render_index(self.jira_dir, query)
        elif parsed.path == "/api/manifest":
            try:
                manifest = load_json(self.jira_dir / "manifest.json")
            except FileNotFoundError:
                self.send_json({"error": "no manifest found; run jira-wb sync"}, status=HTTPStatus.NOT_FOUND)
                return
            self.send_json(manifest)
            return
        elif parsed.path.startswith("/issue/"):
            key = unquote(parsed.path.removeprefix("/issue/"))
            status, content = render_issue(self.jira_dir, key)
        else:
            status = HTTPStatus.NOT_FOUND
            content = page("<h1>Not found</h1>")

        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(jira_dir: Path, host: str, port: int) -> None:
    handler = type("ConfiguredWorkbenchHandler", (WorkbenchHandler,), {"jira_dir": jira_dir})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Serving Jira Workbench at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
