"""Background refresh for `jira-wb serve --watch`.

Two tiers, chosen per tick:

- quick (every `interval` seconds): one "updated in the last N minutes" JQL
  request (sync.quick_sync). When nothing changed that request is the whole
  cost.
- full (every `full_interval` seconds, or when the last check is older than
  that -- e.g. after a long idle pause or a server restart): the same
  per-project sync_project a manual `jira-wb sync` runs, which also covers
  what a quick sync can't see (deletions, metadata, board moves that don't
  bump `updated`).

Load controls: ticks pause entirely once the UI has had no requests for
`idle_after` seconds (the status poll doesn't count) and resume on the next
real request; a tick is skipped while another process holds the sync lock;
failures back off exponentially (honoring a 429's Retry-After) up to 30
minutes; every delay is jittered +-10%.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .sync import SyncLockBusy, read_json, sync_lock, write_json

MAX_BACKOFF_SECONDS = 1800
LOCK_RETRY_SECONDS = 30
WINDOW_OVERLAP_MINUTES = 2


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """A rate-limited (HTTP 429) response's Retry-After, if the exception
    carries one (requests.HTTPError, which atlassian-python-api raises)."""
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 429:
        return None
    value = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        return max(1.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


class BackgroundSync:
    def __init__(
        self,
        jira_dir: Path,
        *,
        quick: Callable[[int], int],
        full: Callable[[], int],
        interval: float = 300,
        full_interval: float = 3600,
        idle_after: float = 900,
        now: Callable[[], float] = time.time,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        """quick(since_minutes) and full() each return how many items changed."""
        self.jira_dir = jira_dir
        self._quick = quick
        self._full = full
        self.interval = interval
        self.full_interval = full_interval
        self.idle_after = idle_after
        self._now = now
        self._jitter = jitter if jitter is not None else (lambda seconds: seconds * random.uniform(0.9, 1.1))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        state = read_json(self._state_path()) if self._state_path().exists() else None
        state = state if isinstance(state, dict) else {}
        self.last_check_at = _parse_iso(state.get("lastCheckAt"))
        self.last_full_at = _parse_iso(state.get("lastFullAt"))
        self.last_activity = now()
        self.state = "starting"
        self.last_kind: str | None = None
        self.last_error: str | None = None
        self.failures = 0
        self.changes_total = 0

    # -- public API ------------------------------------------------------

    def touch(self) -> None:
        """Record UI activity; wakes a paused loop immediately."""
        self.last_activity = self._now()
        if self.state == "paused":
            self._wake.set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="jira-wb-watch", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def status(self) -> dict[str, Any]:
        now = self._now()
        return {
            "enabled": True,
            "state": self.state,
            "lastKind": self.last_kind,
            "lastSuccessAt": _iso(self.last_check_at),
            "lastSuccessAgeSeconds": None if self.last_check_at is None else max(0, int(now - self.last_check_at)),
            "changesTotal": self.changes_total,
            "error": self.last_error,
        }

    # -- one scheduling step (directly testable) ---------------------------

    def tick(self) -> float | None:
        """Runs at most one refresh; returns seconds until the next tick, or
        None to sleep until touch() wakes it."""
        now = self._now()
        if now - self.last_activity > self.idle_after:
            self.state = "paused"
            return None
        try:
            with sync_lock(self.jira_dir, blocking=False):
                self.state = "running"
                due_full = (
                    self.last_full_at is None
                    or self.last_check_at is None
                    or now - self.last_full_at >= self.full_interval
                    or now - self.last_check_at >= self.full_interval
                )
                if due_full:
                    changed = self._full()
                    self.last_full_at = now
                    self.last_kind = "full"
                else:
                    since = math.ceil((now - self.last_check_at) / 60) + WINDOW_OVERLAP_MINUTES
                    changed = self._quick(since)
                    self.last_kind = "quick"
                self.last_check_at = now
                self._save_state()
        except SyncLockBusy:
            self.state = "waiting"
            return LOCK_RETRY_SECONDS
        except Exception as exc:  # noqa: BLE001 -- reported via status(), retried with backoff
            self.failures += 1
            self.last_error = str(exc) or exc.__class__.__name__
            self.state = "backoff"
            delay = retry_after_seconds(exc)
            if delay is None:
                delay = min(self.interval * (2**self.failures), MAX_BACKOFF_SECONDS)
            return self._jitter(delay)
        with self._lock:
            self.changes_total += max(0, int(changed or 0))
        self.failures = 0
        self.last_error = None
        self.state = "idle"
        return self._jitter(self.interval)

    # -- internals ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            delay = self.tick()
            self._wake.wait(delay)
            self._wake.clear()

    def _state_path(self) -> Path:
        return self.jira_dir / "meta" / "watch-state.json"

    def _save_state(self) -> None:
        self._state_path().parent.mkdir(parents=True, exist_ok=True)
        write_json(
            self._state_path(),
            {"lastCheckAt": _iso(self.last_check_at), "lastFullAt": _iso(self.last_full_at)},
        )
