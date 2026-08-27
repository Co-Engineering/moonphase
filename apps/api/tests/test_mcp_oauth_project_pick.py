"""Picking a session to relay an MCP OAuth connection through.

"Connect" offered from a project's or the org's own Configure dialog has no
one specific session in hand the way the session-scoped dialog does — the
resulting credential is org-wide regardless of which session carries the
relay, so any one of the caller's own running sessions will do. This pins
down the picker in isolation, since it is the one piece of judgement in an
otherwise mechanical pair of new endpoints.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from moonphase.routers.mcp_oauth import _own_running_session


def _row(*, mine: bool, state: str, name: str = "s") -> dict[str, Any]:
    return {"is_mine": mine, "state": state, "tmux_session": name}


def test_picks_the_callers_own_running_session() -> None:
    rows = [
        _row(mine=False, state="running", name="theirs"),
        _row(mine=True, state="stopped", name="mine-but-stopped"),
        _row(mine=True, state="running", name="mine-and-running"),
    ]
    picked = _own_running_session(rows, where="")
    assert picked["tmux_session"] == "mine-and-running"


def test_any_qualifying_session_is_acceptable() -> None:
    """The credential that comes out is org-wide either way, so which one of
    several candidates gets used is not a decision worth agonising over."""
    rows = [_row(mine=True, state="running", name=n) for n in ("a", "b")]
    picked = _own_running_session(rows, where="")
    assert picked["tmux_session"] in {"a", "b"}


def test_no_qualifying_session_raises_a_clear_409() -> None:
    rows = [
        _row(mine=False, state="running"),
        _row(mine=True, state="stopped"),
    ]
    with pytest.raises(HTTPException) as excinfo:
        _own_running_session(rows, where=" in this project")

    assert excinfo.value.status_code == 409
    assert "in this project" in excinfo.value.detail


def test_an_empty_session_list_also_raises_rather_than_indexing() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _own_running_session([], where="")
    assert excinfo.value.status_code == 409
