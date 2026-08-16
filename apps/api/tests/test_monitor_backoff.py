"""The monitor's behaviour towards servers it cannot reach.

A server whose key stopped working fails identically on every sweep. Retrying
each of its projects every twenty seconds costs a connection attempt apiece and
fills the log with the same line, which is how a real problem gets missed.
"""

from __future__ import annotations

import pytest

from moonphase.monitor import BASE_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS, SessionMonitor
from moonphase.ssh import SSHError


class _Clock:
    """A hand-wound clock, so backoff is tested by arithmetic not by waiting."""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def monitor_and_clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("moonphase.monitor.time.monotonic", lambda: clock.now)
    return SessionMonitor(), clock


ROWS = [
    {"id": "p1", "name": "alpha", "server_id": "srv-1"},
    {"id": "p2", "name": "beta", "server_id": "srv-1"},
    {"id": "p3", "name": "gamma", "server_id": "srv-2"},
]


async def _sweep(monitor: SessionMonitor, monkeypatch, failing: set[str]) -> list[str]:
    """Run one sweep and report which projects were actually checked."""
    checked: list[str] = []

    async def fake_check(row):
        if row["server_id"] in failing:
            raise SSHError("Authentication failed")
        checked.append(row["id"])

    async def fake_rows(_conn):
        return ROWS

    monkeypatch.setattr(monitor, "_check", fake_check)
    monkeypatch.setattr("moonphase.monitor._running_projects", fake_rows)

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    await monitor.sweep()
    return checked


async def test_a_failing_server_is_skipped_on_the_next_sweep(
    monitor_and_clock, monkeypatch
) -> None:
    monitor, clock = monitor_and_clock

    # First sweep: both of srv-1's projects are attempted and both fail.
    checked = await _sweep(monitor, monkeypatch, failing={"srv-1"})
    assert checked == ["p3"], "the healthy server should still be checked"

    # Immediately after, srv-1 is skipped entirely — not retried per project.
    clock.advance(20)
    checked = await _sweep(monitor, monkeypatch, failing={"srv-1"})
    assert checked == ["p3"]
    assert monitor._failures["srv-1"] == 1, "a skipped sweep is not a new failure"


async def test_backoff_grows_and_is_capped(monitor_and_clock, monkeypatch) -> None:
    monitor, clock = monitor_and_clock

    delays = []
    for _ in range(8):
        await _sweep(monitor, monkeypatch, failing={"srv-1", "srv-2"})
        delays.append(monitor._retry_after["srv-1"] - clock.now)
        # Jump past the backoff so the next sweep actually retries.
        clock.advance(delays[-1] + 1)

    assert delays[0] == BASE_BACKOFF_SECONDS
    assert delays[1] > delays[0], "backoff should grow"
    assert max(delays) <= MAX_BACKOFF_SECONDS, "backoff must be capped"
    # Capped rather than growing forever: a server that comes back should be
    # noticed in minutes, not hours.
    assert delays[-1] == MAX_BACKOFF_SECONDS


async def test_recovery_clears_the_backoff(monitor_and_clock, monkeypatch) -> None:
    monitor, clock = monitor_and_clock

    await _sweep(monitor, monkeypatch, failing={"srv-1"})
    assert "srv-1" in monitor._retry_after

    clock.advance(BASE_BACKOFF_SECONDS + 1)
    checked = await _sweep(monitor, monkeypatch, failing=set())

    assert sorted(checked) == ["p1", "p2", "p3"]
    assert "srv-1" not in monitor._failures, "a success must reset the counter"
    assert "srv-1" not in monitor._retry_after
