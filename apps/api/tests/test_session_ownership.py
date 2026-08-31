"""In-flight sign-in sessions are looked up by id alone, with no other check.

Every one of these ids is a high-entropy random token, but the lookup is a
plain process-global dict shared by every user and every org on the instance —
`login._sessions`, `github._sessions`, `mcp_login._sessions`. Nothing about
the id itself proves who is allowed to poll, advance, or complete a session,
and each of these flows ends by writing a credential under the session's own
`org_id`. Whoever's request reaches the handler decides whose credential gets
written and which org's account gets touched next — so ownership has to be
checked explicitly, not inferred from "they knew the id."

These tests exercise the checks directly, without SSH or Docker: a mismatch
must be refused before any of that work starts, and must read exactly like an
unknown id — never a different message that would let a caller tell "wrong
id" apart from "someone else's session".
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi import HTTPException

from moonphase import github, login, mcp_login
from moonphase.auth import Principal
from moonphase.harness import HarnessKind
from moonphase.routers import mcp_oauth, profile


def _principal(user_id: str) -> Principal:
    return Principal(user_id=user_id, email=None, claims={"sub": user_id})


# --- the harness account sign-in relay ---------------------------------------


@pytest.fixture
def login_session():
    session = login.LoginSession(
        id=f"{login._PROCESS_EPOCH}.{uuid.uuid4().hex}",
        org_id="alice-org",
        harness_kind=str(HarnessKind.CLAUDE_CODE),
        server_id="server-1",
        container=f"{login.CONTAINER_PREFIX}test",
        user_id="alice",
    )
    login._sessions[session.id] = session
    yield session
    login.forget(session.id)


def test_the_owner_can_look_up_their_own_login_session(login_session) -> None:
    found = profile._get_login_session(login_session.id, _principal("alice"))
    assert found is login_session


def test_someone_elses_login_session_is_not_found(login_session) -> None:
    with pytest.raises(HTTPException) as caught:
        profile._get_login_session(login_session.id, _principal("mallory"))
    assert caught.value.status_code == 404
    assert caught.value.detail == "No such sign-in."


def test_polling_someone_elses_login_session_is_refused_before_any_ssh_work(
    login_session, monkeypatch
) -> None:
    """The check has to happen before load_server_target/ssh.pool.get — an
    unrelated caller must never reach code that mutates the shared session."""

    async def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not attempt SSH for someone else's session")

    monkeypatch.setattr(profile.runtime, "load_server_target", must_not_run)
    login_session.state = "verifying"

    import asyncio

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            profile.poll_harness_login(login_session.id, principal=_principal("mallory"))
        )
    assert caught.value.status_code == 404


# --- github device flow -------------------------------------------------------


@pytest.fixture
def github_session():
    flow = github.DeviceFlow(
        device_code="d",
        user_code="ABCD-EFGH",
        verification_uri="https://github.com/login/device",
        interval=5,
        expires_at=time.monotonic() + 900,
        scopes="repo",
    )
    session = github.DeviceSession(
        id=uuid.uuid4().hex, org_id="alice-org", flow=flow, user_id="alice"
    )
    github.put_session(session)
    yield session
    github.drop_session(session.id)


def test_the_owner_can_look_up_their_own_github_session(github_session) -> None:
    found = profile._get_github_session(github_session.id, _principal("alice"))
    assert found is github_session


def test_someone_elses_github_session_is_not_found(github_session) -> None:
    with pytest.raises(HTTPException) as caught:
        profile._get_github_session(github_session.id, _principal("mallory"))
    assert caught.value.status_code == 404
    assert caught.value.detail == "No such GitHub sign-in."


async def test_polling_someone_elses_github_session_never_calls_github(
    github_session, monkeypatch
) -> None:
    async def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not poll GitHub for someone else's session")

    monkeypatch.setattr(profile.github, "poll_device_flow", must_not_run)

    with pytest.raises(HTTPException) as caught:
        await profile.poll_github_device(github_session.id, principal=_principal("mallory"))
    assert caught.value.status_code == 404


# --- mcp oauth relay -----------------------------------------------------------


@pytest.fixture
def mcp_session():
    session = mcp_login.McpLoginSession(
        id=uuid.uuid4().hex,
        org_id="alice-org",
        project_id=str(uuid.uuid4()),
        session_name="main",
        server_name="some-mcp",
        home="/home/dev",
        container="mp-project-1",
        tmux_session="mp-main",
        user_id="alice",
    )
    mcp_login._sessions[session.id] = session
    yield session
    mcp_login._sessions.pop(session.id, None)


def test_the_owner_can_look_up_their_own_mcp_session(mcp_session) -> None:
    found = mcp_oauth._get_mcp_session(
        mcp_session.id, uuid.UUID(mcp_session.project_id), _principal("alice")
    )
    assert found is mcp_session


def test_someone_elses_mcp_session_is_not_found(mcp_session) -> None:
    with pytest.raises(HTTPException) as caught:
        mcp_oauth._get_mcp_session(
            mcp_session.id, uuid.UUID(mcp_session.project_id), _principal("mallory")
        )
    assert caught.value.status_code == 404
    assert caught.value.detail == "No such connection attempt."


def test_a_different_project_id_in_the_url_is_not_found(mcp_session) -> None:
    """The flow's own project_id is fixed at /start time, from the same
    place its container/tmux session come from. A caller who still owns the
    session but names a *different* project in the URL must be refused
    exactly like an unknown session — otherwise the access check (on the
    URL's project) and the resource actually driven (the flow's own
    project) could diverge."""
    other_project = uuid.uuid4()
    assert str(other_project) != mcp_session.project_id
    with pytest.raises(HTTPException) as caught:
        mcp_oauth._get_mcp_session(mcp_session.id, other_project, _principal("alice"))
    assert caught.value.status_code == 404
    assert caught.value.detail == "No such connection attempt."


# --- an unknown id and someone else's session must be indistinguishable ------


def test_a_wrong_id_and_a_wrong_owner_read_identically() -> None:
    alice = _principal("alice")

    with pytest.raises(HTTPException) as unknown:
        profile._get_login_session("not-a-real-session", alice)
    with pytest.raises(HTTPException) as wrong_owner:
        session = login.LoginSession(
            id=f"{login._PROCESS_EPOCH}.{uuid.uuid4().hex}",
            org_id="bob-org",
            harness_kind=str(HarnessKind.CLAUDE_CODE),
            server_id="server-1",
            container=f"{login.CONTAINER_PREFIX}test2",
            user_id="bob",
        )
        login._sessions[session.id] = session
        try:
            profile._get_login_session(session.id, alice)
        finally:
            login.forget(session.id)

    assert unknown.value.status_code == wrong_owner.value.status_code
    assert unknown.value.detail == wrong_owner.value.detail
