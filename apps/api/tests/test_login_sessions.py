"""`_sessions` lookups — no Docker or SSH involved, just the in-memory dict.

`login._sessions` is a plain process-local dict (see the module docstring): a
restart drops every sign-in in flight. `get` stamps ids with a per-process
epoch so a lookup miss can say *why* the id is unknown — from a since-gone
process, rather than simply wrong — instead of a generic "no such sign-in".
"""

from __future__ import annotations

import pytest

from moonphase import login
from moonphase.harness import HarnessKind


def test_a_session_from_this_process_is_found() -> None:
    session = login.LoginSession(
        id=f"{login._PROCESS_EPOCH}.abc123",
        org_id="org",
        harness_kind=str(HarnessKind.CLAUDE_CODE),
        server_id="server",
        container=f"{login.CONTAINER_PREFIX}abc123",
    )
    login._sessions[session.id] = session
    try:
        assert login.get(session.id) is session
    finally:
        login.forget(session.id)


def test_an_id_stamped_with_an_earlier_epoch_reports_a_restart() -> None:
    """The dict can never actually hold this id — a mismatched epoch is only
    possible if the process that issued it is gone — so this must be
    distinguishable from a plain unknown id."""
    with pytest.raises(login.SessionLostToRestart):
        login.get("some-earlier-epoch.abc123")


def test_a_genuinely_unknown_id_is_just_not_found() -> None:
    assert login.get("not-a-real-session-id") is None
