"""Each account's limits, as Claude Code's `/usage` shows them.

Read from the provider with the account's own sign-in. The parts worth
pinning: what the answer becomes, which token is used — never refreshed,
borrowed from a running session once the stored one has expired — and what
the page is told when it cannot be read.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from moonphase import plan_usage
from moonphase.routers import usage as usage_router

NOW = 1_800_000_000.0


def creds(token: str, expires_in_s: float) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": "never-used",
                "expiresAt": int((NOW + expires_in_s) * 1000),
            }
        }
    )


SAMPLE = {
    "five_hour": {"utilization": 42.0, "resets_at": "2027-01-15T14:00:00Z"},
    "seven_day": {"utilization": 13, "resets_at": "2027-01-19T08:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 5.5, "resets_at": None},
    "extra_thing": {"utilization": 250, "resets_at": "garbage"},
}


def test_the_answer_becomes_windows_in_a_sensible_order() -> None:
    windows = plan_usage.parse_windows(SAMPLE)
    assert [(w.key, w.label) for w in windows] == [
        ("five_hour", "Current session"),
        ("seven_day", "Current week (all models)"),
        ("seven_day_sonnet", "Current week (Sonnet)"),
        ("extra_thing", "Extra thing"),
    ]
    assert windows[0].percent == 42.0
    assert windows[0].resets_at is not None and windows[0].resets_at.hour == 14
    # Clamped, and a reset time that is not a time is left out rather than guessed.
    assert windows[3].percent == 100.0 and windows[3].resets_at is None


def test_an_unexpected_answer_is_no_windows_rather_than_an_error() -> None:
    assert plan_usage.parse_windows(["not", "a", "dict"]) == []
    assert plan_usage.parse_windows({"five_hour": {"utilization": None}}) == []


def test_the_token_is_read_from_a_credentials_file() -> None:
    token = plan_usage.token_from_credentials_file(creds("abc", 3600))
    assert token is not None and token.token == "abc" and token.valid(NOW)
    assert plan_usage.token_from_credentials_file("not json") is None
    assert plan_usage.token_from_credentials_file(json.dumps({"mcpOAuth": {}})) is None


def test_the_freshest_valid_token_wins_and_expired_ones_never_do() -> None:
    expired = plan_usage.token_from_credentials_file(creds("old", -10))
    short = plan_usage.token_from_credentials_file(creds("short", 600))
    long = plan_usage.token_from_credentials_file(creds("long", 7200))
    assert plan_usage.freshest([expired, short, long, None], NOW).token == "long"  # type: ignore[union-attr]
    assert plan_usage.freshest([expired, None], NOW) is None


def _client(status: int, body: object) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["anthropic-beta"] == plan_usage.OAUTH_BETA
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fetch_returns_the_windows() -> None:
    async with _client(200, SAMPLE) as client:
        windows = await plan_usage.fetch("tok", client)
    assert windows[0].percent == 42.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "expired"), (403, "unsupported"), (429, "rate_limited"), (500, "error")],
)
async def test_fetch_says_why_it_could_not(status: int, reason: str) -> None:
    async with _client(status, {}) as client:
        with pytest.raises(plan_usage.UsageUnavailable) as err:
            await plan_usage.fetch("tok", client)
    assert err.value.reason == reason


# --- the route ---------------------------------------------------------------


@pytest.fixture
def world(monkeypatch):
    plan_usage._cache.clear()
    plan_usage._backoff.clear()
    org = uuid4()
    personal, work, key = uuid4(), uuid4(), uuid4()
    w = SimpleNamespace(
        org=org,
        accounts=[
            {"id": personal, "label": "Personal", "auth_mode": "oauth", "is_default": True},
            {"id": work, "label": "Work", "auth_mode": "oauth", "is_default": False},
            {"id": key, "label": "Client", "auth_mode": "api_key", "is_default": False},
        ],
        materials={
            personal: {"id": personal, "oauth_blob": creds("personal-stored", 3600)},
            work: {"id": work, "oauth_blob": creds("work-stored", -3600)},
            key: {"id": key, "api_key": "sk"},
        },
        sessions=[],
        projects=[],
        container_files={},
        fetched_with=[],
        refuse=None,
        refuse_token={},
        ids=SimpleNamespace(personal=personal, work=work, key=key),
    )

    @asynccontextmanager
    async def session_ctx(*a):
        yield None

    async def personal_org_id(conn):
        return w.org

    async def list_all_sessions(conn):
        return w.sessions

    async def list_projects(conn, server_id=None):
        return w.projects

    async def list_accounts(conn, org_id, harness):
        return w.accounts

    async def resolve(conn, *, org_id, project_id, harness, account_id=None):
        return w.materials.get(account_id)

    async def load_project_context(claims, project_id, *, require):
        return SimpleNamespace(target="t", container="mp-x")

    async def pool_get(target):
        return object()

    async def read_file(conn, container, path):
        return w.container_files.get(path)

    async def fetch(token, client=None):
        w.fetched_with.append(token)
        if token in w.refuse_token:
            raise w.refuse_token[token]
        if w.refuse is not None:
            raise w.refuse
        return plan_usage.parse_windows(SAMPLE)

    monkeypatch.setattr(usage_router, "user_session", session_ctx)
    monkeypatch.setattr(usage_router, "service_session", session_ctx)
    monkeypatch.setattr(usage_router.queries, "personal_org_id", personal_org_id)
    monkeypatch.setattr(usage_router.queries, "list_all_sessions", list_all_sessions)
    monkeypatch.setattr(usage_router.queries, "list_projects", list_projects)
    monkeypatch.setattr(
        usage_router.queries, "list_harness_accounts_privileged", list_accounts
    )
    monkeypatch.setattr(
        usage_router.queries, "resolve_harness_credential_privileged", resolve
    )
    monkeypatch.setattr(usage_router.runtime, "load_project_context", load_project_context)
    monkeypatch.setattr(usage_router.ssh.pool, "get", pool_get)
    monkeypatch.setattr(usage_router.profile, "read_file", read_file)
    monkeypatch.setattr(usage_router.plan_usage, "fetch", fetch)
    w.statusline = {}

    async def exec_capture(conn, container, argv, **kw):
        # argv is ["sh", "-c", script, "sh", path]
        entry = w.statusline.get(argv[-1])
        if entry is None:
            return SimpleNamespace(ok=True, stdout="")
        mtime, doc = entry
        return SimpleNamespace(ok=True, stdout=f"{int(mtime)}\n{json.dumps(doc)}")

    monkeypatch.setattr(usage_router.docker_remote, "exec_capture", exec_capture)
    w.now = NOW
    monkeypatch.setattr(usage_router.time, "time", lambda: w.now)
    monkeypatch.setattr(plan_usage.time, "time", lambda: w.now)
    return w


PRINCIPAL = SimpleNamespace(claims={}, user_id="u")


@pytest.mark.asyncio
async def test_a_valid_stored_sign_in_is_used_as_is(world) -> None:
    out = await usage_router.read_account_usage(PRINCIPAL)
    personal = next(a for a in out if a.label == "Personal")
    assert personal.status == "ok" and personal.windows[0].percent == 42.0
    assert "personal-stored" in world.fetched_with


@pytest.mark.asyncio
async def test_an_expired_sign_in_borrows_from_a_running_session_on_that_account(world) -> None:
    project = {"id": uuid4(), "org_id": world.org, "harness_credential_id": world.ids.work}
    world.projects = [project]
    world.sessions = [
        {
            "project_id": project["id"], "is_mine": True, "state": "running",
            "home_dir": "/home/dev/sessions/client", "harness_credential_id": None,
            "tmux_session": "client",
        }
    ]
    world.container_files["/home/dev/sessions/client/.claude/.credentials.json"] = creds(
        "work-live", 3600
    )

    out = await usage_router.read_account_usage(PRINCIPAL)

    work = next(a for a in out if a.label == "Work")
    assert work.status == "ok"
    assert "work-live" in world.fetched_with
    assert "work-stored" not in world.fetched_with


@pytest.mark.asyncio
async def test_with_nothing_current_it_says_how_to_fix_it(world) -> None:
    out = await usage_router.read_account_usage(PRINCIPAL)
    work = next(a for a in out if a.label == "Work")
    assert work.status == "expired" and work.windows == []
    assert "sign it in again" in (work.detail or "")


@pytest.mark.asyncio
async def test_someone_elses_session_is_never_borrowed_from(world) -> None:
    project = {"id": uuid4(), "org_id": world.org, "harness_credential_id": world.ids.work}
    world.projects = [project]
    world.sessions = [
        {
            "project_id": project["id"], "is_mine": False, "state": "running",
            "home_dir": "/home/dev/sessions/theirs", "harness_credential_id": None,
        }
    ]
    world.container_files["/home/dev/sessions/theirs/.claude/.credentials.json"] = creds(
        "their-token", 3600
    )
    await usage_router.read_account_usage(PRINCIPAL)
    assert "their-token" not in world.fetched_with


@pytest.mark.asyncio
async def test_an_api_key_account_has_no_windows_and_is_never_asked(world) -> None:
    out = await usage_router.read_account_usage(PRINCIPAL)
    client = next(a for a in out if a.label == "Client")
    assert client.status == "api_key" and client.windows == []
    assert "sk" not in world.fetched_with


@pytest.mark.asyncio
async def test_a_second_look_within_a_minute_does_not_ask_again(world) -> None:
    await usage_router.read_account_usage(PRINCIPAL)
    asked = len(world.fetched_with)
    await usage_router.read_account_usage(PRINCIPAL)
    assert len(world.fetched_with) == asked


def test_retry_after_is_read_as_seconds_or_a_date() -> None:
    assert plan_usage.retry_after_seconds("120", NOW) == 120
    assert plan_usage.retry_after_seconds(None, NOW) is None
    assert plan_usage.retry_after_seconds("not a time", NOW) is None
    assert plan_usage.retry_after_seconds("Thu, 01 Jan 1970 00:00:00 GMT", NOW) == 0


@pytest.mark.asyncio
async def test_fetch_passes_on_how_long_to_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "90"}, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(plan_usage.UsageUnavailable) as err:
            await plan_usage.fetch("tok", client)
    assert err.value.reason == "rate_limited" and err.value.retry_after == 90


def _personal(out):
    return next(a for a in out if a.label == "Personal")


@pytest.mark.asyncio
async def test_after_being_told_to_slow_down_it_does_not_ask_again_until_it_may(world) -> None:
    world.refuse = plan_usage.UsageUnavailable("rate_limited", "slow down", retry_after=600)
    first = _personal(await usage_router.read_account_usage(PRINCIPAL))
    assert first.status == "rate_limited" and first.retry_at is not None
    asked = world.fetched_with.count("personal-stored")

    # A minute later, and five: still inside the wait it was given.
    for minutes in (1, 5):
        world.now = NOW + 60 * minutes
        out = _personal(await usage_router.read_account_usage(PRINCIPAL))
        assert out.status == "rate_limited"
    assert world.fetched_with.count("personal-stored") == asked

    # Once it has passed, it asks — and a good answer clears the backoff.
    world.refuse = None
    world.now = NOW + 601
    out = _personal(await usage_router.read_account_usage(PRINCIPAL))
    assert out.status == "ok" and out.retry_at is None


@pytest.mark.asyncio
async def test_with_no_retry_after_it_backs_off_longer_each_time(world) -> None:
    world.refuse = plan_usage.UsageUnavailable("rate_limited", "slow down")
    waits = []
    for _ in range(3):
        out = _personal(await usage_router.read_account_usage(PRINCIPAL))
        waits.append(out.retry_at.timestamp() - world.now)  # type: ignore[union-attr]
        world.now = out.retry_at.timestamp() + 1  # type: ignore[union-attr]
    assert waits == [300, 600, 1200]


@pytest.mark.asyncio
async def test_while_waiting_the_last_good_reading_stays_visible(world) -> None:
    await usage_router.read_account_usage(PRINCIPAL)
    world.now = NOW + plan_usage.CACHE_SECONDS + 1
    world.refuse = plan_usage.UsageUnavailable("rate_limited", "slow down", retry_after=60)
    out = _personal(await usage_router.read_account_usage(PRINCIPAL))
    assert out.status == "rate_limited" and out.stale
    assert out.windows[0].percent == 42.0


def _running_on_work(world, token_expiry_s):
    project = {"id": uuid4(), "org_id": world.org, "harness_credential_id": world.ids.work}
    world.projects = [project]
    world.sessions = [
        {
            "project_id": project["id"], "is_mine": True, "state": "running",
            "home_dir": "/home/dev/sessions/w", "harness_credential_id": None,
            "tmux_session": "w",
        }
    ]
    world.container_files["/home/dev/sessions/w/.claude/.credentials.json"] = creds(
        "work-live", token_expiry_s
    )


def _work(out):
    return next(a for a in out if a.label == "Work")


@pytest.mark.asyncio
async def test_a_refused_token_falls_through_to_the_next(world) -> None:
    _running_on_work(world, 3600)
    world.materials[world.ids.work]["oauth_token"] = "work-setup-token"
    world.refuse_token["work-live"] = plan_usage.UsageUnavailable("expired", "expired")

    out = _work(await usage_router.read_account_usage(PRINCIPAL))

    assert world.fetched_with[-2:] == ["work-live", "work-setup-token"]
    assert out.status == "ok" and out.source == "setup_token"


@pytest.mark.asyncio
async def test_says_when_only_a_setup_token_is_left_and_it_cannot_read_usage(world) -> None:
    # The session's token is there but idle and expired, so only the
    # long-lived token remains — and Anthropic will not let it read usage.
    _running_on_work(world, -60)
    world.materials[world.ids.work]["oauth_token"] = "work-setup-token"
    world.refuse_token["work-setup-token"] = plan_usage.UsageUnavailable(
        "unsupported", "not allowed. Anthropic said: \u201cscope\u201d"
    )

    out = _work(await usage_router.read_account_usage(PRINCIPAL))

    assert out.status == "unsupported"
    assert "setup-token" in (out.detail or "") and "scope" in (out.detail or "")


@pytest.mark.asyncio
async def test_a_borrowed_reading_says_where_it_came_from(world) -> None:
    _running_on_work(world, 3600)
    out = _work(await usage_router.read_account_usage(PRINCIPAL))
    assert out.status == "ok" and out.source == "session"


@pytest.mark.asyncio
async def test_a_limit_refusal_keeps_anthropics_words_while_waiting(world) -> None:
    world.refuse = plan_usage.UsageUnavailable(
        "rate_limited", "refused. Anthropic said: \u201cusage limit reached\u201d", retry_after=900
    )
    first = _personal(await usage_router.read_account_usage(PRINCIPAL))
    world.now = NOW + 60
    later = _personal(await usage_router.read_account_usage(PRINCIPAL))
    for out in (first, later):
        assert "usage limit reached" in (out.detail or "")
        assert out.retry_at is not None


@pytest.mark.asyncio
async def test_anthropics_own_message_is_passed_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"type": "permission_error", "message": "missing scope"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(plan_usage.UsageUnavailable) as err:
            await plan_usage.fetch("tok", client)
    assert "missing scope" in err.value.detail


# --- readings a session's status line saved ----------------------------------

STATUSLINE_DOC = {
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 41.2, "resets_at": NOW + 86400},
        "spend_limit": {"used_percentage": 5, "resets_at": NOW + 99},
    }
}


def test_a_status_line_reading_becomes_windows() -> None:
    windows = plan_usage.parse_statusline(STATUSLINE_DOC, NOW)
    assert [(w.key, w.percent) for w in windows] == [("five_hour", 23.5), ("seven_day", 41.2)]
    assert windows[0].resets_at is not None


def test_a_window_whose_reset_passed_is_empty_again() -> None:
    doc = {"rate_limits": {"five_hour": {"used_percentage": 90, "resets_at": NOW - 1}}}
    [w] = plan_usage.parse_statusline(doc, NOW)
    assert w.percent == 0 and w.resets_at is None


def test_an_odd_status_line_file_is_no_windows() -> None:
    assert plan_usage.parse_statusline({"model": {}}, NOW) == []
    assert plan_usage.parse_statusline([1], NOW) == []


def _on(world, account_id, home):
    project = {"id": uuid4(), "org_id": world.org, "harness_credential_id": account_id}
    world.projects.append(project)
    world.sessions.append(
        {
            "project_id": project["id"], "is_mine": True, "state": "running",
            "home_dir": home, "harness_credential_id": None, "tmux_session": home,
        }
    )


@pytest.mark.asyncio
async def test_a_sessions_status_line_is_read_before_asking_anthropic(world) -> None:
    _on(world, world.ids.work, "/home/dev/sessions/w")
    world.statusline["/home/dev/sessions/w/.moonphase/usage.json"] = (NOW - 30, STATUSLINE_DOC)

    work = next(a for a in await usage_router.read_account_usage(PRINCIPAL) if a.label == "Work")

    assert work.status == "ok" and work.source == "statusline"
    assert [w.percent for w in work.windows] == [23.5, 41.2]
    assert work.fetched_at is not None and work.fetched_at.timestamp() == NOW - 30
    assert "work-stored" not in world.fetched_with


@pytest.mark.asyncio
async def test_the_newest_of_several_sessions_wins(world) -> None:
    _on(world, world.ids.work, "/home/dev/sessions/a")
    _on(world, world.ids.work, "/home/dev/sessions/b")
    older = {"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": NOW + 60}}}
    world.statusline["/home/dev/sessions/a/.moonphase/usage.json"] = (NOW - 600, older)
    world.statusline["/home/dev/sessions/b/.moonphase/usage.json"] = (NOW - 5, STATUSLINE_DOC)

    work = next(a for a in await usage_router.read_account_usage(PRINCIPAL) if a.label == "Work")

    assert work.windows[0].percent == 23.5


@pytest.mark.asyncio
async def test_without_a_status_line_reading_anthropic_is_still_asked(world) -> None:
    out = await usage_router.read_account_usage(PRINCIPAL)
    personal = next(a for a in out if a.label == "Personal")
    assert personal.source == "stored" and "personal-stored" in world.fetched_with
