"""A container that comes back from a reboot with nothing running in it.

The restart policy brings the container itself back, but tmux — and every
agent's conversation with it — does not survive that. Previously this was
purely informational ("Resume a session to pick it back up"); the monitor now
attempts the same `--continue` resume the button would, one session at a
time, so a reboot is invisible rather than an errand per session.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from moonphase import monitor as monitor_module
from moonphase.monitor import SessionMonitor
from moonphase.profile import WorkspaceProfile


class _NullSession:
    async def __aenter__(self):
        class _Conn:
            async def execute(self, *args: Any, **kwargs: Any) -> None:
                return None

        return _Conn()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Running:
    state = "running"


def _row(session: str, *, user_id: str | None = "u1") -> dict[str, Any]:
    return {
        "id": "proj-1", "org_id": "o1", "name": "alpha", "server_id": "srv-1",
        "harness": "claude_code", "container_name": "c-alpha",
        "session_id": f"c-alpha:{session}", "tmux_session": session,
        "user_id": user_id, "home_dir": f"/home/dev/sessions/{session}",
        "workdir": f"/home/dev/sessions/{session}/work",
        "activity": "working", "pane_digest": "same", "notified_state": None,
    }


def _authed_profile() -> WorkspaceProfile:
    from moonphase.harness import HarnessAuthMode, HarnessCredential

    return WorkspaceProfile(
        org_id="o1",
        harness_credential=HarnessCredential(mode=HarnessAuthMode.API_KEY, api_key="sk-x"),
    )


@pytest.fixture
def monitor(monkeypatch):
    async def fake_inspect(_conn, _container):
        return _Running()

    async def empty_panes(_conn, _container, **kw):
        return {}

    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    monkeypatch.setattr("moonphase.monitor.docker_remote.inspect", fake_inspect)
    monkeypatch.setattr("moonphase.monitor.sessions.capture_all_panes", empty_panes)
    return SessionMonitor()


async def test_every_session_resumes_on_its_own(monitor, monkeypatch) -> None:
    resumed: list[dict[str, Any]] = []

    async def fake_org(_conn, _uid):
        return "o1"

    async def fake_project(_conn, _pid):
        return {"id": "proj-1"}

    async def fake_profile(*a, **k):
        return _authed_profile()

    async def fake_ensure(_conn, _container, **kwargs):
        resumed.append(kwargs)
        return False

    monkeypatch.setattr(
        "moonphase.monitor.queries.personal_org_id_for_user_privileged", fake_org
    )
    monkeypatch.setattr("moonphase.monitor.queries.get_project", fake_project)
    monkeypatch.setattr(
        "moonphase.monitor.runtime.load_session_profile_privileged", fake_profile
    )
    monkeypatch.setattr("moonphase.monitor.sessions.ensure_session", fake_ensure)

    group = [_row("one"), _row("two")]
    await monitor._check_container(object(), "c-alpha", group)

    assert {r["session"] for r in resumed} == {"one", "two"}
    assert all(r["resume"] is True for r in resumed)


async def test_a_project_that_no_longer_exists_does_not_stop_its_neighbour(
    monitor, monkeypatch
) -> None:
    """One session's dangling reference must not sink the sweep for the rest."""

    async def fake_org(_conn, _uid):
        return "o1"

    async def fake_project(_conn, pid):
        return None if pid == "gone" else {"id": pid}

    async def fake_profile(*a, **k):
        return _authed_profile()

    resumed: list[str] = []

    async def fake_ensure(_conn, _container, *, session, **kwargs):
        resumed.append(session)
        return False

    monkeypatch.setattr(
        "moonphase.monitor.queries.personal_org_id_for_user_privileged", fake_org
    )
    monkeypatch.setattr("moonphase.monitor.queries.get_project", fake_project)
    monkeypatch.setattr(
        "moonphase.monitor.runtime.load_session_profile_privileged", fake_profile
    )
    monkeypatch.setattr("moonphase.monitor.sessions.ensure_session", fake_ensure)

    ok_row = _row("healthy")
    broken_row = _row("orphaned")
    broken_row["id"] = "gone"

    result = await monitor._auto_resume(object(), "c-alpha", [ok_row, broken_row])

    assert resumed == ["healthy"]
    assert result == (1, 1)


async def test_a_session_with_no_owner_is_left_for_a_person(monitor, monkeypatch) -> None:
    """Pre-ownership sessions (shared home, no user_id) have no account to
    resume on — this must not raise, just count it as needing a manual resume."""
    called = False

    async def fake_ensure(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr("moonphase.monitor.sessions.ensure_session", fake_ensure)

    result = await monitor._auto_resume(
        object(), "c-alpha", [_row("legacy", user_id=None)]
    )

    assert result == (0, 1)
    assert not called


async def test_a_credential_that_no_longer_authenticates_counts_as_failed(
    monitor, monkeypatch
) -> None:
    async def fake_org(_conn, _uid):
        return "o1"

    async def fake_project(_conn, _pid):
        return {"id": "proj-1"}

    async def fake_profile(*a, **k):
        # No harness_credential set — has_harness_auth is False.
        return WorkspaceProfile(org_id="o1")

    async def fake_ensure(*a, **k):
        raise AssertionError("must not attempt to resume without a credential")

    monkeypatch.setattr(
        "moonphase.monitor.queries.personal_org_id_for_user_privileged", fake_org
    )
    monkeypatch.setattr("moonphase.monitor.queries.get_project", fake_project)
    monkeypatch.setattr(
        "moonphase.monitor.runtime.load_session_profile_privileged", fake_profile
    )
    monkeypatch.setattr("moonphase.monitor.sessions.ensure_session", fake_ensure)

    result = await monitor._auto_resume(object(), "c-alpha", [_row("one")])
    assert result == (0, 1)


async def test_partial_recovery_says_so_in_the_status_detail(monitor, monkeypatch) -> None:
    recorded: dict[str, Any] = {}

    async def fake_reconcile(_self, row, *, status, detail):
        recorded["status"] = status
        recorded["detail"] = detail

    async def fake_auto_resume(_self, _conn, _container, _group):
        return (1, 1)

    monkeypatch.setattr(SessionMonitor, "_reconcile_project", fake_reconcile)
    monkeypatch.setattr(SessionMonitor, "_auto_resume", fake_auto_resume)

    await monitor._check_container(object(), "c-alpha", [_row("one"), _row("two")])

    assert recorded["status"] == "running"
    assert "1 of 2" in recorded["detail"]


async def test_full_recovery_clears_the_status_detail(monitor, monkeypatch) -> None:
    recorded: dict[str, Any] = {}

    async def fake_reconcile(_self, row, *, status, detail):
        recorded["status"] = status
        recorded["detail"] = detail

    async def fake_auto_resume(_self, _conn, _container, _group):
        return (2, 0)

    monkeypatch.setattr(SessionMonitor, "_reconcile_project", fake_reconcile)
    monkeypatch.setattr(SessionMonitor, "_auto_resume", fake_auto_resume)

    await monitor._check_container(object(), "c-alpha", [_row("one"), _row("two")])

    assert recorded["status"] == "running"
    assert recorded["detail"] is None


# ---------------------------------------------------------------------------
# Not retrying forever
#
# The condition that triggers auto-resume — running container, session rows,
# no panes — stays true for precisely the sessions that failed to come back.
# Without a backoff the monitor retries them every sweep: at a 20-second
# interval, one session whose owner revoked their credential becomes a few
# thousand pointless SSH round-trips a day, each one logged as a fresh
# failure.
# ---------------------------------------------------------------------------


def test_a_failed_resume_is_not_retried_on_the_very_next_sweep(monitor) -> None:
    assert monitor._resume_due("c-alpha") is True

    monitor._resume_attempted["c-alpha"] = time.monotonic()
    assert monitor._resume_due("c-alpha") is False


def test_the_backoff_expires(monitor) -> None:
    monitor._resume_attempted["c-alpha"] = (
        time.monotonic() - monitor_module.RESUME_RETRY_INTERVAL_SECONDS - 1
    )
    assert monitor._resume_due("c-alpha") is True


def test_a_container_that_never_failed_resumes_immediately(monitor) -> None:
    """A reboot has to be invisible, so a first attempt never waits."""
    assert monitor._resume_due("never-seen-before") is True


async def test_a_clean_resume_leaves_the_next_reboot_immediate(
    monitor, monkeypatch
) -> None:
    """Backoff is earned by failure, and must not outlive it.

    A container that failed once, waited out the backoff, then came back
    cleanly must not carry the old failure's clock into the next reboot.
    """
    monitor._resume_attempted["c-alpha"] = (
        time.monotonic() - monitor_module.RESUME_RETRY_INTERVAL_SECONDS - 1
    )

    async def fake_reconcile(_self, _row, *, status, detail):
        return None

    async def fake_auto_resume(_self, _conn, _container, _group):
        return (2, 0)

    monkeypatch.setattr(SessionMonitor, "_reconcile_project", fake_reconcile)
    monkeypatch.setattr(SessionMonitor, "_auto_resume", fake_auto_resume)

    await monitor._check_container(object(), "c-alpha", [_row("one")])

    assert monitor._resume_due("c-alpha") is True


async def test_a_failed_resume_starts_the_clock_through_a_real_sweep(
    monitor, monkeypatch
) -> None:
    async def fake_reconcile(_self, _row, *, status, detail):
        return None

    async def fake_auto_resume(_self, _conn, _container, _group):
        return (0, 1)

    monkeypatch.setattr(SessionMonitor, "_reconcile_project", fake_reconcile)
    monkeypatch.setattr(SessionMonitor, "_auto_resume", fake_auto_resume)

    await monitor._check_container(object(), "c-alpha", [_row("one")])

    assert monitor._resume_due("c-alpha") is False
