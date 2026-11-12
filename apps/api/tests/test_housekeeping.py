"""What the daily clean-up removes, and what it never does.

The scripts are run for real, against a temporary tree standing in for a
container's /tmp, /home/dev and /workspace (and a stub `docker` for the
server's), with the cap scaled down so a few files can exceed it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from moonphase import housekeeping

DAY = 86400
MB = 1024 * 1024


def _file(path: Path, size: int = 1024, age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.truncate(size) if size > 10 * MB else f.write(os.urandom(size))
    when = time.time() - age_days * DAY
    os.utime(path, (when, when))
    return path


def _age(path: Path, days: float) -> None:
    when = time.time() - days * DAY
    os.utime(path, (when, when))


@pytest.fixture
def box(tmp_path: Path):
    root = tmp_path / "c"
    tmp, home, workspace = root / "tmp", root / "home" / "dev", root / "workspace"
    for d in (tmp, home, workspace):
        d.mkdir(parents=True)
    # Through placeholders: the temporary tree is itself under /tmp.
    script = (
        housekeeping.CONTAINER_SCRIPT.replace(f"CAP_KB={housekeeping.CACHE_CAP_KB}", "CAP_KB=3072")
        .replace("/home/dev", "@HOME@")
        .replace("/workspace", "@WORKSPACE@")
        .replace("/tmp", "@TMP@")
        .replace("@HOME@", str(home))
        .replace("@WORKSPACE@", str(workspace))
        .replace("@TMP@", str(tmp))
    )

    def run() -> str:
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return result.stdout

    return tmp, home, workspace, run


def test_tmp_keeps_what_is_recent_and_drops_what_sat_for_a_week(box) -> None:
    tmp, _, _, run = box
    old = _file(tmp / "claude-1001" / "proj" / "task" / "out.log", age_days=8)
    fresh = _file(tmp / "claude-1001" / "proj" / "task" / "now.log", age_days=1)
    empty_old = tmp / "go-build123"
    empty_old.mkdir()
    _age(empty_old, 9)

    run()

    assert not old.exists() and fresh.exists()
    assert not empty_old.exists()


def test_an_abandoned_huge_file_goes_after_a_day_but_not_one_being_written(box) -> None:
    """The 17 GB dev-server log: recent by the week's measure, but done."""
    tmp, _, _, run = box
    done = _file(tmp / "claude-1001" / "p" / "s" / "scratchpad" / "dev.log", 1100 * MB, 1.5)
    live = _file(tmp / "claude-1001" / "p" / "s" / "scratchpad" / "live.log", 1100 * MB, 0.1)

    run()

    assert not done.exists() and live.exists()


def test_tmux_socket_is_never_touched(box) -> None:
    """Every session's terminal goes through it; it can be weeks old."""
    tmp, _, _, run = box
    tmux = tmp / "tmux-1001"
    tmux.mkdir()
    sock_path = tmux / "default"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(sock_path))
    try:
        os.utime(sock_path, (time.time() - 60 * DAY,) * 2)
        _age(tmux, 60)

        run()

        assert sock_path.exists() and tmux.exists()
    finally:
        sock.close()


def test_go_build_cache_is_trimmed_to_the_cap_least_recently_used_first(box) -> None:
    _, home, _, run = box
    cache = home / "sessions" / "init" / ".cache" / "go-build" / "ab"
    files = [_file(cache / f"f{i}-d", MB, age_days=6 - i) for i in range(6)]

    out = run()

    assert "trimmed" in out
    kept = [f.exists() for f in files]
    # Oldest first: whatever went is a run from the oldest end, enough to get
    # under the cap (directories count too), and the newest stay.
    assert kept == sorted(kept)
    assert kept[:3] == [False] * 3 and kept[-2:] == [True, True]


def test_a_cache_under_the_cap_is_left_alone(box) -> None:
    _, home, _, run = box
    small = _file(home / ".cache" / "go-build" / "aa" / "x-d", MB, age_days=30)

    run()

    assert small.exists()


def test_an_oversized_next_cache_is_removed_but_the_build_and_code_stay(box) -> None:
    _, home, workspace, run = box
    big = home / "sessions" / "leads" / "devserver" / ".next"
    for i in range(4):
        _file(big / "cache" / "webpack" / f"{i}.pack", MB)
    _file(big / "server" / "app.js", 10)
    small = workspace / "web" / ".next" / "cache"
    _file(small / "images" / "a.webp", MB)
    code = _file(home / "sessions" / "leads" / "devserver" / "app" / "page.tsx", 10, 30)

    run()

    assert not (big / "cache").exists()
    assert (big / "server" / "app.js").exists() and code.exists()
    assert small.exists()


def test_nothing_outside_tmp_and_the_caches_is_touched(box) -> None:
    _, home, workspace, run = box
    keep = [
        _file(home / "sessions" / "a" / "work" / "main.go", 4 * MB, 100),
        _file(home / "sessions" / "a" / ".claude" / "projects" / "t.jsonl", MB, 100),
        _file(home / "models" / "olmoe" / "weights.bin", 4 * MB, 100),
        _file(workspace / "node_modules" / "x" / "index.js", 4 * MB, 100),
    ]

    run()

    assert all(p.exists() for p in keep)


# --- the server ---------------------------------------------------------------

STUB_DOCKER = r"""#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$1 $2" in
  "ps -a") printf 'moonphase/env-debian:inuse\nnginx:latest\n' ;;
  "images --format")
    printf 'moonphase/env-debian:inuse\nmoonphase/env-debian:old\n'
    printf 'moonphase/env-browser:new\nnginx:latest\nmoonphase/env-x:gone\n' ;;
  "image inspect")
    case "$5" in
      *:new) date -u -d '-2 hours' +%Y-%m-%dT%H:%M:%S.000000000Z ;;
      *) echo 2026-01-01T00:00:00.000000000Z ;;
    esac ;;
esac
"""


def test_only_unused_environment_images_older_than_a_day_are_removed(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"

    subprocess.run(
        ["sh", "-c", housekeeping.SERVER_SCRIPT],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "DOCKER_LOG": str(log)},
        check=True,
        timeout=60,
    )

    removed = [line.split()[1] for line in log.read_text().splitlines() if line.startswith("rmi")]
    assert sorted(removed) == ["moonphase/env-debian:old", "moonphase/env-x:gone"]
    assert "builder prune -af --filter until=168h" in log.read_text()


# --- the monitor's schedule ---------------------------------------------------


async def test_the_monitor_cleans_running_containers_on_online_servers_once_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from moonphase import monitor as monitor_module

    @asynccontextmanager
    async def no_db():
        yield None

    async def list_servers(conn):
        return [{"id": "s1", "name": "worker", "status": "online"},
                {"id": "s2", "name": "asleep", "status": "offline"}]

    async def list_projects(conn):
        return [
            {"server_id": "s1", "container_name": "mp-a", "status": "running"},
            {"server_id": "s1", "container_name": "mp-b", "status": "stopped"},
            {"server_id": "s2", "container_name": "mp-c", "status": "running"},
        ]

    cleaned: list[str] = []

    async def clean_container(conn, container):
        cleaned.append(container)
        return ""

    async def clean_server(conn):
        cleaned.append("server")
        return ""

    async def target_for(self, row):
        return row["server_id"]

    async def pool_get(target):
        return object()

    monkeypatch.setattr(monitor_module, "service_session", no_db)
    monkeypatch.setattr(monitor_module.queries, "list_servers", list_servers)
    monkeypatch.setattr(monitor_module.queries, "list_projects", list_projects)
    monkeypatch.setattr(monitor_module.housekeeping, "clean_container", clean_container)
    monkeypatch.setattr(monitor_module.housekeeping, "clean_server", clean_server)
    monkeypatch.setattr(monitor_module.SessionMonitor, "_target_for", target_for)
    monkeypatch.setattr(monitor_module.ssh.pool, "get", pool_get)
    monitor = monitor_module.SessionMonitor()

    assert await monitor.sweep_housekeeping() == 1
    assert cleaned == ["mp-a", "server"]
    # Not again the same day.
    assert await monitor.sweep_housekeeping() == 0
    assert cleaned == ["mp-a", "server"]
