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
jira_url = "https://example.atlassian.net"
jira_email = "you@example.com"
jira_api_token = "..."

# Optional. Where synced issues, caches, and shadows live -- like a mail
# client's local profile, one shared store regardless of which directory you
# happen to run jira-wb from. Defaults to $XDG_DATA_HOME/jira-wb (usually
# ~/.local/share/jira-wb) if unset; only set this to override that default.
jira_dir = "jira"

# Optional. Defaults to Jira's native components field.
component_field = "customfield_10071"

# Optional. Only sync Done issues that have been in that status for this
# many months or less -- see "Limiting sync history" below. Unset means
# unbounded (sync every issue regardless of age), same as today.
sync_history_months = 24

[view]
# project, status, component, fix_version, and assignee each accept either a
# bare string (one value, shown here) or an array of strings (multiple
# values, matched as "any of") -- e.g. status = ["To Do", "In Progress"].
project = "SAT"
status = "To Do"
component = "helm-chart"
fix_version = "2026.07"
assignee = "you@example.com"
board = "SAT board"
board_scope = "active"
filter = "prometheus"
swimlane = "component"
active = true
preview_lines = 10
# Optional. When a Kanban board's "active" scope is selected, hide Done/Close
# items whose status last changed more than this many days ago. Unset means
# never hide them. This is a local, explicit setting -- not a replica of
# Jira's own (unqueryable) "days to show completed issues" board setting.
hide_done_after_days = 7
# Optional, default false. Use Nerd Font v3 glyphs for the index's type-icon
# column instead of plain Unicode shapes (◆●■▲). Only turn this on if your
# terminal font is actually a Nerd Font -- there's no way for this tool to
# detect that, so the wrong setting here just shows broken/tofu glyphs
# instead of the plain shapes. No effect on anything else in the app.
nerd_font = true
# Optional. The custom field Jira Cloud mirrors its "Development" panel data
# (branches/PRs) into -- already part of a normal sync (fields="*all"), so
# the index's Dev column reads it with no extra API calls. "customfield_10000"
# is the common default, but custom field ids can differ per Jira instance.
dev_status_field = "customfield_10000"

[versions]
# Applies to the Meta fix-versions browser's `/` filter only (see below) --
# not the fixVersion picker used when editing/creating an issue, which is
# always scoped to that issue's own project (see [versions.by_component]).
filter = "helm-chart-sa 3\\.[45]"

# Optional. A regex fix-version filter per component, scoped per project --
# applied only to the fixVersion *picker* (Detail's Fix Version row, New
# Issue's Version row), narrowing an already project-scoped list further.
# Keyed by whatever the issue's own component value is (case-insensitive).
# A component with no entry here just sees every version in its project,
# unfiltered. Scoped per project (not one flat table) since the same
# component name can mean, and be versioned, completely differently across
# two different configured projects.
[versions.by_component.SAT]
helm-chart = "helm-chart-sa"
terraform = "infra-\\d+"

[versions.by_component.PLAT]
helm-chart = "plat-helm"

[issue]
# Default --type for `jira-wb issue create` (both CLI and the New issue TUI
# screen's own default logic is separate -- see Creating issues below).
# No built-in fallback name; set this to whatever your project actually uses.
default_type = "Story"

[serve]
host = "127.0.0.1"
port = 8765
```

### Multiple projects

`project = "SAT"` above is shorthand for a single, default, read-write project.
To sync several projects from the same Jira Cloud site into one shared
`jira_dir`, replace it with a `[[projects]]` array instead:

```toml
[[projects]]
key = "SAT"
default = true

[[projects]]
key = "OTHERPROJ"
read_only = true
```

- Exactly one entry may set `default = true` -- this is the project the TUI
  opens against and the one `jira-wb issue create` targets when `--project`
  isn't passed.
- `read_only = true` blocks field/status edits, pushes, and new-issue creation
  for that project everywhere (CLI and TUI) -- **adding a comment still always
  works**, even on a read-only project, since comments are never blocked.
- All projects share the same `jira_url`/`jira_email`/`jira_api_token` --
  multi-project support assumes one Jira Cloud site with shared credentials,
  not several separate instances.
- `jira-wb sync` with no `--project` syncs every configured project in turn;
  `jira-wb sync --project X` still syncs just that one project, same as
  before.

### Limiting sync history

A project with several thousand issues will have plenty that were closed a
long time ago and aren't worth continuing to sync. `sync_history_months`
(top-level, applies to every project) and a per-project `history_months`
override bound how far back **Done** issues are pulled in:

```toml
sync_history_months = 24  # default: 2 years of closed-issue history

[[projects]]
key = "SAT"
default = true
# no override -- uses the 24-month default above

[[projects]]
key = "HUGEPROJ"
history_months = 6  # this one project only keeps the last 6 months of Done issues
```

Currently-open issues are **never** excluded by this, regardless of how old
they are -- only issues Jira already considers Done, and only once they've
sat in that status longer than the cutoff. `--history-months N` on the CLI
overrides both settings for that one sync run, across every project being
synced.

This only changes what a *future* sync fetches -- an issue that's already
been synced and later ages out of the window is left alone on disk, not
deleted. If a project's local storage grows too large over time, pruning
old synced issues is a separate concern this doesn't address yet.

One consequence of excluding old Done issues: a parent (Epic, Story, or
anything else `fields.parent` can point at) might be old and Done enough to
fall outside the cutoff while a child of it is still open, or was itself
touched more recently. Every sync backfills these regardless of the cutoff
-- as long as the parent lives in a project you're already syncing, its
child never ends up pointing at a parent your local copy doesn't have.

Sync the configured project:

```bash
jira-wb sync
```

Or pass the sync values explicitly:

```bash
jira-wb sync --project SAT --component-field customfield_10071 --jira-dir jira \
  --jira-url https://example.atlassian.net --jira-email you@example.com --jira-api-token "..."
```

A normal sync skips an issue whose own `updated` timestamp hasn't changed. That
misses drift caused by something *else* changing -- e.g. a fix version renamed in
Jira doesn't bump the timestamp of every issue that references it, so their locally
cached fix version name can go stale even though nothing about the issue itself
changed. Pass `--force` to re-fetch every issue regardless of its timestamp:

```bash
jira-wb sync --force
```

To refresh just the one issue you're looking at, without waiting on a full
project sync, press `R` on its Detail screen in the interactive view -- an
immediate, single-issue equivalent of `sync --force`. Any local shadow edit
on that issue is preserved.

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
jira-wb meta refresh --boards
jira-wb meta boards
jira-wb meta boards --cached
```

Project fix versions are cached under `jira/meta/versions.json`. Project components are cached under `jira/meta/components.json`. Kanban boards are cached under `jira/meta/boards.json` (see [Boards (Kanban)](#boards-kanban) below). `jira-wb sync` refreshes the version cache. `jira-wb meta` commands use the Jira API credentials; use `--cached` for offline inspection.

`jira-wb meta version-add` and `jira-wb meta component-add` write directly to Jira through the Python API and then refresh the corresponding local cache. These project metadata operations do not use the issue shadow workflow.

Without a subcommand, `jira-wb meta` opens an interactive metadata browser (built with [Textual](https://textual.textualize.io/), with full mouse support): Enter opens the fix-versions browser, shows component metadata, lists Kanban boards, or opens the labels browser; `n` adds a version or component. This same screen is also reachable with `M` from inside `jira-wb view`, so you don't need to leave your browsing session to check metadata.

The labels browser lists every label used across your locally synced issues (shadow-merged, so a pending local edit shows immediately) with its issue count. Jira has no API for labels as an independent entity, unlike fix versions/components -- a label is just free text on each issue's `labels` field. So `e` (rename) and `d` (delete) here find every locally synced issue with the selected label, edit each one's shadow, and push them -- the same thing Jira's own bulk-edit issue navigator does under the hood, just without a dedicated API for it. Both ask for confirmation (with the affected issue count) before touching anything, and require Jira API credentials to push. `n` (new) doesn't create anything -- Jira has no standalone label registry to add to, so it just explains that and points you at applying a label directly to an issue instead (Detail or the New issue screen's Labels field).

The `[versions].filter` setting applies to the fix-versions browser's `/` filter. It uses Python regular expression syntax, case-insensitive. It is not a full PCRE engine.

The fixVersion **picker** (Detail's Fix Version row, New Issue's Version row) is a separate thing from the browser above and always scoped to the issue's own project -- Jira refuses to assign a fixVersion from a different project on push, so the picker never offers one. `[versions.by_component]` (see the config example above) additionally narrows that already project-scoped list by a regex keyed on the issue's own component, for projects where fix versions are themselves organized per component.

By default the picker only offers versions that are both unreleased and unarchived -- assigning new work to an already-shipped version is unusual enough to want a deliberate second step rather than a default option. Archived versions are never offered at all, matching Jira's own UI (archiving a version removes it from every Fix Version/Affects Version picker for good). A "show released" checkbox inside the picker reveals released-but-unarchived versions too, without leaving it -- toggle it with a click, Tab to it and press Space/Enter, or press `Ctrl+R` as a shortcut from the filter box.

## Boards (Kanban)

A Jira board is not a field on an issue — it's a saved filter (JQL), plus, for Kanban boards, a separate active/backlog split tracked by Jira itself. Jira Workbench reproduces both locally:

- **Board membership**: each board's filter is fetched once (`jira-wb meta refresh --boards`) and compiled into a small local predicate, then evaluated against your *locally synced and shadow-edited* copy of each issue — so editing a component or label locally updates board membership immediately, without waiting for a push or another refresh, the same way every other local filter already works.
- **Active vs. backlog** (Kanban boards only): fetched from Jira's real backlog data at refresh time and cached alongside membership. This part genuinely can't be shadow-live — it's a classification Jira computes server-side, not a field — so it reflects the state as of your last `meta refresh --boards`, the same kind of staleness this tool already accepts for versions and components between refreshes.

Only a narrow, deliberately-scoped subset of JQL is supported: `field = value` / `field != value` clauses (a configured component field, `labels`, or `project`), combined with `AND`/`OR` and parentheses, with a trailing `ORDER BY` ignored. A board whose filter uses anything else (functions like `currentUser()`, date comparisons, `IN (...)`, other fields) is marked **unsupported** in `jira-wb meta boards` output rather than silently evaluated incorrectly.

A board only ever shows issues from its own project in Jira, even when its saved filter's JQL never says so explicitly (many board filters are just e.g. `component = helm-chart`, relying on the board's own project association) — so a filter that doesn't already reference `project` itself is implicitly scoped to whichever project the board was fetched from. A board whose filter genuinely spans several projects (an explicit `project = A OR project = B`) is left alone.

**Sprints and Scrum-based backlogs are a different Jira feature and are explicitly not supported.** A Scrum board's filter membership is still evaluated (same as Kanban), but its active/backlog split is driven by Sprint field assignment, which Jira Workbench does not track — `backlogKeys` is always `null` for Scrum-type boards. Use Jira directly for sprint planning.

In the interactive `view`, use `f` (Filters) to filter by board and, for Kanban boards, by active/backlog scope, or `S` (swimlane) to group by board — an item genuinely can belong to more than one board at once (board membership isn't exclusive), so its swimlane shows every matching board joined together rather than duplicating the row.

"Moving an item between boards" and editing a board's filter from Jira Workbench are not supported yet — board membership follows directly from the same component/label fields you already edit via the normal shadow workflow, and the ambiguity of which field to change for an OR-based filter needs its own design before that's added.

### Local boards (bespoke filters) and the active toggle

`jira-wb meta` → Boards (`M` then Enter on Boards) lists every Jira board across every synced project, plus any **local** boards you've defined yourself — one unified list, one `Type` column showing `jira`, `jira unsupported`, or `local`:

```
Name            Active  Type             Status                          Backlog
SAT board       yes     jira             ok                              12 issues
PLAT board      no      jira unsupported unsupported: unsupported...     n/a
My PLAT Filter  yes     local            -                               -
```

A **local** board is a named, saved combination of the same filters Filters (`f`) already has — project/status/component/fix version/assignee plus a text pattern, each multi-select — matched the same way any other local filter is (no JQL involved), so it works regardless of what the JQL compiler above can or can't parse. From the Boards screen, `n` (new) or `e` (edit) opens a single pane with the board's Name as its own row alongside every filter row — edit any of them with Enter, same as Filters — then `ctrl+s` saves (a name is required) or `q`/Esc cancels without saving anything. `d` deletes a local board. Jira boards are list-only here — `e`/`d` on one just tells you to change the underlying Jira board or the issue's own fields instead.

Every board — Jira or local — has an **Active** toggle (`a`), which is purely local and independent of whether Jira's own filter is supported: it only controls whether that board is offered as an option in the Filters screen's board picker (this Meta list always shows everything, active or not, so you can find and re-enable something later). Toggling active never affects matching for a board you've already selected, e.g. via a saved `[view].board` default.

A local board can also define its own **Active filter** row — a Jira board's active/backlog split always comes from Jira's own live-fetched Kanban data (see above), but a local board has no such data, so by default `board_scope` is a no-op for it. Setting an Active filter (Enter on that row opens the same kind of filter-row sub-editor, minus the Name row) changes that: whatever matches it counts as "active"; everything else already on the board counts as "backlog" — mirroring Jira's own strict two-way Kanban split, just defined locally instead of fetched. Clearing it (`d` on that row) goes back to the no-op default.

## Creating issues

Create a Jira issue and sync it locally from the command line:

```bash
jira-wb issue create --summary "Fix helm chart values" --type Story --component helm-chart
jira-wb issue create --summary "Fix helm chart values" --type Bug --priority High \
  --parent SAT-100 --description "Steps to reproduce..."
jira-wb issue create --summary "Dry run only" --type Story --dry-run
```

`--type` is required -- pass it explicitly, or set `[issue].default_type` in `config.toml` to skip typing it every time (`--type` still overrides the configured default for one run). There's no built-in fallback name, since which issue types actually exist is entirely up to your project. Which of `--component`, `--parent`, and `--priority` are accepted depends on the issue type's create screen in Jira -- passing one that type doesn't support fails with an error rather than being silently dropped. `--dry-run` prints the fields that would be sent instead of creating the issue.

A richer version of the same flow is available from the interactive `view`: press `c` from the index to open a New issue screen -- one screen, laid out like the work-item Detail view (a Field/Value table), covering the same fields Detail shows for an existing issue except Comments and the `O`-toggled Other fields: Summary, Description, Type, Status, Priority, Version, Assignee, Reporter, Parent, Component, Labels. Each editable in place with Enter (a picker or text prompt, same widgets used everywhere else in the app), except Status. Press `p` to create (asks to confirm first); `q`/Esc cancels.

- **Summary, Description, and Type** have no default and are required -- creating fails with a warning until all three are filled in.
- **Status** always reads "To Do" and can't be edited: Jira has no create-time status field at all (a new issue always starts at its workflow's initial status; reaching any other one is only possible as a separate transition after creation), so this form doesn't send it anywhere -- it's shown for information only.
- **Priority, Version (fix version), Assignee, Labels, and Component** default to the epic under the cursor's own values -- on an Epic row that's the epic itself; on any of its children, the same epic (creating a sibling) -- in any swimlane mode, not just grouped by epic. With no epic in context (including the "(none)" epic lane), these start blank.
- **Parent** defaults to that same epic's key.
- **Reporter** is independent of the epic and always defaults to whichever account your configured API credentials belong to.
- Component, Version, Priority, Labels, Assignee, Reporter, and Parent only appear once you pick a Type -- Jira lets different issue types have different create screens, so which of these apply depends on the selected Type's actual create screen in Jira, and updates live if you change Type again.
- **Labels** opens a checklist (toggle with Space) of every label already used across your locally synced issues, instead of free text -- since you sync the whole project, that's the full set as of your last sync, so there's no risk of a typo silently minting a new label. A separate input below the list is the only way to add a genuinely new one, so that's always a deliberate, visible action.

Every default is just a starting point -- change any field before creating. The new issue is synced locally and selected in the list as soon as it's created.

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

Without a work item key, `jira-wb view` opens an interactive browser built with [Textual](https://textual.textualize.io/) — mail-style index navigation with full mouse support (click a row to open it, scroll wheel on any list or table). Active-only filtering is enabled by default; pass `--all` to include closed, done, and resolved items. Configure `[view].component` and `[view].filter` to choose the default interactive subset; pass `--component` or `--filter` to override them for one run. `[view]` also accepts `project`, `status`, `fix_version`, `assignee`, `board`, `board_scope`, `swimlane`, and `active` (boolean) to set every other filter dimension's default. `project`, `status`, `component`, `fix_version`, and `assignee` each accept a bare string or an array of strings (see the config example above) — an array matches "any of" those values, the same as picking several in the Filters screen itself. `[view].nerd_font` (boolean, default `false`) switches the index's type-icon column from plain Unicode shapes to Nerd Font v3 glyphs -- purely cosmetic, opt-in only, since there's no reliable way for this tool to detect whether your terminal font actually has those glyphs. Press `s` from the index to save your current filters (all of the above, but not modified-only, which is a transient toggle) as the new `[view]` defaults, written back to `config.toml` in place -- comments and unrelated settings are preserved. `[view].preview_lines` (default 10) controls how many lines of Description and Comments are each previewed in the detail table before opening the full viewer. Click a column header to sort by it (click again to reverse; an arrow marks the active sort). Use `h` for full help, `j`/`k` or arrow keys to move, Enter or click to open an item, `/` to search rows vi-style such as `SAT-612`, `n`/`N` to repeat search, `g` to go to a matching row, `v` to type and view a work item key, `a` to toggle active-only filtering, `m` to toggle modified-only filtering, `f` to open the consolidated Filters screen (project, status, component, fix version, assignee, board, board scope, and free-text — each a row you can edit, clear, or clear all at once; every field except board/board scope is multi-select, via a checkbox picker: type to narrow, Enter picks the top match and clears the filter box so you can keep picking, Esc/Done closes), `c` to create a new Jira issue (see [Creating issues](#creating-issues) above), `s` to save the current filters as the default, `S` to cycle swimlane grouping (including by board), `z` to collapse or expand the lane under the cursor (shows a hidden-item count while collapsed), `Z` to collapse or expand every visible lane at once, `P` to push all local shadow changes (opens a review list first — Enter on an item shows a full side-by-side diff, `p` pushes, `q`/Esc cancels), `M` to open Jira metadata (fix versions, components) without leaving the session, `s`/`o`/`d` for the local shadow view / original synced issue / shadow diff on a work item, `r` to revert the current item shadow, `p` to push the current item shadow to Jira, and `q` to go back or quit.

Work item detail shows local hierarchy when available. Child items show their parent epic above the title; epics show locally synced children below the epic line.

When Jira API access is configured, the detail view also shows a **Development** panel listing any branches/PRs already linked to the issue (via whatever git-hosting integration your Jira site has — GitHub, Bitbucket, etc.), fetched once when you open the item. This reads Jira's own Development panel data — read-only in this tool, and no GitHub/GitLab token or config is needed for it. Since that data comes from an internal Jira API rather than an officially documented one, the panel simply stays empty if it's ever unavailable rather than erroring. (Creating branches/PRs from jira-workbench is not implemented yet.)

That same live check (`issue_editmeta`, fetched once per item opened) also drives two more editable fields beyond the fixed set: **Due date**, and *any* labels-type custom field your Jira site has (the same underlying field type as native Labels — for example a "Customers" field), discovered generically rather than hardcoded to one specific field. Neither shows up until that fetch succeeds, and both stay hidden (not erroring) if it fails or no API is configured — native Labels editing is unaffected either way, since it's always available regardless of that fetch. Due date takes `YYYY-MM-DD`; typing `(none)` clears it, leaving the prompt blank cancels (same convention as everywhere else a "no value" option exists). A labels-type custom field opens the same checklist picker as Labels.

The index list also has a **Dev** column with an at-a-glance Git/PR indicator for every synced issue — blank if there's no branch/PR, "Branch" if there's a branch but no PR yet, or the PR's state (Open/Draft/Merged/Declined) if there is one. Unlike the Development panel above, this needs no live API call at all: Jira Cloud already mirrors a summary of that data into a regular custom field (`[view].dev_status_field`, default `customfield_10000`) that a normal sync fetches for every issue. Click a Dev cell to resolve and open the actual branch/PR — that click is the one point this does make a live call (to fetch the real URL), and only for that one issue; if there's more than one branch/PR linked, a picker lets you choose which to open.

If an issue has Jira issue links (blocks/is blocked by/relates to/etc.), Detail shows one row per link phrase, each a pill per linked issue key. This is read-only for now — selecting the row just notes that it isn't editable yet.

The generated layout is:

```text
jira/
├── project.json
├── meta/
│   ├── versions.json
│   └── boards.json
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

Before pushing a work item, Jira Workbench fetches the remote `updated` value and compares it to the value captured when the first local shadow edit was created. If it differs, that doesn't necessarily mean a conflict -- a comment from someone else, a linked dependency, or any other unrelated change also bumps `updated` without touching the fields you're pushing. In that case Jira Workbench re-fetches the full issue and checks, field by field, whether anything *you're actually editing* also changed remotely (a version rename is not treated as a conflict, since the underlying version id is unchanged -- only the display name). The push is blocked only if a field you edited was also genuinely changed remotely; otherwise it proceeds, and the post-push refresh picks up whatever else changed remotely along the way.

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
