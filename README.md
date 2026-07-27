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

Create `~/.config/jira-wb/config.toml`:

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
preview_lines = 10

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
jira-wb meta native-component-add "helm-chart"
```

Project fix versions are cached under `jira/meta/versions.json`. Project components are cached under `jira/meta/components.json`. `jira-wb sync` refreshes the version cache. `jira-wb meta` commands use the Jira API credentials; use `--cached` for offline inspection.

`jira-wb meta version-add` and `jira-wb meta component-add` write directly to Jira through the Python API and then refresh the corresponding local cache. These project metadata operations do not use the issue shadow workflow.

Without a subcommand, `jira-wb meta` opens an interactive metadata browser (built with [Textual](https://textual.textualize.io/), with full mouse support): Enter opens the fix-versions browser or shows component metadata, `n` adds a version or component. This same screen is also reachable with `M` from inside `jira-wb view`, so you don't need to leave your browsing session to check or edit metadata.

The `[versions].filter` setting applies to the fix-versions browser's `/` filter. It uses Python regular expression syntax, case-insensitive. It is not a full PCRE engine.

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

Without a work item key, `jira-wb view` opens an interactive browser built with [Textual](https://textual.textualize.io/) — mail-style index navigation with full mouse support (click a row to open it, scroll wheel on any list or table). Active-only filtering is enabled by default; pass `--all` to include closed, done, and resolved items. Configure `[view].component` and `[view].filter` to choose the default interactive subset; pass `--component` or `--filter` to override them for one run. `[view].preview_lines` (default 10) controls how many lines of Description and Comments are each previewed in the detail table before opening the full viewer. Click a column header to sort by it (click again to reverse; an arrow marks the active sort). Use `h` for full help, `j`/`k` or arrow keys to move, Enter or click to open an item, `/` to search rows vi-style such as `SAT-612`, `n`/`N` to repeat search, `g` to go to a matching row, `v` to type and view a work item key, `a` to toggle active-only filtering, `m` to toggle modified-only filtering, `f` to open the consolidated Filters screen (component, fix version, assignee, and free-text — each a row you can edit, clear, or clear all at once), `S` to cycle swimlane grouping, `P` to push all local shadow changes (opens a review list first — Enter on an item shows a full side-by-side diff, `p` pushes, `q`/Esc cancels), `M` to open Jira metadata (fix versions, components) without leaving the session, `s`/`o`/`d` for the local shadow view / original synced issue / shadow diff on a work item, `r` to revert the current item shadow, `p` to push the current item shadow to Jira, and `q` to go back or quit.

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

## Scoped API tokens (recommended over classic tokens)

`jira_api_token` above accepts either a classic Atlassian API token (long-lived, full account access) or a newer **scoped API token** (fine-grained permissions, 1–365 day expiration). Scoped tokens are a drop-in replacement — same `jira_email` + `jira_api_token` Basic Auth, no code or config shape changes — with two differences:

1. Create one at https://id.atlassian.com/manage-profile/security/api-tokens → "Create API token with scopes", picking a Jira scope set such as `read:jira-work write:jira-work read:jira-user manage:jira-project`, and an expiration (max 365 days — you'll need to regenerate and update your config when it expires).
2. Point `jira_url` at the cloud-routed endpoint instead of your tenant domain: `https://api.atlassian.com/ex/jira/<cloud_id>`. Find your cloud ID with:

   ```bash
   curl -s https://your-site.atlassian.net/_edge/tenant_info
   ```

There's no OAuth app registration, no client secret, and no browser flow involved — it's the same static-credential model as classic tokens, just scoped and time-boxed.

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
python -m pip install -e ".[test,lint]" build
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
