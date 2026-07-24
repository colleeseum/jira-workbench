# Jira Workbench

Jira Workbench syncs a Jira project into a local JSON directory and serves a small local web UI for browsing and searching the synced work items.

Jira Workbench talks to Jira exclusively through the REST API (via `atlassian-python-api`) — no external CLI dependency.

## Features

- Incremental project sync using the Jira Cloud REST API
- Local component-oriented storage under `jira/components/<component>/<key>/`
- Per-work-item `issue.json`, `comments.json`, `attachments.json`, and `sync.json`
- Local shadow edits for fields and comments before pushing to Jira
- Generated `manifest.json` for fast browsing and search
- Local read-only web UI via `jira-wb serve`
- Jira project metadata admin commands through `atlassian-python-api`

## Requirements

- Python 3.11 or newer
- Jira API token credentials (a Jira Cloud API token plus your account email and site URL)

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
jira_dir = "jira"
jira_url = "https://example.atlassian.net"
jira_email = "you@example.com"
jira_api_token = "..."

# Optional. Defaults to Jira's native components field.
component_field = "customfield_10071"

[view]
component = "helm-chart"
filter = "prometheus"

[versions]
filter = "helm-chart-sa 3\\.[45]"

[serve]
host = "127.0.0.1"
port = 8765
```

Sync the configured project:

```bash
jira-wb sync
```

Or pass the sync values explicitly:

```bash
jira-wb sync --project SAT --component-field customfield_10071 --jira-dir jira \
  --jira-url https://example.atlassian.net --jira-email you@example.com --jira-api-token "..."
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

Refresh and inspect cached Jira project metadata:

```bash
jira-wb meta
jira-wb meta doctor
jira-wb meta refresh
jira-wb meta refresh --components
jira-wb meta versions
jira-wb meta versions --cached
jira-wb meta components
jira-wb meta components --cached
jira-wb meta version-add "helm-chart-sa 3.5.0"
jira-wb meta version-rename "helm-chart-sa 3.5.0" "helm-chart-sa 3.5.1"
jira-wb meta version-release "helm-chart-sa 3.5.1" --release-date 2026-07-21
jira-wb meta version-archive "helm-chart-sa 3.5.1"
jira-wb meta version-delete "helm-chart-sa 3.5.1"
jira-wb meta version-delete "old-version" --move-fix-to "new-version"
jira-wb meta component-add "helm-chart"
```

Project fix versions are cached under `jira/meta/versions.json`. Project components are cached under `jira/meta/components.json`. `jira-wb sync` refreshes the version cache. `jira-wb meta` commands use the Jira API credentials; use `--cached` for offline inspection.

`jira-wb meta version-add` and `jira-wb meta component-add` write directly to Jira through the Python API and then refresh the corresponding local cache. These project metadata operations do not use the issue shadow workflow.

Without a subcommand, `jira-wb meta` opens a curses metadata browser where Enter lists versions or components and `a` adds the selected metadata type.

The `[versions].filter` setting applies to the curses version list. It uses Python regular expression syntax, case-insensitive. It is not a full PCRE engine.

View synced work items in the terminal:

```bash
jira-wb view
jira-wb view --components
jira-wb view --component helm-chart
jira-wb view --filter prometheus
jira-wb view --all
jira-wb view SAT-1
jira-wb view SAT-1 --original
jira-wb view SAT-1 --diff
```

Without a work item key, `jira-wb view` opens a curses browser inspired by mail-style index navigation. Active-only filtering is enabled by default; pass `--all` to include closed, done, and resolved items. Configure `[view].component` and `[view].filter` to choose the default interactive subset; pass `--component` or `--filter` to override them for one run. Use `h` for full help, `j`/`k` or arrow keys to move, Enter to open an item, `/` to search rows vi-style such as `SAT-612`, `n`/`N` to repeat search, `g` to go to a matching row, `v` to type and view a work item key, `w` to toggle summary wrapping, `a` to toggle active-only filtering, `[` and `]` to cycle component filters, `c` to type a component filter, `C` to clear the component filter, `f` to type a text filter, `\` to clear the text filter, `s` for the local shadow view, `o` for the original synced Jira issue, `d` for the shadow diff, `r` to revert the current item shadow, `p` to push the current item shadow to Jira, and `q` to go back or quit.

Work item detail shows local hierarchy when available. Child items show their parent epic above the title; epics show locally synced children below the epic line.

The generated layout is:

```text
jira/
├── project.json
├── meta/
│   └── versions.json
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

Current push support is intentionally narrow to the Jira REST API fields Jira Workbench has verified update behavior for:

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
