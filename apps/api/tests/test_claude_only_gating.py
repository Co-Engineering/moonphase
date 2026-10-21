"""Claude Code's own paths, kept to Claude Code once there is a second harness
with accounts of its own.

Several things are Claude Code underneath however generically they are reached:
the MCP OAuth relay runs `claude mcp login`, the Dashboard's plan limits read
Claude's credentials file and the status-line reading Claude Code leaves in a
session's HOME, the usage page's billing framing is how Claude is paid for,
and the account cache cleared on a switch is `~/.claude.json`. While every
session was a Claude one, "any running session" and "any account" meant the
same thing as "a Claude one". With Antigravity sessions and accounts about,
each of these would pick one up and misread it, so each is pinned here —
without a database, by monkeypatching the queries and the server around the
route, as test_harness_accounts.py does.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from moonphase import mcp_login, plan_usage, queries
from moonphase.harness import SessionSpace
from moonphase.routers import mcp_oauth, projects
from moonphase.routers import usage as usage_router
from moonphase.schemas import AccountChoiceIn, McpOAuthStartIn

PRINCIPAL = SimpleNamespace(user_id=uuid4(), claims={}, email="a@b.c")


@asynccontextmanager
async def _no_db(*a):
    yield None


def _session(name: str, *, harness: str | None, mine: bool = True, state: str = "running",
             project_id: Any = None, home: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "is_mine": mine,
        "state": state,
        "tmux_session": name,
        "project_id": project_id or uuid4(),
        "home_dir": home or f"/home/dev/sessions/{name}",
        "harness_credential_id": None,
    }
    if harness is not None:
        row["harness"] = harness
    return row


# --- MCP OAuth: which session carries the relay -------------------------------


def test_an_antigravity_session_is_never_picked_to_relay_through() -> None:
    """The first of your running sessions used to be the one, whatever it ran:
    `claude mcp login` started inside an Antigravity session, and failed,
    while a Claude Code session that would have worked sat beside it."""
    rows = [
        _session("agy", harness="antigravity"),
        _session("claude", harness="claude_code"),
    ]
    assert mcp_oauth._own_running_session(rows, where="")["tmux_session"] == "claude"


def test_with_only_other_harnesses_running_the_409_says_it_needs_claude_code() -> None:
    rows = [_session("agy", harness="antigravity")]
    with pytest.raises(HTTPException) as err:
        mcp_oauth._own_running_session(rows, where=" in this project")
    assert err.value.status_code == 409
    assert "Claude Code" in err.value.detail
    assert "in this project" in err.value.detail


def test_a_row_that_does_not_say_its_harness_is_taken_as_claude_code() -> None:
    """Every row from the database names one (the column is not null). A row
    built by hand without it, as older tests build them, is Claude Code — what
    a project runs when it names none."""
    rows = [_session("legacy", harness=None)]
    assert mcp_oauth._own_running_session(rows, where="")["tmux_session"] == "legacy"


@pytest.fixture
def relay(monkeypatch):
    """`_start`'s surroundings, recording how far a request got."""
    w = SimpleNamespace(harness="antigravity", reached=[], picked=None)

    async def load_project_context(claims, project_id, *, require=None):
        return SimpleNamespace(
            project={"id": project_id}, target="t", container="mp-x", harness=w.harness
        )

    async def load_session_space(claims, project_id, session_name):
        return SessionSpace(home=f"/home/dev/sessions/{session_name}"), {"is_mine": True}

    async def load_session_profile(*a, **kw):
        w.reached.append("profile")
        return SimpleNamespace(has_harness_auth=True)

    async def personal_org_id(conn):
        return uuid4()

    async def pool_get(target):
        return object()

    async def ensure_session(*a, **kw):
        w.reached.append("ensure_session")

    async def start(conn, **kw):
        w.reached.append("mcp_login")
        return mcp_login.McpLoginSession(
            id="m1", org_id=kw["org_id"], project_id=kw["project_id"],
            session_name=kw["session_name"], server_name=kw["server_name"],
            home=kw["space"].home, container=kw["container"], tmux_session="t",
        )

    monkeypatch.setattr(mcp_oauth.runtime, "load_project_context", load_project_context)
    monkeypatch.setattr(mcp_oauth.runtime, "load_session_space", load_session_space)
    monkeypatch.setattr(mcp_oauth.runtime, "load_session_profile", load_session_profile)
    monkeypatch.setattr(mcp_oauth.queries, "personal_org_id", personal_org_id)
    monkeypatch.setattr(mcp_oauth, "user_session", _no_db)
    monkeypatch.setattr(mcp_oauth.ssh.pool, "get", pool_get)
    monkeypatch.setattr(mcp_oauth.sessions, "ensure_session", ensure_session)
    monkeypatch.setattr(mcp_oauth.mcp_login, "start", start)
    return w


@pytest.mark.asyncio
async def test_connecting_through_an_antigravity_session_is_refused_before_it_is_touched(
    relay,
) -> None:
    """`_check` already refused a non-Claude project; `_start` did not, and
    went on to re-apply the session's profile and run `claude mcp login` in
    a HOME with no Claude Code configuration in it."""
    with pytest.raises(HTTPException) as err:
        await mcp_oauth._start(uuid4(), "agy", "linear", PRINCIPAL)

    assert err.value.status_code == 409
    assert "only available for Claude Code" in err.value.detail
    assert relay.reached == []


@pytest.mark.asyncio
async def test_a_claude_code_session_still_relays(relay) -> None:
    relay.harness = "claude_code"

    out = await mcp_oauth._start(uuid4(), "claude", "linear", PRINCIPAL)

    assert out.session_id == "m1"
    assert relay.reached == ["profile", "ensure_session", "mcp_login"]


@pytest.mark.asyncio
async def test_connecting_from_settings_relays_through_a_claude_code_session(
    monkeypatch,
) -> None:
    """Settings has no project in hand and takes any of your sessions — now
    any of your Claude Code ones, even when an Antigravity one comes first."""
    claude_project = uuid4()
    rows = [
        _session("agy", harness="antigravity"),
        _session("claude", harness="claude_code", project_id=claude_project),
    ]
    picked: list[tuple[Any, str]] = []

    async def list_all_sessions(conn):
        return rows

    async def start(project_id, session_name, server_name, principal):
        picked.append((project_id, session_name))
        return "started"

    monkeypatch.setattr(mcp_oauth, "user_session", _no_db)
    monkeypatch.setattr(mcp_oauth.queries, "list_all_sessions", list_all_sessions)
    monkeypatch.setattr(mcp_oauth, "_start", start)

    await mcp_oauth.start_mcp_oauth_for_org(McpOAuthStartIn(server_name="linear"), PRINCIPAL)

    assert picked == [(claude_project, "claude")]


# --- the Dashboard's plan limits ----------------------------------------------

NOW = 1_800_000_000.0


def _creds(token: str, expires_in: float) -> str:
    return json.dumps(
        {"claudeAiOauth": {"accessToken": token, "expiresAt": int((NOW + expires_in) * 1000)}}
    )


@pytest.fixture
def dashboard(monkeypatch):
    plan_usage._cache.clear()
    plan_usage._backoff.clear()
    org = uuid4()
    personal = uuid4()
    w = SimpleNamespace(
        org=org,
        personal=personal,
        accounts=[
            {"id": personal, "label": "Personal", "auth_mode": "oauth", "is_default": True},
        ],
        # Expired, so the route goes looking in running sessions for a token.
        materials={personal: {"id": personal, "oauth_blob": _creds("stored", -3600)}},
        sessions=[],
        projects=[],
        files={},
        statusline={},
        looked_in=[],
        fetched_with=[],
    )

    async def personal_org_id(conn):
        return w.org

    async def list_all_sessions(conn):
        return w.sessions

    async def list_projects(conn, server_id=None):
        return w.projects

    async def list_accounts(conn, org_id, harness):
        assert harness == "claude_code"
        return w.accounts

    async def resolve(conn, *, org_id, project_id, harness, account_id=None):
        return w.materials.get(account_id)

    async def load_project_context(claims, project_id, *, require):
        return SimpleNamespace(target="t", container="mp-x")

    async def pool_get(target):
        return object()

    async def read_file(conn, container, path):
        w.looked_in.append(path)
        return w.files.get(path)

    async def exec_capture(conn, container, argv, **kw):
        w.looked_in.append(argv[-1])
        entry = w.statusline.get(argv[-1])
        if entry is None:
            return SimpleNamespace(ok=True, stdout="")
        return SimpleNamespace(ok=True, stdout=f"{int(entry[0])}\n{json.dumps(entry[1])}")

    async def fetch(token, client=None):
        w.fetched_with.append(token)
        return plan_usage.parse_windows(
            {"five_hour": {"utilization": 10.0, "resets_at": "2027-01-15T12:00:00Z"}}
        )

    monkeypatch.setattr(usage_router, "user_session", _no_db)
    monkeypatch.setattr(usage_router, "service_session", _no_db)
    monkeypatch.setattr(usage_router.queries, "personal_org_id", personal_org_id)
    monkeypatch.setattr(usage_router.queries, "list_all_sessions", list_all_sessions)
    monkeypatch.setattr(usage_router.queries, "list_projects", list_projects)
    monkeypatch.setattr(usage_router.queries, "list_harness_accounts_privileged", list_accounts)
    monkeypatch.setattr(usage_router.queries, "resolve_harness_credential_privileged", resolve)
    monkeypatch.setattr(usage_router.runtime, "load_project_context", load_project_context)
    monkeypatch.setattr(usage_router.ssh.pool, "get", pool_get)
    monkeypatch.setattr(usage_router.profile, "read_file", read_file)
    monkeypatch.setattr(usage_router.docker_remote, "exec_capture", exec_capture)
    monkeypatch.setattr(usage_router.plan_usage, "fetch", fetch)
    monkeypatch.setattr(usage_router.time, "time", lambda: NOW)
    monkeypatch.setattr(plan_usage.time, "time", lambda: NOW)
    return w


def _running(w, name: str, harness: str) -> str:
    project = {"id": uuid4(), "org_id": w.org, "harness_credential_id": None}
    w.projects.append(project)
    w.sessions.append(_session(name, harness=harness, project_id=project["id"]))
    return f"/home/dev/sessions/{name}"


@pytest.mark.asyncio
async def test_an_antigravity_session_is_not_counted_on_the_default_claude_account(
    dashboard,
) -> None:
    """An Antigravity session that names no account of its own fell through
    to the default — Claude's default, here — and was searched for Claude's
    credentials file and status-line reading. Whatever it held would have
    been reported as the Claude account's."""
    home = _running(dashboard, "agy", "antigravity")
    dashboard.files[f"{home}/.claude/.credentials.json"] = _creds("not-claudes", 3600)
    dashboard.statusline[f"{home}/.moonphase/usage.json"] = (
        NOW - 30,
        {"rate_limits": {"five_hour": {"used_percentage": 99, "resets_at": NOW + 60}}},
    )

    [personal] = await usage_router.read_account_usage(PRINCIPAL)

    assert not any(path.startswith(home) for path in dashboard.looked_in)
    assert "not-claudes" not in dashboard.fetched_with
    assert personal.source != "statusline"
    assert personal.status == "expired"


@pytest.mark.asyncio
async def test_a_claude_code_session_on_the_default_account_is_still_read(dashboard) -> None:
    home = _running(dashboard, "claude", "claude_code")
    dashboard.files[f"{home}/.claude/.credentials.json"] = _creds("live", 3600)

    [personal] = await usage_router.read_account_usage(PRINCIPAL)

    assert personal.status == "ok" and personal.source == "session"
    assert dashboard.fetched_with == ["live"]


# --- how Claude is paid for ---------------------------------------------------


class _Recording:
    def __init__(self) -> None:
        self.sql = ""
        self.params: dict[str, Any] = {}

    async def execute(self, statement, params):
        self.sql = str(statement)
        self.params = params
        return SimpleNamespace(first=lambda: ("api_key",))


@pytest.mark.asyncio
async def test_the_billing_framing_is_read_from_one_harnesss_accounts() -> None:
    """Across every harness, the newest default wins — so a Gemini API key
    added for Antigravity would have turned a Claude subscription's usage
    page into a bill."""
    conn = _Recording()

    assert await queries.first_harness_credential_privileged(conn, "u") == "api_key"

    assert "hc.harness = cast(:h as harness_kind)" in conn.sql
    assert conn.params["h"] == "claude_code"


@pytest.mark.asyncio
async def test_the_usage_page_asks_how_claude_code_is_paid_for(monkeypatch) -> None:
    asked: list[dict[str, Any]] = []

    async def nothing(*a, **kw):
        return []

    async def no_limits(conn):
        return None

    async def first_credential(conn, user_id, **kw):
        asked.append(kw)
        return "oauth"

    monkeypatch.setattr(usage_router, "user_session", _no_db)
    monkeypatch.setattr(usage_router, "service_session", _no_db)
    for name in ("list_model_prices", "usage_since", "usage_by_project", "usage_buckets",
                 "usage_times"):
        monkeypatch.setattr(usage_router.queries, name, nothing)
    monkeypatch.setattr(usage_router.queries, "get_usage_limits", no_limits)
    monkeypatch.setattr(
        usage_router.queries, "first_harness_credential_privileged", first_credential
    )

    out = await usage_router.read_usage(hours=24, principal=PRINCIPAL)

    assert out.billing == "oauth"
    assert asked == [{"harness": "claude_code"}]


# --- switching a session's account --------------------------------------------


@pytest.fixture
def switch(monkeypatch):
    w = SimpleNamespace(harness="antigravity", ran=[], restarted=[])

    async def load_project_context(claims, project_id, *, require=None):
        return SimpleNamespace(
            project={"id": project_id, "org_id": "org"}, target="t",
            container="mp-x", harness=w.harness,
        )

    async def get_session(conn, project_id, name):
        return {"is_mine": True, "home_dir": f"/home/dev/sessions/{name}"}

    async def set_account(conn, project_id, name, account_id):
        return None

    async def pool_get(target):
        return object()

    async def exec_capture(conn, container, argv, **kw):
        w.ran.append(argv)
        return SimpleNamespace(ok=True)

    async def start_session(project_id, payload, principal):
        w.restarted.append(payload)
        return "restarted"

    monkeypatch.setattr(projects.runtime, "load_project_context", load_project_context)
    monkeypatch.setattr(projects.queries, "get_session", get_session)
    monkeypatch.setattr(projects.queries, "set_session_harness_account", set_account)
    monkeypatch.setattr(projects.ssh.pool, "get", pool_get)
    monkeypatch.setattr(projects.docker_remote, "exec_capture", exec_capture)
    monkeypatch.setattr(projects, "start_session", start_session)
    monkeypatch.setattr(projects, "user_session", _no_db)
    monkeypatch.setattr(projects, "service_session", _no_db)
    return w


@pytest.mark.asyncio
async def test_switching_an_antigravity_session_leaves_claudes_cache_alone(switch) -> None:
    """`~/.claude.json` is Claude Code's; in an Antigravity session the edit
    was an SSH round trip to a file that is not there. The restart that
    rewrites agy's own token file still happens."""
    await projects.switch_session_account(
        uuid4(), "s", AccountChoiceIn(account_id=None), PRINCIPAL
    )

    assert switch.ran == []
    assert len(switch.restarted) == 1


@pytest.mark.asyncio
async def test_switching_a_claude_code_session_still_forgets_the_old_account(switch) -> None:
    switch.harness = "claude_code"

    await projects.switch_session_account(
        uuid4(), "s", AccountChoiceIn(account_id=None), PRINCIPAL
    )

    assert switch.ran == [["sh", "-c", projects._FORGET_SIGNED_IN_ACCOUNT]]
    assert len(switch.restarted) == 1
