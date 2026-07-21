# Jira Workbench

Jira Workbench syncs a Jira project into a local JSON directory and serves a small local web UI for browsing and searching the synced work items.

The first implementation is a Python refactor of the original `jira-sync` shell script. It still uses Atlassian CLI (`acli`) for Jira access, but the local data layout and manifest generation are now testable Python code.

## Features

- Incremental project sync using `acli jira workitem`
- Local component-oriented storage under `jira/components/<component>/<key>/`
- Per-work-item `issue.json`, `comments.json`, `attachments.json`, and `sync.json`
- Local shadow edits for fields and comments before pushing to Jira
- Generated `manifest.json` for fast browsing and search
- Local read-only web UI via `jira-wb serve`
- No third-party runtime Python dependencies

## Requirements

- Python 3.11 or newer
- `acli` authenticated against your Jira site

## Installation

```bash
python -m pip install --user -e .
```

Confirm the command is available:

```bash
jira-wb --help
```

## Usage

Create `~/.jira-wb.conf`:

```toml
project = "SAT"
component_field = "customfield_10071"
jira_dir = "jira"
acli = "acli"

[serve]
host = "127.0.0.1"
port = 8765
```

Sync the configured project:

```bash
jira-wb sync
```

Or pass the required sync values explicitly:

```bash
jira-wb sync --project SAT --component-field customfield_10071 --jira-dir jira --acli acli
```

Serve the local browser:

```bash
jira-wb serve
```

Or pass the server values explicitly:

```bash
jira-wb serve --jira-dir jira --host 127.0.0.1 --port 8765
```

Then open:

```text
http://127.0.0.1:8765
```

View synced work items in the terminal:

```bash
jira-wb view
jira-wb view SAT-1
jira-wb view SAT-1 --original
jira-wb view SAT-1 --diff
```

Without a work item key, `jira-wb view` opens a curses browser inspired by mail-style index navigation. Use `j`/`k` or arrow keys to move, Enter to open an item, `s` for the local shadow view, `o` for the original synced Jira issue, `d` for the shadow diff, and `q` to go back or quit.

The generated layout is:

```text
jira/
├── project.json
├── manifest.json
└── components/
    └── <component>/
        └── <issue-key>/
            ├── issue.json
            ├── comments.json
            ├── attachments.json
            ├── sync.json
            └── shadow.json
```

## Local shadow workflow

Shadow changes are local-only until pushed. They are stored beside each synced issue in `shadow.json`; `issue.json` remains the last synced Jira snapshot. After a successful push, Jira Workbench removes `shadow.json`.

Set a local field:

```bash
jira-wb shadow set SAT-1 description "Local draft description"
jira-wb shadow set SAT-1 fix_version "2026.07"
```

Add a local comment:

```bash
jira-wb shadow comment SAT-1 "Local draft comment"
```

Review and mark changes ready:

```bash
jira-wb shadow status
jira-wb shadow diff SAT-1
jira-wb shadow commit SAT-1
```

Push committed local changes:

```bash
jira-wb shadow push SAT-1
```

Before pushing a work item, Jira Workbench fetches the remote `updated` value and compares it to the value captured when the first local shadow edit was created. If the work item changed remotely, that item is skipped and not pushed.

Current push support is intentionally narrow because it uses verified `acli workitem edit` flags:

- `description`
- `summary`
- `labels`
- `assignee`
- `type`
- comments

Other fields, such as `fix_version`, can be stored locally and diffed, but push refuses them until the field-edit adapter is added.

## Development

```bash
python -m pip install -e .
python -m pip install pytest ruff build
python -m compileall -q src
ruff check .
pytest
```

## Release

The release workflow is tag-driven. Update `pyproject.toml`, commit, then push an annotated tag that matches the package version:

```bash
git tag -a v0.1.0 -m "Release 0.1.0"
git push origin v0.1.0
```

The workflow builds the wheel and source distribution, runs tests, smoke-tests the generated wheel, and publishes a GitHub Release.
