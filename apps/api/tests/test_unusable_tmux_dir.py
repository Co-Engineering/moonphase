"""A container whose tmux directory came back owned by an id it no longer maps.

Seen on a Sysbox container after its host rebooted: /tmp/tmux-1001 owned by
`nobody`, tmux refusing to start ("has unsafe permissions"), and nothing in
the container able to remove it. Moonphase recreates such a container from
its image, keeping both volumes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from moonphase import ssh
from moonphase.routers import projects


def _check(tmp: Path, uid: int) -> int:
    script = projects._TMUX_DIR_UNUSABLE.replace("/tmp", str(tmp)).replace(
        "$(id -u dev)", str(uid)
    )
    return subprocess.run(["sh", "-c", script]).returncode


def test_the_check_is_true_only_for_a_directory_someone_else_owns(tmp_path: Path) -> None:
    me = os.getuid()
    assert _check(tmp_path, me) != 0, "no tmux directory yet: tmux makes one"
    (tmp_path / f"tmux-{me}").mkdir()
    assert _check(tmp_path, me) != 0, "the user's own"
    # The same directory, as seen by a container whose dev user is another id.
    (tmp_path / f"tmux-{me + 1}").mkdir()
    assert _check(tmp_path, me + 1) == 0


@pytest.fixture
def start(monkeypatch: pytest.MonkeyPatch):
    w = SimpleNamespace(unusable=False, recreated=0, started=0)

    async def pool_get(target):
        return object()

    async def inspect(conn, name):
        return SimpleNamespace(state="running", image="img", runtime="sysbox-runc")

    async def stale(principal, ctx, container):
        return False

    async def exec_capture(conn, container, argv, **kw):
        assert kw.get("user") == "root"
        return ssh.CommandResult(exit_status=0 if w.unusable else 1, stdout="", stderr="")

    async def recreate(principal, ctx):
        w.recreated += 1

    monkeypatch.setattr(projects.ssh.pool, "get", pool_get)
    monkeypatch.setattr(projects.docker_remote, "inspect", inspect)
    monkeypatch.setattr(projects, "_recreate_if_stale", stale)
    monkeypatch.setattr(projects.docker_remote, "exec_capture", exec_capture)
    monkeypatch.setattr(projects, "_recreate_container", recreate)
    return w


async def test_a_container_tmux_cannot_start_in_is_recreated(start) -> None:
    start.unusable = True
    ctx: Any = SimpleNamespace(target="t", container="mp-nextengine")

    await projects._start_container(None, ctx)  # type: ignore[arg-type]

    assert start.recreated == 1


async def test_a_healthy_container_is_left_alone(start) -> None:
    ctx: Any = SimpleNamespace(target="t", container="mp-ok")

    await projects._start_container(None, ctx)  # type: ignore[arg-type]

    assert start.recreated == 0
