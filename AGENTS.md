# Instructions for coding agents

These instructions apply to every coding agent working in this repository. The Git repository is
the shared project memory. Do not rely on access to prior agent conversations.

The commit, issue, and test conventions below mirror the ones used in the ForeView repository
(`colleeseum/ForeView`). The code base predates them: bringing existing code up to this standard is
tracked as issues in the
[jira-workbench Roadmap](https://github.com/users/colleeseum/projects/4) Project, not done
opportunistically inside unrelated changes.

## Before substantial work

Read these tracked documents before substantial work:

- `README.md` for features, configuration, commands, and the web UI;
- `TODO.md` for known bugs and open design questions not yet captured as issues;
- the [jira-workbench Roadmap](https://github.com/users/colleeseum/projects/4) GitHub Project for
  roadmap priorities and progress.

Inspect the relevant implementation and tests before proposing changes. Treat executable code and
tests as authoritative when documentation conflicts with implementation, but flag and correct the
stale documentation. Follow established project patterns unless there is a justified reason to
change them.

## Progress reporting

For substantial tasks, do not work silently for long periods.

- Break large tasks into smaller, clearly defined steps before implementation.
- Report the initial plan before beginning substantial investigation or changes.
- Provide a brief progress update after each meaningful investigation or implementation step.
- If the requested task is too broad to execute efficiently, propose a smaller sequence of tasks
  rather than silently performing an exhaustive analysis.

## Engineering expectations

- Follow sound software-engineering practices: cohesive modules, clear responsibilities, and
  explicit data flow. Avoid unnecessary coupling and duplication.
- Preserve the module boundaries:
  - `cli.py` parses arguments and reports results. `server.py` adapts HTTP requests and
    responses. Neither should hold business logic that the other front end also needs; shared
    workflows belong in `service.py` or the owning module.
  - `db.py` owns all SQL and the SQLite schema and migrations. No other module issues SQL.
  - `sync.py` owns pulling Jira data into the local cache. `metadata.py` owns project metadata
    caches (versions, components, boards, assignees). `shadow.py` owns staged local edits and
    pushing them. `watch.py` owns the background refresh schedule.
- Talk to Jira only through `atlassian-python-api` clients and the existing helper functions. Do not
  add a second HTTP path to Jira for something an existing helper already covers.
- Preserve backward compatibility of the on-disk cache (`~/.local/share/jira-wb/`) and of
  `config.toml` unless a requirement explicitly changes it. Database migrations in `db.py` are
  append-only: add a new migration rather than editing an applied one.
- Do not introduce dependencies without a concrete justification. Prefer the standard library and
  existing project dependencies.
- For APIs and libraries where current behavior matters (the Jira Cloud REST API in particular),
  consult current authoritative documentation instead of relying only on model knowledge.
- Never commit credentials, API tokens, synced Jira or Freshdesk data, the local cache, or
  `config.toml`. Use synthetic fixtures for tests and examples.

## Live Jira safety

- Tests must never contact a live Jira or Freshdesk instance. Use fake clients.
- Do not push, create, update, or delete real Jira issues (`shadow push`, `issue create` without
  `--dry-run`, or the web UI's push actions) unless the maintainer explicitly asks for it in that
  conversation. Stage changes and let the maintainer push.
- Do not rely on `read_only = true` in `config.toml` as a safety net; it is not reliably enforced.

## Test integrity and completion

- Treat an existing failing test as evidence about application behavior. Fix production code first.
- Do not weaken, delete, rewrite, or change an expected result merely to make a test pass.
- If a test appears incorrect, obsolete, or inconsistent with a new requirement, explain the issue
  before modifying it. When a requirement intentionally changes, state which expectations change
  and why.
- Add unit tests for new behavior and a regression test for every defect not already covered.
  A regression test should fail without the fix; confirm that when practical.
- Target at least 80% branch-aware coverage once coverage is measured (tracked as an issue). Never
  lower an enforced threshold to accommodate a change.
- Run the relevant focused tests while working. Before considering work complete, run what CI
  runs: `ruff check .` and `pytest`.
- Commits should be coherent and reasonably atomic. Do not include unrelated user changes or
  untracked working material.

## GitHub work tracking

- Start substantial feature and defect work from an issue in the
  [jira-workbench Roadmap](https://github.com/users/colleeseum/projects/4) Project (statuses:
  Todo, In Progress, Done), labelled `roadmap`.
- Keep implementation issues atomic: one cohesive, independently reviewable outcome with explicit
  acceptance criteria, normally delivered by one pull request. Use parent issues for broader work
  and create a sub-issue for each planned PR-sized batch before implementation. Close a leaf issue
  only after its completing PR is merged; close its parent after all required children are complete.
- Write issues with an `## Outcome` section and an `## Acceptance criteria` checklist. Title them
  with the roadmap identifier, for example `SYNC-03: Refresh assignees during sync`.
- Whenever creating an issue, including a sub-issue, add it to the GitHub Project in the same
  workflow, set its status, and link its parent when applicable. Verify Project membership; report
  any failure instead of claiming the issue is tracked.
- Roadmap identifiers use an area prefix and a number, with a letter suffix for sub-issues
  (`WEB-04`, `WEB-04A`). Areas:

  | Prefix | Area |
  |---|---|
  | `SYNC` | Jira sync, background refresh (`sync.py`, `watch.py`) |
  | `META` | Project metadata: versions, components, boards, assignees (`metadata.py`) |
  | `SHADOW` | Staged edits and pushing to Jira (`shadow.py`, `issue.py`) |
  | `WEB` | Web UI (`server.py`) |
  | `TUI` | Terminal UI (`view.py`, `tui/`) |
  | `CLI` | Command-line interface and configuration (`cli.py`, `config.py`) |
  | `DB` | SQLite index, schema, migrations (`db.py`) |
  | `FD` | Freshdesk integration (`freshdesk.py`) |
  | `AI` | Assistant integration (`assistant.py`) |
  | `QA` | Tests, coverage, linting, typing, CI, tooling |
  | `DOC` | Documentation |

  Add a new prefix only when no existing area fits.
- Include the roadmap identifier in the branch name, for example `feature/sync-03-assignees`.
- Include both the roadmap identifier and GitHub issue number in commit subjects, for example
  `SYNC-03 (#12): Refresh assignees during sync`. Keep the subject to one line; put detail in the
  body.
- Reference the issue from the pull request. Use `Closes #12` only when the pull request completes
  the entire issue; use `Refs #12` for partial work.
- When pushing a task branch, create a pull request in the same workflow if one does not already
  exist, unless the maintainer explicitly requests otherwise. Include the issue links, scope, and
  verification results, and report the PR URL. Opening or updating a PR does not authorize merging
  it.
- Do not rewrite established history solely to retrofit this convention to older commits.

## Git identity

- Commits attributed to Serge Colle use `serge.colle@mindstep.ca` for both author and committer.
  Do not use another address.
- Do not add AI attribution to commits or pull requests (no `Co-Authored-By` trailers or
  "Generated with" lines).
- Do not use GitHub's merge, squash, or rebase buttons. Land accepted commits from a correctly
  configured local checkout so the committer identity comes from local Git configuration.

## Maintaining shared project context

Keep the shared documentation current as work proceeds. When a change alters behavior, a
configuration option, a command, or a known limitation, update `README.md` (or the relevant focused
document) in the same change. Replace or remove stale statements rather than adding contradictory
notes. Move items out of `TODO.md` once they are captured as issues.

Record only durable project knowledge. Do not add routine implementation details, transient
debugging history, or conversation transcripts.
