"""advance() must never call a stale or unrelated credential entry a success
for *this* connection attempt, and a pane that plainly shows failure must
always win over whatever happens to be sitting in the credentials file.

Both fixes exist because a server the org already connected once leaves its
old entry in the credentials file; pasting anything on a reconnect used to
deterministically read as success regardless of whether the real OAuth
exchange happened at all.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from moonphase import mcp_login
from moonphase.mcp_login import McpLoginSession


@dataclass
class _FakeResult:
    stdout: str
    ok: bool = True


def _joined(command: list[str]) -> str:
    return " ".join(command)


def _session(**overrides: object) -> McpLoginSession:
    defaults: dict[str, object] = dict(
        id="s1",
        org_id="o1",
        project_id="p1",
        session_name="main",
        server_name="sentry",
        home="/home/dev",
        container="c1",
        tmux_session="moonphase-mcp-login-abc",
        user_id="alice",
        state="verifying",
    )
    defaults.update(overrides)
    return McpLoginSession(**defaults)  # type: ignore[arg-type]


def test_an_unchanged_credential_entry_is_not_harvested(monkeypatch) -> None:
    """The org already connected this server once and nothing about *this*
    attempt changed the file. Pasting anything must not mark it complete."""
    entry_dict = {"sentry|abc123": {"accessToken": "old-token"}}
    entry = json.dumps(entry_dict)

    async def fake_exec_capture(conn, container, command, **kwargs):
        if "cat " in _joined(command):
            return _FakeResult(json.dumps({"mcpOAuth": entry_dict}))
        return _FakeResult("")  # tmux capture-pane / kill-session

    monkeypatch.setattr(mcp_login.docker_remote, "exec_capture", fake_exec_capture)

    session = _session(existing_credential=entry)
    result = asyncio.run(mcp_login.advance(conn=None, session=session))

    assert result.state == "verifying"
    assert result.credential_entry is None


def test_a_genuinely_new_credential_entry_is_harvested(monkeypatch) -> None:
    old_entry = json.dumps({"sentry|abc123": {"accessToken": "old-token"}})
    new_entry_dict = {"sentry|def456": {"accessToken": "new-token"}}

    async def fake_exec_capture(conn, container, command, **kwargs):
        if "cat " in _joined(command):
            return _FakeResult(json.dumps({"mcpOAuth": new_entry_dict}))
        return _FakeResult("")

    monkeypatch.setattr(mcp_login.docker_remote, "exec_capture", fake_exec_capture)

    session = _session(existing_credential=old_entry)
    result = asyncio.run(mcp_login.advance(conn=None, session=session))

    assert result.state == "complete"
    assert result.credential_entry == json.dumps(new_entry_dict)


def test_a_visible_failure_wins_even_if_a_credential_entry_is_present(monkeypatch) -> None:
    """A pane that plainly says this attempt failed must never be
    overridden by an entry sitting in the file, new or not."""
    new_entry_dict = {"sentry|def456": {"accessToken": "new-token"}}

    async def fake_exec_capture(conn, container, command, **kwargs):
        joined = _joined(command)
        if "capture-pane" in joined:
            return _FakeResult(
                "Couldn't complete authentication: OAuth state mismatch"
            )
        if "cat " in joined:
            return _FakeResult(json.dumps({"mcpOAuth": new_entry_dict}))
        return _FakeResult("")

    monkeypatch.setattr(mcp_login.docker_remote, "exec_capture", fake_exec_capture)

    session = _session(existing_credential=None)
    result = asyncio.run(mcp_login.advance(conn=None, session=session))

    assert result.state == "error"
    assert result.credential_entry is None
