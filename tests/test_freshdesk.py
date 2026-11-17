from __future__ import annotations

from pathlib import Path
from typing import Any

from jira_workbench.config import FreshdeskSettings
from jira_workbench.freshdesk import (
    DEFAULT_RETRY_AFTER_SECONDS,
    EPOCH_START,
    MAX_RATE_LIMIT_RETRIES,
    FreshdeskClient,
    FreshdeskError,
    FreshdeskSyncResult,
    find_existing_ticket,
    freshdesk_dir,
    freshdesk_ticket_fields,
    sync_freshdesk_source,
    ticket_key,
)
from jira_workbench.sync import read_json


class _FakeResponse:
    def __init__(
        self, status_code: int, payload: Any = None, text: str = "", headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    """Stands in for requests.Session -- records every call and returns a
    caller-scripted response, so FreshdeskClient's HTTP layer (auth,
    pagination, error handling) can be tested without a real network call
    or a mocking library."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None, tuple[str, str]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, auth: tuple[str, str] | None = None, timeout: int | None = None) -> _FakeResponse:
        self.calls.append((url, params, auth))
        return self.responses.pop(0)


class FakeFreshdeskClient:
    def __init__(
        self,
        tickets: dict[int, dict[str, Any]],
        *,
        index_keys: list[int] | None = None,
        failing_ticket_ids: set[int] = frozenset(),
    ) -> None:
        self.tickets = tickets
        self.index_keys = list(tickets.keys()) if index_keys is None else index_keys
        # Renamed to match reality: with the description/requester embedded
        # directly in list_tickets (see FreshdeskClient.list_tickets), the
        # only remaining per-ticket call that can fail is conversations.
        self.failing_ticket_ids = failing_ticket_ids
        self.calls: list[tuple[Any, ...]] = []

    def list_tickets(self, *, updated_since: str | None = None) -> list[dict[str, Any]]:
        # Full ticket dicts, not bare {"id": ...} -- the real list endpoint
        # (with include=description,requester) already embeds everything a
        # ticket needs, so there's no separate get_ticket call left to
        # simulate here.
        self.calls.append(("list_tickets", updated_since))
        return [self.tickets[ticket_id] for ticket_id in self.index_keys]

    def list_conversations(self, ticket_id: int) -> list[dict[str, Any]]:
        self.calls.append(("list_conversations", ticket_id))
        if ticket_id in self.failing_ticket_ids:
            raise FreshdeskError(f"simulated failure for ticket {ticket_id}")
        return [{"body_text": f"reply on {ticket_id}"}]

    def list_agents(self) -> list[dict[str, Any]]:
        return [{"id": 501, "contact": {"name": "Alice Agent"}}]

    def list_groups(self) -> list[dict[str, Any]]:
        return [{"id": 10, "name": "Support"}]

    def status_names_by_id(self) -> dict[str, str]:
        return {}


def _ticket(ticket_id: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": ticket_id,
        "subject": f"Ticket {ticket_id}",
        "status": 2,
        "priority": 3,
        "type": "Question",
        "responder_id": 501,
        "group_id": 10,
        "tags": ["vip"],
        "updated_at": "2026-01-01T00:00:00Z",
        "description": "<p>body</p>",
    }
    base.update(overrides)
    return base


def test_ticket_key_format() -> None:
    assert ticket_key(10293) == "FD-10293"


def test_freshdesk_ticket_fields_maps_known_status_and_priority() -> None:
    fields = freshdesk_ticket_fields(
        _ticket(1, status=2, priority=4), agents_by_id={"501": "Alice Agent"}, groups_by_id={"10": "Support"}
    )

    assert fields["key"] == "FD-1"
    assert fields["project"] == "FRESHDESK"
    assert fields["component"] == "Support"
    assert fields["status"] == "Open"
    assert fields["statusCategory"] == "new"
    assert fields["priority"] == "Urgent"
    assert fields["assignee"] == "Alice Agent"
    assert fields["labels"] == ["vip"]
    assert fields["fixVersion"] is None
    assert fields["epic"] is None


def test_freshdesk_ticket_fields_maps_every_status_category() -> None:
    open_fields = freshdesk_ticket_fields(_ticket(1, status=2), agents_by_id={}, groups_by_id={})
    pending_fields = freshdesk_ticket_fields(_ticket(2, status=3), agents_by_id={}, groups_by_id={})
    resolved_fields = freshdesk_ticket_fields(_ticket(3, status=4), agents_by_id={}, groups_by_id={})
    closed_fields = freshdesk_ticket_fields(_ticket(4, status=5), agents_by_id={}, groups_by_id={})

    assert (open_fields["status"], open_fields["statusCategory"]) == ("Open", "new")
    assert (pending_fields["status"], pending_fields["statusCategory"]) == ("Pending", "indeterminate")
    assert (resolved_fields["status"], resolved_fields["statusCategory"]) == ("Resolved", "done")
    assert (closed_fields["status"], closed_fields["statusCategory"]) == ("Closed", "done")


def test_freshdesk_ticket_fields_falls_back_for_an_unrecognized_status_code() -> None:
    # Real custom statuses (e.g. "Engineering Backlog", "Waiting on
    # Product") are overwhelmingly mid-workflow, not "just created" --
    # confirmed against a real account -- so the fallback category is
    # "indeterminate", not "new".
    fields = freshdesk_ticket_fields(_ticket(1, status=99), agents_by_id={}, groups_by_id={})

    assert fields["status"] == "Status 99"
    assert fields["statusCategory"] == "indeterminate"


def test_freshdesk_ticket_fields_uses_this_accounts_real_status_name(tmp_path: Path) -> None:
    fields = freshdesk_ticket_fields(
        _ticket(1, status=7), agents_by_id={}, groups_by_id={}, status_names={"7": "Waiting on Engineering"}
    )

    assert fields["status"] == "Waiting on Engineering"
    assert fields["statusCategory"] == "indeterminate"


def test_freshdesk_ticket_fields_prefers_real_status_names_over_the_universal_defaults() -> None:
    # This account's own ticket_fields choices take priority even for the
    # 4 universal ids, in case its agent-facing label differs slightly
    # from STATUS_NAMES' own generic one.
    fields = freshdesk_ticket_fields(_ticket(1, status=2), agents_by_id={}, groups_by_id={}, status_names={"2": "Open"})

    assert fields["status"] == "Open"


def test_freshdesk_ticket_fields_handles_an_unassigned_ticket() -> None:
    fields = freshdesk_ticket_fields(
        _ticket(1, responder_id=None, group_id=None), agents_by_id={"501": "Alice Agent"}, groups_by_id={"10": "Support"}
    )

    assert fields["assignee"] == ""
    assert fields["component"] == ""


def test_freshdesk_ticket_fields_unresolved_agent_or_group_id_falls_back_to_empty() -> None:
    fields = freshdesk_ticket_fields(_ticket(1, responder_id=999, group_id=999), agents_by_id={}, groups_by_id={})

    assert fields["assignee"] == ""
    assert fields["component"] == ""


def test_find_existing_ticket_returns_none_when_missing(tmp_path: Path) -> None:
    assert find_existing_ticket(freshdesk_dir(tmp_path), 42) is None


def test_find_existing_ticket_finds_a_synced_ticket(tmp_path: Path) -> None:
    base_dir = freshdesk_dir(tmp_path)
    ticket_dir = base_dir / "tickets" / "42"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "ticket.json").write_text("{}")

    found = find_existing_ticket(base_dir, 42)

    assert found == ticket_dir / "ticket.json"


def test_sync_freshdesk_source_writes_ticket_and_conversation_files(tmp_path: Path) -> None:
    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    client = FakeFreshdeskClient({1: _ticket(1), 2: _ticket(2, subject="Second")})

    result = sync_freshdesk_source(settings, tmp_path, client, progress=None)

    assert result == FreshdeskSyncResult(ticket_count=2, changed_count=2)
    assert read_json(tmp_path / "freshdesk/tickets/1/ticket.json")["subject"] == "Ticket 1"
    assert read_json(tmp_path / "freshdesk/tickets/2/ticket.json")["subject"] == "Second"
    assert read_json(tmp_path / "freshdesk/tickets/1/conversations.json") == [{"body_text": "reply on 1"}]
    assert read_json(tmp_path / "freshdesk/agents.json") == {"501": "Alice Agent"}
    assert read_json(tmp_path / "freshdesk/groups.json") == {"10": "Support"}


def test_sync_freshdesk_source_indexes_tickets_into_the_sql_index(tmp_path: Path) -> None:
    from jira_workbench import db

    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    client = FakeFreshdeskClient({1: _ticket(1)})

    sync_freshdesk_source(settings, tmp_path, client, progress=None)

    items = db.list_items(tmp_path, source_slug="freshdesk")
    assert [item["key"] for item in items] == ["FD-1"]
    assert items[0]["assignee"] == "Alice Agent"


def test_sync_freshdesk_source_populates_board_membership_for_a_matching_local_board(tmp_path: Path) -> None:
    from jira_workbench import db
    from jira_workbench.metadata import add_local_board

    # Board created before the source has ever synced (so before its
    # `sources` row even exists) -- write_board_settings's own
    # recompute_board_membership_all_sources naturally skips it then; this
    # confirms sync_freshdesk_source's own recompute call is what actually
    # backfills membership once the source exists, not a defect if it
    # doesn't happen automatically before that.
    add_local_board(tmp_path, "FD Board", {"project": ["FRESHDESK"]})

    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    sync_freshdesk_source(settings, tmp_path, FakeFreshdeskClient({1: _ticket(1)}), progress=None)

    items = db.list_items(tmp_path, source_slug="freshdesk")
    assert items[0]["boards"] == ["FD Board"]


def test_sync_freshdesk_source_second_run_passes_updated_since(tmp_path: Path) -> None:
    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    client = FakeFreshdeskClient({1: _ticket(1)})

    sync_freshdesk_source(settings, tmp_path, client, progress=None)
    sync_freshdesk_source(settings, tmp_path, client, progress=None)

    updated_since_values = [call[1] for call in client.calls if call[0] == "list_tickets"]
    assert updated_since_values[0] is None
    assert updated_since_values[1] is not None


def test_sync_freshdesk_source_force_ignores_the_last_sync_cutoff(tmp_path: Path) -> None:
    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    client = FakeFreshdeskClient({1: _ticket(1)})

    sync_freshdesk_source(settings, tmp_path, client, progress=None)
    sync_freshdesk_source(settings, tmp_path, client, force=True, progress=None)

    updated_since_values = [call[1] for call in client.calls if call[0] == "list_tickets"]
    assert updated_since_values == [None, None]


def test_sync_freshdesk_source_keeps_a_tickets_data_when_only_its_conversations_fail(tmp_path: Path) -> None:
    # The ticket's own data comes from the (already-succeeded) list
    # response now, not a separate per-ticket fetch -- only conversations
    # can still fail per ticket, and that shouldn't discard the ticket
    # data already in hand.
    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    client = FakeFreshdeskClient({1: _ticket(1), 2: _ticket(2), 3: _ticket(3)}, failing_ticket_ids={2})

    result = sync_freshdesk_source(settings, tmp_path, client, progress=None)

    assert result.changed_count == 3
    assert result.failed_keys == ("FD-2",)
    assert (tmp_path / "freshdesk/tickets/1/ticket.json").exists()
    assert (tmp_path / "freshdesk/tickets/3/ticket.json").exists()
    assert (tmp_path / "freshdesk/tickets/2/ticket.json").exists()
    assert read_json(tmp_path / "freshdesk/tickets/2/conversations.json") == []


def test_sync_freshdesk_source_still_writes_last_sync_after_a_partial_failure(tmp_path: Path) -> None:
    # A ticket Freshdesk can't serve must not prevent updated_since from
    # advancing -- otherwise every future run keeps redoing a full,
    # unbounded resync instead of the incremental slice this feature is
    # for (see the "index way" -- only sync tickets updated since last
    # time -- the whole point of last_sync.json).
    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    client = FakeFreshdeskClient({1: _ticket(1), 2: _ticket(2)}, failing_ticket_ids={2})

    sync_freshdesk_source(settings, tmp_path, client, progress=None)
    sync_freshdesk_source(settings, tmp_path, client, progress=None)

    updated_since_values = [call[1] for call in client.calls if call[0] == "list_tickets"]
    assert updated_since_values[0] is None
    assert updated_since_values[1] is not None


def test_sync_freshdesk_source_reindex_removes_tickets_no_longer_returned(tmp_path: Path) -> None:
    from jira_workbench import db

    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    client = FakeFreshdeskClient({1: _ticket(1), 2: _ticket(2)})
    sync_freshdesk_source(settings, tmp_path, client, progress=None)
    assert {item["key"] for item in db.list_items(tmp_path, source_slug="freshdesk")} == {"FD-1", "FD-2"}

    # Simulate ticket 2 being deleted from the local tree between syncs
    # (out of scope for this pass's sync loop itself -- reindex just needs
    # to reflect whatever's actually on disk).
    import shutil

    shutil.rmtree(tmp_path / "freshdesk/tickets/2")
    db.reindex_freshdesk_items(tmp_path, source_slug="freshdesk")

    assert {item["key"] for item in db.list_items(tmp_path, source_slug="freshdesk")} == {"FD-1"}


def _client(
    responses: list[_FakeResponse], *, sleeps: list[float] | None = None
) -> tuple[FreshdeskClient, _FakeSession]:
    session = _FakeSession(responses)
    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="my-key")
    sleep = (lambda seconds: sleeps.append(seconds)) if sleeps is not None else (lambda seconds: None)
    return FreshdeskClient(settings, session=session, sleep=sleep), session


def test_freshdesk_client_authenticates_with_api_key_as_username() -> None:
    client, session = _client([_FakeResponse(200, [])])

    client.list_tickets()

    _, _, auth = session.calls[0]
    assert auth == ("my-key", "X")


def test_freshdesk_client_requests_the_correct_base_url() -> None:
    client, session = _client([_FakeResponse(200, [])])

    client.list_tickets()

    url, _, _ = session.calls[0]
    assert url == "https://acme.freshdesk.com/api/v2/tickets"


def test_freshdesk_client_list_tickets_paginates_until_a_short_page(tmp_path: Path) -> None:
    full_page = [{"id": i} for i in range(100)]
    short_page = [{"id": 100}, {"id": 101}]
    client, session = _client([_FakeResponse(200, full_page), _FakeResponse(200, short_page)])

    tickets = client.list_tickets()

    assert len(tickets) == 102
    pages = [call[1]["page"] for call in session.calls]
    assert pages == [1, 2]


def test_freshdesk_client_list_tickets_stops_on_an_empty_page() -> None:
    client, session = _client([_FakeResponse(200, [])])

    tickets = client.list_tickets()

    assert tickets == []
    assert len(session.calls) == 1


def test_freshdesk_client_list_tickets_passes_updated_since() -> None:
    client, session = _client([_FakeResponse(200, [])])

    client.list_tickets(updated_since="2026-01-01T00:00:00Z")

    _, params, _ = session.calls[0]
    assert params["updated_since"] == "2026-01-01T00:00:00Z"


def test_freshdesk_client_list_tickets_embeds_description_and_requester() -> None:
    client, session = _client([_FakeResponse(200, [{"id": 42}])])

    tickets = client.list_tickets()

    assert tickets == [{"id": 42}]
    _, params, _ = session.calls[0]
    assert params["include"] == "description,requester"


def test_freshdesk_client_status_names_by_id_parses_the_real_ticket_fields_shape() -> None:
    # Exact shape confirmed against a real account's own GET /ticket_fields
    # response: choices is a dict keyed by status id (string), each value a
    # [agent_label, customer_label] pair.
    ticket_fields = [
        {"name": "priority", "choices": {"1": "Low"}},
        {
            "name": "status",
            "choices": {
                "2": ["Open", "Working"],
                "7": ["Waiting on Engineering", "Escalated to Engineering"],
                "19": ["Engineering Backlog", "Escalated to Engineering"],
            },
        },
    ]
    client, _session = _client([_FakeResponse(200, ticket_fields)])

    assert client.status_names_by_id() == {
        "2": "Open",
        "7": "Waiting on Engineering",
        "19": "Engineering Backlog",
    }


def test_freshdesk_client_status_names_by_id_returns_empty_without_a_status_field() -> None:
    client, _session = _client([_FakeResponse(200, [{"name": "priority", "choices": {}}])])

    assert client.status_names_by_id() == {}


def test_freshdesk_client_list_tickets_defaults_updated_since_to_epoch_start_when_unset() -> None:
    # Freshdesk's list endpoint defaults to the last 30 days when
    # updated_since is omitted entirely -- a "full" sync (no real cursor
    # yet) must pass an explicit early date instead of leaving it out.
    client, session = _client([_FakeResponse(200, [])])

    client.list_tickets()

    _, params, _ = session.calls[0]
    assert params["updated_since"] == EPOCH_START


def test_freshdesk_client_raises_freshdesk_error_on_http_error() -> None:
    client, _session = _client([_FakeResponse(401, text="invalid credentials")])

    try:
        client.list_tickets()
    except FreshdeskError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected FreshdeskError")


def test_freshdesk_client_retries_a_429_after_waiting_retry_after_seconds() -> None:
    sleeps: list[float] = []
    client, session = _client(
        [
            _FakeResponse(429, headers={"Retry-After": "3"}),
            _FakeResponse(200, [{"body_text": "hi"}]),
        ],
        sleeps=sleeps,
    )

    conversations = client.list_conversations(42)

    assert conversations == [{"body_text": "hi"}]
    assert sleeps == [3.0]
    assert len(session.calls) == 2


def test_freshdesk_client_retries_multiple_consecutive_429s() -> None:
    sleeps: list[float] = []
    client, session = _client(
        [
            _FakeResponse(429, headers={"Retry-After": "1"}),
            _FakeResponse(429, headers={"Retry-After": "2"}),
            _FakeResponse(200, []),
        ],
        sleeps=sleeps,
    )

    client.list_agents()

    assert sleeps == [1.0, 2.0]
    assert len(session.calls) == 3


def test_freshdesk_client_falls_back_to_a_default_wait_when_retry_after_is_missing() -> None:
    sleeps: list[float] = []
    client, _session = _client([_FakeResponse(429), _FakeResponse(200, [])], sleeps=sleeps)

    client.list_agents()

    assert sleeps == [DEFAULT_RETRY_AFTER_SECONDS]


def test_freshdesk_client_gives_up_after_max_retries_and_raises() -> None:
    sleeps: list[float] = []
    client, session = _client(
        [_FakeResponse(429, headers={"Retry-After": "0"}) for _ in range(MAX_RATE_LIMIT_RETRIES)], sleeps=sleeps
    )

    try:
        client.list_agents()
    except FreshdeskError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("expected FreshdeskError")
    assert len(session.calls) == MAX_RATE_LIMIT_RETRIES


def test_freshdesk_client_reports_rate_limit_waits_via_progress() -> None:
    messages: list[str] = []
    session = _FakeSession([_FakeResponse(429, headers={"Retry-After": "4"}), _FakeResponse(200, [])])
    settings = FreshdeskSettings(slug="freshdesk", domain="acme", api_key="key")
    client = FreshdeskClient(settings, session=session, sleep=lambda _seconds: None, progress=messages.append)

    client.list_agents()

    assert any("rate limited" in message and "4" in message for message in messages)
