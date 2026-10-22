"""The monitor names the agent a notification is about.

The wording itself is pinned in test_activity.py. This is the wiring: the
monitor knows each session's harness from its project, and a notification
about an Antigravity session that said "Claude stopped working" would send
someone to look for the wrong thing. No database: the monitor's writes go to
a stand-in, and what it would push is captured instead of sent.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from moonphase import activity
from moonphase import monitor as monitor_module
from moonphase.activity import ActivityState


class _Connection:
    async def execute(self, *_: Any, **__: Any) -> None:
        return None


@contextlib.asynccontextmanager
async def _service_session():
    yield _Connection()


async def _record_activity(_conn: Any, **_: Any) -> None:
    return None


def _row(harness: str) -> dict[str, Any]:
    return {
        "id": "p1", "org_id": "o1", "name": "landing-page", "harness": harness,
        "session_id": "s1", "tmux_session": "moonphase", "user_id": "u1",
        "activity": "working", "pane_digest": "before", "notified_state": None,
    }


@pytest.mark.parametrize(
    ("harness", "state", "body"),
    [
        ("antigravity", ActivityState.IDLE, "Antigravity stopped working."),
        ("antigravity", ActivityState.AWAITING_INPUT, "Antigravity is waiting for an answer."),
        ("opencode", ActivityState.IDLE, "OpenCode stopped working."),
        # Unchanged, word for word.
        ("claude_code", ActivityState.IDLE, "Claude stopped working."),
        ("claude_code", ActivityState.AWAITING_INPUT, "Claude is waiting for an answer."),
    ],
)
async def test_a_notification_names_the_sessions_own_agent(
    monkeypatch: pytest.MonkeyPatch, harness: str, state: ActivityState, body: str
) -> None:
    monkeypatch.setattr(monitor_module, "service_session", _service_session)
    monkeypatch.setattr(monitor_module, "_record_activity", _record_activity)
    pushed: list[tuple[str, str]] = []

    async def notify(_row: Any, title: str, text: str, **_: Any) -> None:
        pushed.append((title, text))

    monitor = monitor_module.SessionMonitor()
    monitor._notify = notify  # type: ignore[method-assign]

    # No detail: the question could not be read off the screen, so the body
    # falls back to naming who is waiting.
    await monitor._settle(_row(harness), activity.Snapshot(state=state, digest="after"))

    assert len(pushed) == 1
    assert pushed[0][1] == body
