"""What a sweep costs, and how it behaves when something misbehaves.

Two things the monitor must get right. It runs forever against every project at
once, so the cost of a sweep has to follow the number of containers rather than
the number of agents inside them. And an unreachable server is ordinary — it
must not become a stream of identical failures, nor silence the projects that
are answering perfectly well.
"""

from __future__ import annotations

from typing import Any

import pytest

from moonphase.activity import ActivitySignals, ActivityState, Snapshot
from moonphase.monitor import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    SessionMonitor,
)
from moonphase.ssh import SSHError


class _Clock:
    """A hand-wound clock, so backoff is tested by arithmetic not by waiting."""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _NullSession:
    async def __aenter__(self):
        class _Conn:
            async def execute(self, *args: Any, **kwargs: Any) -> None:
                return None

        return _Conn()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Harness:
    def activity_signals(self) -> ActivitySignals:
        return ActivitySignals(prompt_patterns=[], busy_patterns=[])


class _Running:
    state = "running"


@pytest.fixture
def monitor_and_clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("moonphase.monitor.time.monotonic", lambda: clock.now)
    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    return SessionMonitor(), clock


def _row(project: str, server: str, container: str, session: str) -> dict[str, Any]:
    return {
        "id": project, "org_id": "o1", "name": project, "server_id": server,
        "harness": "claude_code", "container_name": container,
        "session_id": f"{container}:{session}", "tmux_session": session,
        "user_id": "u1", "activity": "working", "pane_digest": "same",
        "notified_state": None,
    }


ROWS = [
    _row("alpha", "srv-1", "c-alpha", "one"),
    _row("alpha", "srv-1", "c-alpha", "two"),
    _row("gamma", "srv-2", "c-gamma", "one"),
]


async def _sweep(
    monitor: SessionMonitor,
    monkeypatch,
    *,
    unreachable: frozenset[str] | set[str] = frozenset(),
    broken: frozenset[str] | set[str] = frozenset(),
    rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Run one sweep and report which containers were actually inspected."""
    inspected: list[str] = []
    chosen = rows if rows is not None else ROWS

    async def fake_rows(_conn):
        return chosen

    async def fake_target(row):
        if str(row["server_id"]) in unreachable:
            raise SSHError("Authentication failed")
        return object()

    async def fake_get(_target):
        return object()

    async def fake_check(_conn, container, group):
        if container in broken:
            raise SSHError("container is not answering")
        inspected.append(container)
        return len(group)

    monkeypatch.setattr("moonphase.monitor._running_projects", fake_rows)
    monkeypatch.setattr(monitor, "_target_for", fake_target)
    monkeypatch.setattr("moonphase.ssh.pool.get", fake_get)
    monkeypatch.setattr(monitor, "_check_container", fake_check)
    await monitor.sweep()
    return inspected


# --- backoff ----------------------------------------------------------------


async def test_a_failing_server_is_skipped_on_the_next_sweep(
    monitor_and_clock, monkeypatch
) -> None:
    monitor, clock = monitor_and_clock

    inspected = await _sweep(monitor, monkeypatch, unreachable={"srv-1"})
    assert inspected == ["c-gamma"], "the healthy server should still be checked"

    clock.advance(20)
    inspected = await _sweep(monitor, monkeypatch, unreachable={"srv-1"})
    assert inspected == ["c-gamma"]
    assert monitor._failures["srv-1"] == 1, "a skipped sweep is not a new failure"


async def test_backoff_grows_and_is_capped(monitor_and_clock, monkeypatch) -> None:
    monitor, clock = monitor_and_clock

    delays = []
    for _ in range(8):
        await _sweep(monitor, monkeypatch, unreachable={"srv-1", "srv-2"})
        delays.append(monitor._retry_after["srv-1"] - clock.now)
        clock.advance(delays[-1] + 1)

    assert delays[0] == BASE_BACKOFF_SECONDS
    assert delays[1] > delays[0], "backoff should grow"
    assert max(delays) <= MAX_BACKOFF_SECONDS, "backoff must be capped"
    # Capped rather than growing forever: a server that comes back should be
    # noticed in minutes, not hours.
    assert delays[-1] == MAX_BACKOFF_SECONDS


async def test_recovery_clears_the_backoff(monitor_and_clock, monkeypatch) -> None:
    monitor, clock = monitor_and_clock

    await _sweep(monitor, monkeypatch, unreachable={"srv-1"})
    assert "srv-1" in monitor._retry_after

    clock.advance(BASE_BACKOFF_SECONDS + 1)
    inspected = await _sweep(monitor, monkeypatch)

    assert sorted(inspected) == ["c-alpha", "c-gamma"]
    assert "srv-1" not in monitor._failures, "a success must reset the counter"
    assert "srv-1" not in monitor._retry_after


async def test_one_bad_container_does_not_silence_the_others(
    monitor_and_clock, monkeypatch
) -> None:
    """A container that will not answer says nothing about its neighbours.

    Failing per session used to set a server-wide backoff, so one unresponsive
    project froze every other project on the same machine — which is how three
    sessions came to sit reporting hours-old state while a fourth updated
    normally.
    """
    monitor, clock = monitor_and_clock
    rows = ROWS + [_row("delta", "srv-1", "c-delta", "one")]

    inspected = await _sweep(monitor, monkeypatch, broken={"c-alpha"}, rows=rows)
    assert "c-delta" in inspected, "a healthy container on the same server was skipped"
    assert "c-gamma" in inspected
    assert "srv-1" not in monitor._retry_after, (
        "a container fault must not back off the whole machine"
    )

    clock.advance(20)
    inspected = await _sweep(monitor, monkeypatch, broken={"c-alpha"}, rows=rows)
    assert "c-delta" in inspected, "and it must still be checked on the next sweep"


# --- cost --------------------------------------------------------------------


async def test_a_sweep_costs_per_container_not_per_session(
    monitor_and_clock, monkeypatch
) -> None:
    """The reason this was restructured.

    Every session used to be asked about separately: inspect the container,
    check the tmux session exists, capture its pane. Four agents in one project
    meant twelve round trips a sweep, eleven of them re-asking what the first
    already answered — every twenty seconds, forever, per project.
    """
    monitor, _clock = monitor_and_clock
    rows = [_row("alpha", "srv-1", "c-alpha", f"s{n}") for n in range(6)]
    calls: list[str] = []

    async def fake_inspect(_conn, container):
        calls.append(f"inspect:{container}")
        return _Running()

    async def fake_panes(_conn, container, **kwargs):
        calls.append(f"capture:{container}")
        return {f"s{n}": "a static pane" for n in range(6)}

    async def fake_rows(_conn):
        return rows

    async def fake_target(_row):
        return object()

    async def fake_get(_target):
        return object()

    monkeypatch.setattr("moonphase.monitor.docker_remote.inspect", fake_inspect)
    monkeypatch.setattr("moonphase.monitor.sessions.capture_all_panes", fake_panes)
    monkeypatch.setattr("moonphase.monitor._running_projects", fake_rows)
    monkeypatch.setattr(monitor, "_target_for", fake_target)
    monkeypatch.setattr("moonphase.ssh.pool.get", fake_get)
    monkeypatch.setattr("moonphase.monitor.harness_registry.get", lambda kind: _Harness())

    await monitor.sweep()

    assert calls == ["inspect:c-alpha", "capture:c-alpha"], (
        f"six sessions should cost two round trips, not {len(calls)}: {calls}"
    )


# --- the stillness clock -----------------------------------------------------


async def test_a_pane_that_stops_changing_accumulates_stillness(
    monitor_and_clock, monkeypatch
) -> None:
    """The bug that left a finished agent reporting "working" overnight.

    Whether a session is idle is decided by how long its pane has been still,
    and the monitor measures that with a per-session clock. That clock was
    started only when the pane *changed* — so a session that stopped changing
    never started one, the stillness it reported was zero on every sweep, and
    the idle branch could not be reached. Observed as a blue "working" dot ten
    hours after the agent had finished.
    """
    monitor, clock = monitor_and_clock
    seen: list[float] = []

    def fake_classify(pane, *, signals, previous_digest, still_for_seconds):
        seen.append(still_for_seconds)
        return Snapshot(state=ActivityState.WORKING, digest="same")

    async def fake_inspect(_conn, _container):
        return _Running()

    async def fake_panes(_conn, _container, **kwargs):
        return {"one": "a pane that never changes"}

    monkeypatch.setattr("moonphase.monitor.docker_remote.inspect", fake_inspect)
    monkeypatch.setattr("moonphase.monitor.sessions.capture_all_panes", fake_panes)
    monkeypatch.setattr("moonphase.monitor.activity.classify", fake_classify)
    monkeypatch.setattr("moonphase.monitor.harness_registry.get", lambda kind: _Harness())

    group = [_row("alpha", "srv-1", "c-alpha", "one")]
    for _ in range(4):
        await monitor._check_container(object(), "c-alpha", group)
        clock.advance(30)

    assert seen[0] == 0, "the first look has nothing to compare against"
    assert seen[-1] >= 90, (
        f"stillness never accumulated: {seen} — a pane that stops changing must "
        "eventually be recognised as idle"
    )
    assert seen == sorted(seen), f"stillness must only grow while nothing moves: {seen}"
