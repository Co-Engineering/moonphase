"""Each Claude account's plan limits, written down and read back.

The monitor collects them (`account_usage.collect`, `refresh_from_stored`) and
the dashboard only reads what was written. No server and no database here: a
fake container answers the status-line files and credentials files, a fake
store stands in for `private.harness_account_usage`, and a fake Anthropic
answers with whichever token it is given.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from moonphase import account_usage, plan_usage
from moonphase.routers import usage as usage_router

NOW = 1_800_000_000.0

SAMPLE = {
    "five_hour": {"utilization": 23.5, "resets_at": "2027-01-15T12:00:00Z"},
    "seven_day": {"utilization": 41.2, "resets_at": "2027-01-20T09:00:00Z"},
}
STATUSLINE = {
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 41.2, "resets_at": NOW + 86400},
    }
}


def creds(token: str, expires_in: float) -> str:
    return json.dumps(
        {"claudeAiOauth": {"accessToken": token, "expiresAt": int((NOW + expires_in) * 1000)}}
    )


@asynccontextmanager
async def _no_db(*_: Any):
    yield None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    org = uuid4()
    personal, work, key = uuid4(), uuid4(), uuid4()
    w = SimpleNamespace(
        org=org,
        ids=SimpleNamespace(personal=personal, work=work, key=key),
        accounts={
            personal: {"id": personal, "label": "Personal", "auth_mode": "oauth",
                       "is_default": True, "oauth_blob": creds("personal-stored", 3600)},
            work: {"id": work, "label": "Work", "auth_mode": "oauth",
                   "is_default": False, "oauth_blob": creds("work-stored", -3600)},
            key: {"id": key, "label": "Client", "auth_mode": "api_key",
                  "is_default": False, "oauth_blob": None},
        },
        # The fake store: account id -> kept row.
        kept={},
        statusline={},  # home -> (mtime, doc)
        files={},  # path -> contents
        fetched_with=[],
        refuse={},  # token -> UsageUnavailable
        looked_in=[],
    )

    async def personal_org_id_for_user(conn, user_id):
        return w.org

    async def resolve(conn, *, org_id, project_id, harness, account_id=None):
        assert harness == "claude_code"
        chosen = account_id or personal
        return w.accounts.get(chosen)

    async def save_reading(conn, *, account_id, windows, read_at, source):
        row = w.kept.get(account_id)
        if row and row.get("read_at") and read_at <= row["read_at"]:
            return
        w.kept[account_id] = {
            "account_id": account_id, "windows": windows, "read_at": read_at,
            "source": source, "problem": None, "detail": None, "retry_at": None,
            "refusals": 0,
        }

    async def save_problem(conn, *, account_id, problem, detail, retry_at, refusals):
        row = w.kept.setdefault(account_id, {"account_id": account_id, "windows": [],
                                             "read_at": None, "source": None})
        row.update(problem=problem, detail=detail, retry_at=retry_at, refusals=refusals)

    async def get_usage(conn, ids):
        return {i: dict(w.kept[i]) for i in ids if i in w.kept}

    async def accounts_for_usage(conn):
        return [
            {"id": a["id"], "oauth_blob": a["oauth_blob"], **{
                k: (w.kept.get(a["id"]) or {}).get(k) for k in ("read_at", "retry_at", "refusals")
            }}
            for a in w.accounts.values()
            if a["auth_mode"] == "oauth"
        ]

    async def exec_capture(conn, container, argv, **kw):
        homes = argv[4:]
        w.looked_in.extend(homes)
        out = "".join(
            f"{h}\t{int(w.statusline[h][0])}\t{json.dumps(w.statusline[h][1])}\n"
            for h in homes
            if h in w.statusline
        )
        return SimpleNamespace(ok=True, stdout=out)

    async def read_file(conn, container, path):
        w.looked_in.append(path)
        return w.files.get(path)

    async def fetch(token, client=None):
        w.fetched_with.append(token)
        if token in w.refuse:
            raise w.refuse[token]
        return plan_usage.parse_windows(SAMPLE)

    q = account_usage.queries
    monkeypatch.setattr(account_usage, "service_session", _no_db)
    monkeypatch.setattr(q, "personal_org_id_for_user_privileged", personal_org_id_for_user)
    monkeypatch.setattr(q, "resolve_harness_credential_privileged", resolve)
    monkeypatch.setattr(q, "save_account_reading_privileged", save_reading)
    monkeypatch.setattr(q, "save_account_problem_privileged", save_problem)
    monkeypatch.setattr(q, "get_account_usage_privileged", get_usage)
    monkeypatch.setattr(q, "claude_accounts_for_usage_privileged", accounts_for_usage)
    monkeypatch.setattr(account_usage.docker_remote, "exec_capture", exec_capture)
    monkeypatch.setattr(account_usage.profile, "read_file", read_file)
    monkeypatch.setattr(account_usage.plan_usage, "fetch", fetch)

    # The dashboard's side.
    async def personal_org_id(conn):
        return w.org

    async def list_accounts(conn, org_id, harness):
        return [
            {k: a[k] for k in ("id", "label", "auth_mode", "is_default")}
            for a in w.accounts.values()
        ]

    monkeypatch.setattr(usage_router, "user_session", _no_db)
    monkeypatch.setattr(usage_router, "service_session", _no_db)
    monkeypatch.setattr(usage_router.queries, "personal_org_id", personal_org_id)
    monkeypatch.setattr(usage_router.queries, "list_harness_accounts_privileged", list_accounts)
    monkeypatch.setattr(usage_router.time, "time", lambda: NOW)
    return w


def session(world, name: str, *, account: UUID | None = None, harness: str = "claude_code"):
    return {
        "user_id": "u", "project_id": uuid4(), "project_org_id": world.org,
        "session_account_id": account, "harness": harness,
        "home_dir": f"/home/dev/sessions/{name}",
    }


async def dashboard(world) -> dict[str, Any]:
    out = await usage_router.read_account_usage(SimpleNamespace(claims={}, user_id="u"))
    return {a.label: a for a in out}


def at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


# --- what the sessions saw is written down ------------------------------------


async def test_a_sessions_status_line_is_written_down_and_shown(world) -> None:
    s = session(world, "w", account=world.ids.work)
    world.statusline[s["home_dir"]] = (NOW - 30, STATUSLINE)

    await account_usage.collect(None, "c", [s], NOW)  # type: ignore[arg-type]

    work = (await dashboard(world))["Work"]
    assert work.status == "ok" and work.source == "statusline"
    assert [w.percent for w in work.windows] == [23.5, 41.2]
    assert work.fetched_at == at(NOW - 30)
    assert world.fetched_with == []


async def test_it_is_still_shown_after_every_session_on_the_account_is_gone(world) -> None:
    """The point of writing it down: the window keeps counting down after
    the sessions that saw it are closed, and so does what it says."""
    s = session(world, "w", account=world.ids.work)
    world.statusline[s["home_dir"]] = (NOW - 30, STATUSLINE)
    await account_usage.capture_before_removal(None, "c", s, NOW)  # type: ignore[arg-type]
    world.statusline.clear()

    work = (await dashboard(world))["Work"]

    assert [w.percent for w in work.windows] == [23.5, 41.2]


async def test_the_newest_of_several_sessions_wins(world) -> None:
    a, b = session(world, "a", account=world.ids.work), session(world, "b", account=world.ids.work)
    older = {"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": NOW + 60}}}
    world.statusline[a["home_dir"]] = (NOW - 600, older)
    world.statusline[b["home_dir"]] = (NOW - 5, STATUSLINE)

    await account_usage.collect(None, "c", [a, b], NOW)  # type: ignore[arg-type]

    assert (await dashboard(world))["Work"].windows[0].percent == 23.5


async def test_an_older_reading_never_replaces_a_newer_one(world) -> None:
    """Two containers, read in either order: what is kept is the newest."""
    new = session(world, "new", account=world.ids.work)
    old = session(world, "old", account=world.ids.work)
    world.statusline[new["home_dir"]] = (NOW - 5, STATUSLINE)
    world.statusline[old["home_dir"]] = (
        NOW - 900,
        {"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": NOW + 60}}},
    )

    await account_usage.collect(None, "c1", [new], NOW)  # type: ignore[arg-type]
    await account_usage.collect(None, "c2", [old], NOW)  # type: ignore[arg-type]

    assert (await dashboard(world))["Work"].windows[0].percent == 23.5


async def test_a_window_whose_reset_passed_since_shows_as_empty(world, monkeypatch) -> None:
    s = session(world, "w", account=world.ids.work)
    world.statusline[s["home_dir"]] = (NOW - 30, STATUSLINE)
    await account_usage.collect(None, "c", [s], NOW)  # type: ignore[arg-type]
    monkeypatch.setattr(usage_router.time, "time", lambda: NOW + 7200)

    windows = (await dashboard(world))["Work"].windows

    assert [(w.key, w.percent, w.resets_at) for w in windows][0] == ("five_hour", 0.0, None)
    assert windows[1].percent == 41.2


async def test_an_antigravity_session_is_never_read_as_a_claude_account(world) -> None:
    """One that names no account of its own would fall through to Claude's
    default, and whatever its HOME held would be reported as Claude's."""
    s = session(world, "agy", harness="antigravity")
    world.statusline[s["home_dir"]] = (NOW - 30, STATUSLINE)
    world.files[f"{s['home_dir']}/.claude/.credentials.json"] = creds("not-claudes", 3600)

    await account_usage.collect(None, "c", [s], NOW)  # type: ignore[arg-type]

    assert world.looked_in == []
    assert world.ids.personal not in world.kept


async def test_an_api_key_account_is_never_read_or_asked(world) -> None:
    s = session(world, "k", account=world.ids.key)
    world.statusline[s["home_dir"]] = (NOW - 30, STATUSLINE)

    await account_usage.collect(None, "c", [s], NOW)  # type: ignore[arg-type]

    client = (await dashboard(world))["Client"]
    assert client.status == "api_key" and client.windows == []
    assert world.fetched_with == []


# --- asking Anthropic, in the background --------------------------------------


async def test_a_stale_account_is_asked_with_a_token_borrowed_from_its_session(world) -> None:
    s = session(world, "w", account=world.ids.work)
    world.files[f"{s['home_dir']}/.claude/.credentials.json"] = creds("work-live", 3600)

    await account_usage.collect(None, "c", [s], NOW)  # type: ignore[arg-type]

    work = (await dashboard(world))["Work"]
    assert world.fetched_with == ["work-live"]
    assert work.status == "ok" and work.source == "session"


async def test_a_fresh_reading_is_not_asked_about_again(world) -> None:
    s = session(world, "w", account=world.ids.work)
    world.statusline[s["home_dir"]] = (NOW - 30, STATUSLINE)
    world.files[f"{s['home_dir']}/.claude/.credentials.json"] = creds("work-live", 3600)

    await account_usage.collect(None, "c", [s], NOW)  # type: ignore[arg-type]

    assert world.fetched_with == []


async def test_an_expired_borrowed_token_is_never_used(world) -> None:
    s = session(world, "w", account=world.ids.work)
    world.files[f"{s['home_dir']}/.claude/.credentials.json"] = creds("old", -60)

    await account_usage.collect(None, "c", [s], NOW)  # type: ignore[arg-type]

    assert world.fetched_with == []
    assert (await dashboard(world))["Work"].status == "waiting"


async def test_a_valid_stored_sign_in_is_used_with_no_session_running(world) -> None:
    asked = await account_usage.refresh_from_stored(NOW)

    personal = (await dashboard(world))["Personal"]
    assert asked == 1 and world.fetched_with == ["personal-stored"]
    assert personal.status == "ok" and personal.source == "stored"


async def test_a_refused_token_falls_through_to_the_next(world) -> None:
    world.refuse["first"] = plan_usage.UsageUnavailable("expired", "This sign-in has expired.")

    await account_usage.ask_anthropic(
        world.ids.work,
        [("stored", plan_usage.AccountToken("first", None)),
         ("session", plan_usage.AccountToken("second", None))],
        NOW,
    )

    assert world.fetched_with == ["first", "second"]
    assert (await dashboard(world))["Work"].source == "session"


async def test_after_being_told_to_slow_down_it_waits_and_keeps_the_last_reading(world) -> None:
    s = session(world, "w", account=world.ids.work)
    world.statusline[s["home_dir"]] = (NOW - 3600, STATUSLINE)
    world.files[f"{s['home_dir']}/.claude/.credentials.json"] = creds("work-live", 7200)
    world.refuse["work-live"] = plan_usage.UsageUnavailable(
        "rate_limited", "Anthropic refused. Anthropic said: slow down", retry_after=120
    )

    await account_usage.collect(None, "c", [s], NOW)  # type: ignore[arg-type]
    work = (await dashboard(world))["Work"]

    assert work.status == "rate_limited" and "slow down" in (work.detail or "")
    assert work.retry_at == at(NOW + 120)
    assert work.stale and [w.percent for w in work.windows] == [23.5, 41.2]

    # Not asked again before then.
    await account_usage.collect(None, "c", [s], NOW + 60)  # type: ignore[arg-type]
    assert world.fetched_with == ["work-live"]


async def test_with_no_retry_after_it_backs_off_longer_each_time(world) -> None:
    world.refuse["t"] = plan_usage.UsageUnavailable("rate_limited", "No.")
    token = [("session", plan_usage.AccountToken("t", None))]

    waits = []
    for refusals in range(3):
        await account_usage.ask_anthropic(world.ids.work, token, NOW, refusals)
        waits.append(world.kept[world.ids.work]["retry_at"].timestamp() - NOW)

    assert waits == [300.0, 600.0, 1200.0]


async def test_with_nothing_kept_yet_the_dashboard_says_what_will_fill_it(world) -> None:
    work = (await dashboard(world))["Work"]

    assert work.status == "waiting" and work.windows == []
    assert "gets a reply from Claude" in (work.detail or "")


def test_due_follows_the_staleness_and_any_wait() -> None:
    assert account_usage.due(None, NOW)
    assert not account_usage.due({"read_at": at(NOW - 60)}, NOW)
    assert account_usage.due({"read_at": at(NOW - account_usage.STALE_SECONDS)}, NOW)
    assert not account_usage.due({"read_at": None, "retry_at": at(NOW + 1)}, NOW)


# --- deleting a session --------------------------------------------------------


async def test_deleting_a_session_reads_it_one_last_time_before_its_home_goes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The monitor reads it every couple of minutes; this is the last word,
    taken before the file it is in is deleted."""
    from moonphase.routers import projects

    done: list[str] = []
    project_id, account = uuid4(), uuid4()
    row = {
        "project_id": project_id, "tmux_session": "s", "user_id": "u", "is_mine": True,
        "harness": "claude_code", "home_dir": "/home/dev/sessions/s",
        "harness_credential_id": account,
    }

    async def get_project(conn, pid):
        return {"id": pid, "org_id": "o", "access": "admin"}

    async def get_session(conn, pid, name):
        return row

    async def load_project_context(claims, pid, *, require=None):
        return SimpleNamespace(target="t", container="c")

    async def pool_get(target):
        return object()

    async def capture(conn, container, r, now):
        assert r["session_account_id"] == account and r["project_org_id"] == "o"
        done.append("captured")

    def step(name: str):
        async def run(*a: Any, **k: Any) -> bool:
            done.append(name)
            return True

        return run

    monkeypatch.setattr(projects, "user_session", _no_db)
    monkeypatch.setattr(projects.queries, "get_project", get_project)
    monkeypatch.setattr(projects.queries, "get_session", get_session)
    monkeypatch.setattr(projects.queries, "delete_session_row", step("row deleted"))
    monkeypatch.setattr(projects.runtime, "load_project_context", load_project_context)
    monkeypatch.setattr(projects.ssh.pool, "get", pool_get)
    monkeypatch.setattr(projects.account_usage, "capture_before_removal", capture)
    monkeypatch.setattr(projects.sessions, "kill_session", step("killed"))
    monkeypatch.setattr(projects.workspaces, "remove_worktree", step("worktree removed"))
    monkeypatch.setattr(projects.sessions, "remove_session_home", step("home removed"))

    await projects.delete_session(project_id, "s", SimpleNamespace(claims={}, user_id="u"))

    assert done.index("captured") < done.index("home removed")
