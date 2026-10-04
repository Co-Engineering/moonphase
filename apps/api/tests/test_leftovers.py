"""Finding and deleting session directories whose session is gone.

The thing that must never happen is deleting a directory something is still
using — a session owned by someone else, one being created right now, or one
started between the person looking and the person confirming.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from moonphase import leftovers
from moonphase.leftovers import SessionDirectory
from moonphase.routers import projects
from moonphase.schemas import LeftoversCleanIn

NOW = 1_800_000_000.0
OLD = NOW - 3600


def _dir(name: str, size: int = 100, mtime: float = OLD) -> SessionDirectory:
    return SessionDirectory(name=name, bytes=size, modified_at=mtime)


def _orphaned(dirs, *, homes=(), names=(), live=()):
    return leftovers.orphaned(
        dirs,
        claimed_homes=set(homes),
        claimed_names=set(names),
        live_tmux=set(live),
        now=NOW,
    )


def test_a_directory_no_session_claims_is_left_over() -> None:
    found = _orphaned([_dir("gone"), _dir("kept")], homes={"/home/dev/sessions/kept"})
    assert [d.name for d in found] == ["gone"]


def test_a_directory_claimed_by_name_is_kept_even_if_its_home_differs() -> None:
    assert _orphaned([_dir("alice")], names={"alice"}) == []


def test_a_running_tmux_session_keeps_its_directory_before_its_row_exists() -> None:
    assert _orphaned([_dir("brand-new")], live={"brand-new"}) == []


def test_a_recently_changed_directory_is_kept() -> None:
    assert _orphaned([_dir("just-made", mtime=NOW - 60)]) == []


def test_names_moonphase_could_not_have_made_are_never_touched() -> None:
    assert _orphaned([_dir(".."), _dir("has space"), _dir("a/b")]) == []


def test_largest_first() -> None:
    found = _orphaned([_dir("small", 1), _dir("big", 10), _dir("mid", 5)])
    assert [d.name for d in found] == ["big", "mid", "small"]


def test_parse_listing_skips_lines_it_cannot_read() -> None:
    parsed = leftovers.parse_listing(
        "a\t123\t1700000000\nbroken line\nb\tnotanumber\t1\nc\t0\t1700000001\n"
    )
    assert [(d.name, d.bytes) for d in parsed] == [("a", 123), ("c", 0)]


# --- the routes -------------------------------------------------------------


class _World:
    """Everything the routes reach over SSH and the database, faked."""

    def __init__(self, directories, *, homes=(), names=(), live=()) -> None:
        self.directories = list(directories)
        self.homes = set(homes)
        self.names = set(names)
        self.live = set(live)
        self.removed: list[str] = []
        self.started = False
        self.state = "running"


@pytest.fixture
def world(monkeypatch):
    w = _World([])

    async def load_project_context(claims, project_id, *, require):
        assert require is projects.CAN_ADMINISTER
        return SimpleNamespace(target="t", container="mp-x")

    async def pool_get(target):
        return object()

    async def inspect(conn, name):
        return SimpleNamespace(state=w.state)

    async def start(conn, name):
        w.started = True

    async def list_directories(conn, container):
        return list(w.directories)

    async def live_tmux_sessions(conn, container):
        return set(w.live)

    async def remove(conn, container, names):
        w.removed.extend(names)

    async def claims(conn, project_id):
        return set(w.homes), set(w.names)

    @asynccontextmanager
    async def service_session():
        yield None

    monkeypatch.setattr(projects.runtime, "load_project_context", load_project_context)
    monkeypatch.setattr(projects.ssh.pool, "get", pool_get)
    monkeypatch.setattr(projects.docker_remote, "inspect", inspect)
    monkeypatch.setattr(projects.docker_remote, "start", start)
    monkeypatch.setattr(projects.leftovers, "list_directories", list_directories)
    monkeypatch.setattr(projects.leftovers, "live_tmux_sessions", live_tmux_sessions)
    monkeypatch.setattr(projects.leftovers, "remove", remove)
    monkeypatch.setattr(projects.queries, "session_claims_privileged", claims)
    monkeypatch.setattr(projects, "service_session", service_session)
    monkeypatch.setattr(projects.time, "time", lambda: NOW)
    return w


PRINCIPAL = SimpleNamespace(claims={})


@pytest.mark.asyncio
async def test_scan_reports_what_deleting_would_free(world) -> None:
    world.directories = [_dir("old-1", 4_000), _dir("old-2", 1_000), _dir("live", 9)]
    world.homes = {"/home/dev/sessions/live"}

    out = await projects.get_leftovers(uuid4(), PRINCIPAL)

    assert [s.name for s in out.sessions] == ["old-1", "old-2"]
    assert out.total_bytes == 5_000
    assert world.removed == []


@pytest.mark.asyncio
async def test_clean_deletes_only_what_was_confirmed(world) -> None:
    world.directories = [_dir("old-1", 4_000), _dir("old-2", 1_000)]

    out = await projects.clean_leftovers(
        uuid4(), LeftoversCleanIn(names=["old-1"]), PRINCIPAL
    )

    assert world.removed == ["old-1"]
    assert out.freed_bytes == 4_000


@pytest.mark.asyncio
async def test_clean_never_deletes_a_session_started_since_the_scan(world) -> None:
    world.directories = [_dir("reused", 4_000)]
    # Someone opened a session with that name between looking and confirming.
    world.names = {"reused"}

    out = await projects.clean_leftovers(
        uuid4(), LeftoversCleanIn(names=["reused"]), PRINCIPAL
    )

    assert world.removed == []
    assert out.removed == []


@pytest.mark.asyncio
async def test_clean_ignores_names_that_were_never_left_over(world) -> None:
    world.directories = [_dir("old", 10)]

    await projects.clean_leftovers(
        uuid4(), LeftoversCleanIn(names=["old", "..", "someone-elses"]), PRINCIPAL
    )

    assert world.removed == ["old"]


@pytest.mark.asyncio
async def test_a_stopped_container_is_started_not_recreated(world) -> None:
    world.state = "exited"
    await projects.get_leftovers(uuid4(), PRINCIPAL)
    assert world.started
