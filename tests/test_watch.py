from __future__ import annotations

from pathlib import Path
from typing import Any

from jira_workbench.sync import sync_lock
from jira_workbench.watch import BackgroundSync, retry_after_seconds


class Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def _syncer(tmp_path: Path, clock: Clock, calls: list[Any], **kwargs: Any) -> BackgroundSync:
    def quick(since: int) -> int:
        calls.append(("quick", since))
        return 0

    def full() -> int:
        calls.append(("full",))
        return 3

    return BackgroundSync(
        tmp_path,
        quick=kwargs.get("quick", quick),
        full=kwargs.get("full", full),
        interval=300,
        full_interval=3600,
        idle_after=900,
        now=clock,
        jitter=lambda seconds: seconds,
    )


def test_first_tick_is_a_full_refresh_then_quick_checks_with_overlapping_window(tmp_path: Path) -> None:
    clock, calls = Clock(), []
    syncer = _syncer(tmp_path, clock, calls)

    assert syncer.tick() == 300
    clock.t += 300
    syncer.touch()
    assert syncer.tick() == 300

    assert calls == [("full",), ("quick", 7)]  # 5 minutes + 2 minutes overlap
    assert syncer.status()["changesTotal"] == 3
    assert syncer.status()["lastKind"] == "quick"


def test_full_refresh_runs_again_once_full_interval_passes(tmp_path: Path) -> None:
    clock, calls = Clock(), []
    syncer = _syncer(tmp_path, clock, calls)
    syncer.tick()
    for _ in range(12):
        clock.t += 300
        syncer.touch()
        syncer.tick()

    assert calls.count(("full",)) == 2
    assert calls[-1] == ("full",)


def test_state_survives_restart_so_a_recent_check_resumes_with_a_quick_sync(tmp_path: Path) -> None:
    clock, calls = Clock(), []
    _syncer(tmp_path, clock, calls).tick()
    clock.t += 120

    restarted = _syncer(tmp_path, clock, calls)
    restarted.tick()

    assert calls == [("full",), ("quick", 4)]


def test_pauses_when_ui_is_idle_and_resumes_on_activity(tmp_path: Path) -> None:
    clock, calls = Clock(), []
    syncer = _syncer(tmp_path, clock, calls)
    syncer.tick()
    clock.t += 901

    assert syncer.tick() is None
    assert syncer.status()["state"] == "paused"
    assert calls == [("full",)]

    syncer.touch()
    assert syncer.tick() == 300
    assert calls == [("full",), ("quick", 18)]


def test_skips_tick_while_another_sync_holds_the_lock(tmp_path: Path) -> None:
    clock, calls = Clock(), []
    syncer = _syncer(tmp_path, clock, calls)

    with sync_lock(tmp_path):
        assert syncer.tick() == 30

    assert calls == []
    assert syncer.status()["state"] == "waiting"


def test_failures_back_off_exponentially_and_reset_on_success(tmp_path: Path) -> None:
    clock, calls = Clock(), []
    attempts = {"n": 0}

    def flaky_full() -> int:
        attempts["n"] += 1
        if attempts["n"] <= 4:
            raise RuntimeError("jira down")
        return 0

    syncer = _syncer(tmp_path, clock, calls, full=flaky_full)

    assert [syncer.tick() for _ in range(4)] == [600, 1200, 1800, 1800]
    assert syncer.status()["state"] == "backoff"
    assert syncer.status()["error"] == "jira down"
    assert syncer.tick() == 300
    assert syncer.status()["error"] is None


def test_rate_limit_honors_retry_after(tmp_path: Path) -> None:
    class Response:
        status_code = 429
        headers = {"Retry-After": "120"}

    class RateLimited(Exception):
        response = Response()

    def limited_full() -> int:
        raise RateLimited()

    syncer = _syncer(tmp_path, Clock(), [], full=limited_full)

    assert syncer.tick() == 120
    assert retry_after_seconds(RuntimeError("x")) is None


def test_start_and_stop_run_the_background_thread(tmp_path: Path) -> None:
    import threading

    ran = threading.Event()

    def full() -> int:
        ran.set()
        return 0

    syncer = BackgroundSync(tmp_path, quick=lambda since: 0, full=full, interval=3600)
    syncer.start()
    assert ran.wait(5)
    syncer.stop()

    assert syncer._thread is not None and not syncer._thread.is_alive()
