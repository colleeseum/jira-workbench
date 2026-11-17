"""Freshdesk as a second, read-only source.

Mirrors sync.py's overall shape (a Protocol for the remote client, a
SyncResult-style dataclass, a list-then-fetch-each sync loop) but is a
genuinely separate implementation, not a parameterization of it -- Freshdesk's
REST shape (ticket list/get, agents, groups, conversations) shares nothing
structural with Jira's JQL-based client. See the "Freshdesk as a second
source" plan for the full design rationale.

Read-only: there is no shadow/push pipeline here. A ticket's local files are
always a plain mirror of whatever Freshdesk last returned.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests

from .config import FreshdeskSettings
from .sync import Progress, read_json, utc_now, write_json

DEFAULT_SOURCE_KIND = "freshdesk"

# Bounds how many times a single request retries a 429 before giving up --
# Freshdesk's own docs recommend queuing/retrying after Retry-After rather
# than treating a 429 as a hard failure (even invalid requests count
# against the limit, so a request that eventually succeeds shouldn't count
# as "the ticket failed" the way a real 400/404/5xx does). Bounded rather
# than unbounded so a account that's *permanently* over its plan's rate
# limit (not just transiently bursty) still fails loudly instead of the
# sync hanging forever.
MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_RETRY_AFTER_SECONDS = 5.0

# Stands in for "the beginning of time" when there's no real incremental
# cursor yet (first sync, or --force) -- Freshdesk's list endpoint defaults
# to the last 30 days when `updated_since` is omitted entirely, so a "full"
# sync has to pass an explicit, safely-early date instead of just leaving
# the param out. Predates Freshdesk's own 2010 founding, so it's "all
# tickets any real account could have" for every account.
EPOCH_START = "2000-01-01T00:00:00Z"

# Freshdesk's 4 universal built-in statuses -- present on every account
# regardless of plan, so this is the fallback used only when the real,
# possibly-larger status map (fetched per-account from ticket_fields, see
# FreshdeskClient.status_names_by_id) is missing or doesn't have an entry
# -- e.g. before this account's first sync since this feature shipped.
# Confirmed against a real account (Growth plan or higher can add custom
# statuses -- e.g. "Waiting on Engineering", "Engineering Backlog") that
# these 4 numeric ids are NOT the only ones in practical use; relying on
# this dict alone silently mislabels every custom-status ticket as a bare
# "Status <n>".
STATUS_NAMES: dict[int, str] = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}

# Mapped into Jira's existing 3-bucket statusCategory vocabulary so the
# generic Active-only filtering in view.py's filter_items works unchanged
# across both sources. Any status name not listed here (every custom
# status is, by construction -- ticket_fields' own choices payload has no
# per-choice category to read instead) defaults to "indeterminate", not
# "new": a real account's custom statuses are overwhelmingly mid-workflow
# states ("Engineering Backlog", "Waiting on Product", "Call Scheduled",
# ...), never "just created" ones -- confirmed directly against a real
# account's actual ticket_fields response.
STATUS_CATEGORY: dict[str, str] = {
    "Open": "new",
    "Pending": "indeterminate",
    "Resolved": "done",
    "Closed": "done",
}
DEFAULT_STATUS_CATEGORY = "indeterminate"

PRIORITY_NAMES: dict[int, str] = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}


class FreshdeskError(RuntimeError):
    pass


class FreshdeskApiClient(Protocol):
    # No separate get_ticket -- list_tickets embeds everything a ticket
    # needs (description, requester) directly via `include`, so there's no
    # per-ticket detail fetch left to protocol over. See list_tickets'
    # own docstring/comment for why.
    def list_tickets(self, *, updated_since: str | None = None) -> list[dict[str, Any]]: ...

    def list_conversations(self, ticket_id: int) -> list[dict[str, Any]]: ...

    def list_agents(self) -> list[dict[str, Any]]: ...

    def list_groups(self) -> list[dict[str, Any]]: ...

    def status_names_by_id(self) -> dict[str, str]: ...


class FreshdeskClient:
    """Thin wrapper over Freshdesk API v2. Auth is HTTP Basic with the API
    key as the username and a literal "X" as the password -- Freshdesk's own
    documented convention, not a placeholder left by mistake."""

    def __init__(
        self,
        settings: FreshdeskSettings,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        progress: Progress | None = None,
    ) -> None:
        self._base_url = f"https://{settings.domain}.freshdesk.com/api/v2"
        self._auth = (settings.api_key, "X")
        self._session = session or requests.Session()
        self._sleep = sleep
        self._progress = progress

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
            response = self._session.get(f"{self._base_url}/{path}", params=params, auth=self._auth, timeout=30)
            if response.status_code != 429:
                break
            wait_seconds = self._retry_after_seconds(response)
            if self._progress:
                self._progress(
                    f"rate limited by Freshdesk, waiting {wait_seconds:g}s before retrying "
                    f"{path} (attempt {attempt}/{MAX_RATE_LIMIT_RETRIES})..."
                )
            self._sleep(wait_seconds)
        if response.status_code >= 400:
            raise FreshdeskError(f"Freshdesk API error {response.status_code} for {path}: {response.text[:200]}")
        return response.json()

    @staticmethod
    def _retry_after_seconds(response: Any) -> float:
        raw = response.headers.get("Retry-After") if hasattr(response, "headers") else None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER_SECONDS

    def _paginated(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._get(path, params={**params, "per_page": 100, "page": page})
            if not isinstance(batch, list) or not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    def list_tickets(self, *, updated_since: str | None = None) -> list[dict[str, Any]]:
        # updated_since always has a real value, never omitted -- Freshdesk
        # docs: "By default, only tickets that have been created within the
        # past 30 days will be returned. For older tickets, use the
        # updated_since filter." Omitting it (e.g. whenever there's no
        # local last_sync.json cursor yet, or after --force) would silently
        # scope a "full" sync to the last 30 days only, well short of every
        # ticket the account actually has.
        #
        # include=description,requester embeds both directly in this list
        # response -- Freshdesk's per-call credit cost scales with which
        # `include`s a request asks for, not with how many tickets are in
        # the page, so this is a couple of extra credits per 100-ticket
        # page rather than the 1-2 extra API calls *per ticket* a separate
        # get_ticket() call per item would cost (confirmed against a real
        # sync hitting sustained 429s under that older per-ticket shape).
        # description does need this `include` here even though it doesn't
        # on the single-ticket GET (Freshdesk only defaults it in on that
        # endpoint, not this one) -- see FreshdeskClient's own history of
        # getting this exact `include` value wrong per-endpoint.
        params: dict[str, Any] = {
            "order_by": "updated_at",
            "order_type": "asc",
            "include": "description,requester",
            "updated_since": updated_since or EPOCH_START,
        }
        return self._paginated("tickets", params)

    def list_conversations(self, ticket_id: int) -> list[dict[str, Any]]:
        return self._paginated(f"tickets/{ticket_id}/conversations", {})

    def list_agents(self) -> list[dict[str, Any]]:
        return self._paginated("agents", {})

    def list_groups(self) -> list[dict[str, Any]]:
        return self._paginated("groups", {})

    def status_names_by_id(self) -> dict[str, str]:
        """Every status id -> its agent-facing label, straight from this
        account's own ticket_fields -- not just the 4 universal built-ins
        STATUS_NAMES covers. Confirmed against a real account: `choices`
        is a dict keyed by the numeric status id (as a string), each value
        a 2-element [agent_label, customer_label] pair; this uses the
        agent-facing one, matching what STATUS_NAMES' own labels are.
        Returns {} (not raising) if this account's status field can't be
        found -- callers fall back to STATUS_NAMES for anything missing."""
        for field in self._get("ticket_fields"):
            if isinstance(field, dict) and field.get("name") == "status":
                choices = field.get("choices")
                if isinstance(choices, dict):
                    return {
                        str(status_id): labels[0]
                        for status_id, labels in choices.items()
                        if isinstance(labels, list) and labels
                    }
        return {}


def freshdesk_dir(jira_dir: Path) -> Path:
    return jira_dir / "freshdesk"


def ticket_key(ticket_id: int) -> str:
    return f"FD-{ticket_id}"


def find_existing_ticket(base_dir: Path, ticket_id: int | str) -> Path | None:
    """Locates a synced ticket's ticket.json -- the Freshdesk equivalent of
    sync.py's find_existing_issue, but against a flat tickets/<id>/ shape
    (no component-style subdirectory: a Freshdesk group is metadata, not a
    storage-partitioning key the way a Jira component is used today)."""
    path = base_dir / "tickets" / str(ticket_id) / "ticket.json"
    return path if path.exists() else None


def freshdesk_ticket_fields(
    ticket: dict[str, Any],
    *,
    agents_by_id: dict[str, str],
    groups_by_id: dict[str, str],
    status_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Maps a raw Freshdesk ticket onto the same flattened-item vocabulary
    view.py's generic filter/sort/swimlane functions already consume
    (key/project/component/summary/status/statusCategory/type/priority/
    assignee/fixVersion/epic/epicSummary/labels). Only what's needed for the
    Items list and generic filtering -- description, requester, tags detail
    etc. stay in the raw ticket.json file only, read directly by the Detail
    page, the same way Jira's own SQL index never duplicates description or
    comments either.

    status_names is this account's own real status map (see
    FreshdeskClient.status_names_by_id) -- falls back to the universal
    4-status STATUS_NAMES, then to a bare "Status <n>", for a status id
    neither map has (e.g. status_names wasn't fetched yet -- an old
    ticket.json synced before this existed)."""
    ticket_id = ticket.get("id")
    status_code = ticket.get("status")
    status_name = (status_names or {}).get(str(status_code)) or STATUS_NAMES.get(status_code, f"Status {status_code}")
    responder_id = ticket.get("responder_id")
    group_id = ticket.get("group_id")
    return {
        "key": ticket_key(ticket_id),
        "project": "FRESHDESK",
        "component": groups_by_id.get(str(group_id), "") if group_id is not None else "",
        "summary": ticket.get("subject") or "",
        "status": status_name,
        "statusCategory": STATUS_CATEGORY.get(status_name, DEFAULT_STATUS_CATEGORY),
        "type": ticket.get("type") or "",
        "priority": PRIORITY_NAMES.get(ticket.get("priority"), ""),
        "assignee": agents_by_id.get(str(responder_id), "") if responder_id is not None else "",
        "fixVersion": None,
        "epic": None,
        "epicSummary": None,
        "updated": ticket.get("updated_at"),
        "labels": list(ticket.get("tags") or []),
    }


@dataclass(frozen=True)
class FreshdeskSyncResult:
    ticket_count: int
    changed_count: int
    failed_keys: tuple[str, ...] = ()


def _agent_display_name(agent: dict[str, Any]) -> str:
    contact = agent.get("contact")
    if isinstance(contact, dict):
        name = contact.get("name")
        if isinstance(name, str) and name:
            return name
    return ""


def sync_freshdesk_source(
    settings: FreshdeskSettings,
    jira_dir: Path,
    client: FreshdeskApiClient,
    *,
    force: bool = False,
    progress: Progress | None = None,
) -> FreshdeskSyncResult:
    base_dir = freshdesk_dir(jira_dir)
    tickets_dir = base_dir / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress("[1/4] Refreshing agents, groups, and statuses...")
    agents_by_id = {
        str(agent["id"]): _agent_display_name(agent)
        for agent in client.list_agents()
        if isinstance(agent, dict) and agent.get("id") is not None
    }
    groups_by_id = {
        str(group["id"]): group.get("name") or ""
        for group in client.list_groups()
        if isinstance(group, dict) and group.get("id") is not None
    }
    status_names = client.status_names_by_id()
    write_json(base_dir / "agents.json", agents_by_id)
    write_json(base_dir / "groups.json", groups_by_id)
    write_json(base_dir / "statuses.json", status_names)

    last_sync_path = base_dir / "last_sync.json"
    updated_since = None
    if not force and last_sync_path.exists():
        last_sync = read_json(last_sync_path)
        if isinstance(last_sync, dict) and isinstance(last_sync.get("syncedAt"), str):
            updated_since = last_sync["syncedAt"]

    if progress:
        progress("[2/4] Listing tickets...")
    tickets = client.list_tickets(updated_since=updated_since)

    if progress:
        progress("[3/4] Syncing tickets...")
    changed = 0
    failed: list[str] = []
    total = len(tickets)
    for index, ticket in enumerate(tickets, start=1):
        ticket_id = ticket.get("id")
        if not isinstance(ticket_id, int):
            continue
        key = ticket_key(ticket_id)
        if progress:
            progress(f"[3/4] Syncing tickets... {index}/{total} {key}")
        dest = tickets_dir / str(ticket_id)
        dest.mkdir(parents=True, exist_ok=True)
        # The ticket's own data already came back fully embedded in the
        # list response above (see list_tickets) -- nothing left to fetch
        # or fail for it specifically. Conversations are the one remaining
        # per-ticket call (not embeddable in the list response), so it's
        # the only thing that can still fail here -- a real 400/404/5xx
        # from Freshdesk (we've hit both in practice) shouldn't discard the
        # ticket data already in hand, just leave that one ticket's
        # conversations empty and report it, same as Jira's own
        # backfill_missing_parents skipping an unfetchable parent.
        try:
            conversations = client.list_conversations(ticket_id)
        except FreshdeskError as exc:
            failed.append(key)
            conversations = []
            if progress:
                progress(f"[3/4] could not fetch conversations for {key}: {exc}")
        write_json(dest / "ticket.json", ticket)
        write_json(dest / "conversations.json", conversations)
        changed += 1

    if progress:
        progress("[4/4] Indexing...")
    from . import db as _db  # deferred: avoid a module-level import cycle with db.py

    _db.ensure_source(jira_dir, settings.slug, DEFAULT_SOURCE_KIND, f"Freshdesk ({settings.domain})")
    count = _db.reindex_freshdesk_items(jira_dir, source_slug=settings.slug)
    # Mirrors Jira's build_manifest -> reindex_items -> recompute_board_membership
    # chain -- without this, a local board scoped to this source (e.g.
    # project=FRESHDESK) would never actually show any tickets, since
    # board membership is a derived column, not something reindex itself
    # computes (see recompute_board_membership's own docstring).
    _db.recompute_board_membership(jira_dir, source_slug=settings.slug)

    write_json(last_sync_path, {"syncedAt": utc_now()})
    return FreshdeskSyncResult(ticket_count=count, changed_count=changed, failed_keys=tuple(failed))
