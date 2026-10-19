"""Choosing between several Claude accounts, without a database.

The database half — which account a session resolves to — is in
test_harness_accounts_db.py. This covers what happens around it: where a
fresh sign-in is stored, who may switch which session, and that switching
away from a subscription does not leave its login behind.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from moonphase.harness import get as get_harness
from moonphase.routers import profile as profile_router
from moonphase.routers import projects
from moonphase.runtime import Forbidden
from moonphase.schemas import AccountChoiceIn

# --- the credentials file ---------------------------------------------------

CREDS = json.dumps(
    {
        "claudeAiOauth": {"accessToken": "personal"},
        "mcpOAuth": {"linear|abc": {"access_token": "x"}},
    }
)


def test_switching_to_an_api_key_drops_the_old_subscription_login() -> None:
    merged = get_harness("claude_code").merge_into_credentials_file(
        CREDS, {}, keep_account_login=False
    )
    doc = json.loads(merged or "{}")
    assert "claudeAiOauth" not in doc
    assert doc["mcpOAuth"] == {"linear|abc": {"access_token": "x"}}


def test_a_subscription_login_is_kept_when_it_is_what_the_session_runs_on() -> None:
    merged = get_harness("claude_code").merge_into_credentials_file(CREDS, {})
    assert json.loads(merged or "{}")["claudeAiOauth"] == {"accessToken": "personal"}


# --- where a sign-in goes ---------------------------------------------------


@pytest.fixture
def stored(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def replace(conn, **kw):
        calls.append(("replace", kw))
        return {"id": kw["account_id"]}

    async def add(conn, **kw):
        calls.append(("add", kw))
        return {}

    async def upsert(conn, **kw):
        calls.append(("upsert", kw))
        return {}

    @asynccontextmanager
    async def service_session():
        yield None

    monkeypatch.setattr(
        profile_router.queries, "replace_harness_account_material_privileged", replace
    )
    monkeypatch.setattr(profile_router.queries, "add_harness_account_privileged", add)
    monkeypatch.setattr(
        profile_router.queries, "upsert_harness_credential_privileged", upsert
    )
    monkeypatch.setattr(profile_router, "service_session", service_session)
    return calls


PRINCIPAL = SimpleNamespace(user_id="u1", claims={}, email="a@b.c")


async def _store(**kw) -> None:
    await profile_router._store_account(
        PRINCIPAL, org_id=uuid4(), harness="claude_code", auth_mode="oauth",
        oauth_blob="{}", **kw,
    )


@pytest.mark.asyncio
async def test_signing_in_again_to_an_account_replaces_only_that_one(stored) -> None:
    account = uuid4()
    await _store(account_id=account, label=None)
    assert [c[0] for c in stored] == ["replace"]
    assert stored[0][1]["account_id"] == account


@pytest.mark.asyncio
async def test_a_name_adds_another_account(stored) -> None:
    await _store(account_id=None, label="Client X")
    assert [c[0] for c in stored] == ["add"]
    assert stored[0][1]["label"] == "Client X"


@pytest.mark.asyncio
async def test_neither_replaces_the_default_as_before(stored) -> None:
    await _store(account_id=None, label=None)
    assert [c[0] for c in stored] == ["upsert"]


# --- switching a session ----------------------------------------------------


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(
        session={"is_mine": True, "home_dir": "/home/dev/sessions/s"},
        own_accounts=set(),
        set_to=[],
        restarted=[],
    )

    async def load_project_context(claims, project_id, *, require=None):
        return SimpleNamespace(
            project={"id": project_id, "org_id": "org"}, target="t",
            container="mp-x", harness="claude_code",
        )

    async def get_session(conn, project_id, name):
        return w.session

    async def own_account(principal, account_id, harness):
        if account_id not in w.own_accounts:
            raise HTTPException(status_code=404, detail="No such account.")

    async def set_account(conn, project_id, name, account_id):
        w.set_to.append(account_id)

    async def pool_get(target):
        return object()

    async def exec_capture(*a, **kw):
        return SimpleNamespace(ok=True)

    async def start_session(project_id, payload, principal):
        w.restarted.append(payload)
        return "restarted"

    @asynccontextmanager
    async def session_ctx(*a):
        yield None

    monkeypatch.setattr(projects.runtime, "load_project_context", load_project_context)
    monkeypatch.setattr(projects.queries, "get_session", get_session)
    monkeypatch.setattr(projects.queries, "set_session_harness_account", set_account)
    monkeypatch.setattr(projects, "_own_account", own_account)
    monkeypatch.setattr(projects.ssh.pool, "get", pool_get)
    monkeypatch.setattr(projects.docker_remote, "exec_capture", exec_capture)
    monkeypatch.setattr(projects, "start_session", start_session)
    monkeypatch.setattr(projects, "user_session", session_ctx)
    monkeypatch.setattr(projects, "service_session", session_ctx)
    return w


@pytest.mark.asyncio
async def test_switching_restarts_the_session_and_resumes_its_conversation(world) -> None:
    work = uuid4()
    world.own_accounts.add(work)

    await projects.switch_session_account(
        uuid4(), "s", AccountChoiceIn(account_id=work), PRINCIPAL
    )

    assert world.set_to == [work]
    [restart] = world.restarted
    assert restart.restart and restart.resume and restart.session == "s"


@pytest.mark.asyncio
async def test_someone_elses_session_cannot_be_switched(world) -> None:
    world.session = {"is_mine": False, "home_dir": "/x"}
    with pytest.raises(Forbidden):
        await projects.switch_session_account(
            uuid4(), "s", AccountChoiceIn(account_id=None), PRINCIPAL
        )
    assert world.set_to == [] and world.restarted == []


@pytest.mark.asyncio
async def test_an_account_that_is_not_yours_is_refused_before_anything_changes(world) -> None:
    with pytest.raises(HTTPException) as err:
        await projects.switch_session_account(
            uuid4(), "s", AccountChoiceIn(account_id=uuid4()), PRINCIPAL
        )
    assert err.value.status_code == 404
    assert world.set_to == [] and world.restarted == []


@pytest.mark.asyncio
async def test_back_to_the_default_needs_no_account(world) -> None:
    await projects.switch_session_account(
        uuid4(), "s", AccountChoiceIn(account_id=None), PRINCIPAL
    )
    assert world.set_to == [None]
