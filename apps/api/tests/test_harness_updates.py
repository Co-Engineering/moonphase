"""Keeping the harness CLIs current inside a project container.

The update script is the part that runs unattended as root in every container,
so it is run for real here — with `sh`, against temporary directories, and with
a stub `npm` on PATH standing in for the registry. The launcher is run for real
too, since "which binary did it start, with what environment" is a question
about a shell script, not about the Python that wrote it.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from moonphase import harness as registry
from moonphase import harness_updates, sessions, ssh
from moonphase.config import get_settings
from moonphase.harness import SessionSpace
from moonphase.monitor import SessionMonitor
from moonphase.ssh import CommandResult

CLAUDE = "anthropic-ai-claude-code"
OPENCODE = "opencode-ai"

# Answers `view` with $STUB_VERSION, and `install -g --prefix P ... pkg@ver` by
# creating P/bin/<binary> printing the version — or, per $STUB_MODE, cannot
# reach the registry, fails the install, or installs a binary that does not run.
STUB_NPM = r"""#!/bin/sh
echo "npm $* prefix=${npm_config_prefix:-unset}" >> "$STUB_LOG"
case $1 in
    view) [ "${STUB_MODE:-}" = offline ] && exit 1; echo "$STUB_VERSION" ;;
    install)
        shift
        prefix= spec=
        while [ $# -gt 0 ]; do
            case $1 in
                --prefix) prefix=$2; shift ;;
                --cache) shift ;;
                -*) ;;
                *) spec=$1 ;;
            esac
            shift
        done
        [ "${STUB_MODE:-}" = fail ] && exit 1
        pkg=${spec%@*} ver=${spec##*@}
        case $pkg in
            @anthropic-ai/claude-code) bin=claude ;;
            opencode-ai) bin=opencode ;;
        esac
        mkdir -p "$prefix/bin" "$prefix/lib/node_modules/$pkg"
        if [ "${STUB_MODE:-}" = broken ]; then
            printf '#!/bin/sh\nexit 1\n' > "$prefix/bin/$bin"
        else
            printf '#!/bin/sh\necho %s\n' "$ver" > "$prefix/bin/$bin"
        fi
        chmod +x "$prefix/bin/$bin"
        ;;
esac
"""


class Box:
    """One container's worth of directories, and a way to run the script in it."""

    def __init__(self, base: Path, shell: str = "sh") -> None:
        self.base = base
        self.shell = shell
        self.root = base / "opt"
        self.state = base / "state"
        self.legacy = base / "home" / ".npm-global"
        self.home = base / "home"
        self.proc = base / "proc"
        self.bin = base / "bin"
        self.npm_log = base / "npm.log"
        self.script = base / "moonphase-harness-update"
        for d in (self.home, self.proc, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        self.script.write_text(harness_updates.update_script())
        npm = self.bin / "npm"
        npm.write_text(STUB_NPM)
        npm.chmod(0o755)

    def run(
        self, *args: str, version: str = "1.0.0", mode: str = "", **env: str
    ) -> subprocess.CompletedProcess[str]:
        full_env = {
            "PATH": os.environ["PATH"],
            "MOONPHASE_HARNESS_ROOT": str(self.root),
            "MOONPHASE_STATE_DIR": str(self.state),
            "MOONPHASE_LEGACY_PREFIX": str(self.legacy),
            "MOONPHASE_DEV_HOME": str(self.home),
            "MOONPHASE_PROC": str(self.proc),
            "MOONPHASE_UPDATE_PATH": f"{self.bin}:/usr/bin:/bin",
            "STUB_LOG": str(self.npm_log),
            "STUB_VERSION": version,
            "STUB_MODE": mode,
            # What a root `docker exec` inherits from the image.
            "npm_config_prefix": str(self.legacy),
            **env,
        }
        return subprocess.run(
            [self.shell, str(self.script), *args],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def current(self, slug: str = CLAUDE) -> str | None:
        link = self.root / slug / "current"
        return os.readlink(link) if link.is_symlink() else None

    def previous(self, slug: str = CLAUDE) -> str | None:
        link = self.root / slug / "previous"
        return os.readlink(link) if link.is_symlink() else None

    def versions(self, slug: str = CLAUDE) -> set[str]:
        d = self.root / slug
        return {p.name for p in d.iterdir() if not p.is_symlink()} if d.exists() else set()

    def status(self) -> dict[str, Any]:
        return json.loads((self.state / "harness-update.json").read_text())

    def npm_calls(self) -> list[str]:
        return self.npm_log.read_text().splitlines() if self.npm_log.exists() else []


# `sh` is dash on Debian-family images, which is what keeps the script POSIX;
# bash is the shell a base image might have as sh instead.
@pytest.fixture(params=["sh", "bash"])
def box(request, tmp_path: Path) -> Box:
    for tool in ("flock", request.param):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    return Box(tmp_path, request.param)


# --- installing and switching -------------------------------------------------


def test_first_install_switches_current(box: Box) -> None:
    result = box.run("6")

    assert result.returncode == 0, (box.state / "harness-update.log").read_text()
    for slug, binary in ((CLAUDE, "claude"), (OPENCODE, "opencode")):
        assert box.current(slug) == "1.0.0"
        out = subprocess.run(
            [str(box.root / slug / "current" / "bin" / binary)],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip() == "1.0.0"
    status = box.status()
    assert status["result"] == "ok"
    assert status["packages"]["@anthropic-ai/claude-code"]["current"] == "1.0.0"
    assert status["packages"]["opencode-ai"]["result"] == "ok"


def test_install_never_touches_the_home_volume_prefix_or_cache(box: Box) -> None:
    """A root install that honoured the image's npm prefix would write
    root-owned files into the dev user's home volume."""
    box.run("6")

    installs = [c for c in box.npm_calls() if c.startswith("npm install")]
    assert len(installs) == 2
    for call in box.npm_calls():
        assert "prefix=unset" in call
        assert "--cache" in call
        assert str(box.home) not in call.split(" prefix=")[0]
    assert f"--prefix {box.root}/{CLAUDE}/1.0.0" in installs[0]
    assert not box.legacy.exists()


def test_the_same_version_is_a_no_op(box: Box) -> None:
    box.run("6")
    box.npm_log.unlink()

    result = box.run("6", "force")

    assert result.returncode == 0
    assert not [c for c in box.npm_calls() if c.startswith("npm install")]
    assert box.current() == "1.0.0"
    assert box.previous() is None


def test_a_new_version_switches_and_keeps_the_old_as_previous(box: Box) -> None:
    box.run("6")
    result = box.run("6", "force", version="2.0.0")

    assert result.returncode == 0
    assert box.current() == "2.0.0"
    assert box.previous() == "1.0.0"
    assert box.versions() == {"1.0.0", "2.0.0"}


def test_a_failed_install_leaves_current_untouched(box: Box) -> None:
    box.run("6")

    result = box.run("6", "force", version="2.0.0", mode="fail")

    assert result.returncode != 0
    assert box.current() == "1.0.0"
    assert box.versions() == {"1.0.0"}, "a half-installed version was left behind"
    assert box.status()["result"] == "failed"
    assert "failed" in box.status()["packages"]["@anthropic-ai/claude-code"]["result"]


def test_a_binary_that_does_not_run_leaves_current_untouched(box: Box) -> None:
    box.run("6")

    result = box.run("6", "force", version="2.0.0", mode="broken")

    assert result.returncode != 0
    assert box.current() == "1.0.0"
    assert box.versions() == {"1.0.0"}
    assert "does not run" in box.status()["packages"]["@anthropic-ai/claude-code"]["result"]


def test_an_unresolvable_channel_changes_nothing(box: Box) -> None:
    box.run("6")
    box.npm_log.unlink()

    result = box.run("6", "force", version="2.0.0", mode="offline")

    assert result.returncode != 0
    assert box.current() == "1.0.0"
    assert not [c for c in box.npm_calls() if c.startswith("npm install")]


def test_a_channel_that_is_not_a_tag_or_version_is_refused(box: Box) -> None:
    result = box.run("6", "latest; touch pwned", "force")

    assert result.returncode == 2
    assert box.npm_calls() == []
    assert "refusing channel" in (box.state / "harness-update.log").read_text()


# --- when it runs at all --------------------------------------------------------


def test_a_recent_run_is_skipped_and_force_bypasses_it(box: Box) -> None:
    box.run("6")
    box.npm_log.unlink()

    skipped = box.run("6", version="2.0.0")
    assert skipped.returncode == 0
    assert box.npm_calls() == [], "ran again within the interval"
    assert box.current() == "1.0.0"

    forced = box.run("6", "force", version="2.0.0")
    assert forced.returncode == 0
    assert box.current() == "2.0.0"


def test_a_run_older_than_the_interval_goes_ahead(box: Box) -> None:
    box.run("6")
    (box.state / "harness-update.stamp").write_text(f"{int(time.time()) - 7 * 3600}\n")

    box.run("6", version="2.0.0")

    assert box.current() == "2.0.0"


def test_a_held_lock_exits_at_once(box: Box) -> None:
    box.state.mkdir(parents=True)
    with open(box.state / "harness-update.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = box.run("6", "force")

    assert result.returncode == 0
    assert box.npm_calls() == []
    assert not (box.state / "harness-update.json").exists()
    assert box.current() is None


# --- cleaning up ------------------------------------------------------------------


def test_legacy_cleanup_removes_exactly_what_the_old_updaters_left(box: Box) -> None:
    modules = box.legacy / "lib" / "node_modules"
    doomed = [
        modules / "@anthropic-ai" / "claude-code" / "cli.js",
        modules / "@anthropic-ai" / ".claude-code-Xa9fQ2" / "cli.js",
        modules / "opencode-ai" / "package.json",
        modules / ".opencode-ai-k2PzR" / "package.json",
    ]
    kept = [
        modules / "@anthropic-ai" / "sdk" / "index.js",
        modules / "@anthropic-ai" / "claude-code-sibling" / "index.js",
        modules / "pnpm" / "package.json",
        box.legacy / "bin" / "pnpm",
        box.legacy / "lib" / "notes.txt",
    ]
    for path in doomed + kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    (box.legacy / "bin" / "claude").symlink_to(
        "../lib/node_modules/@anthropic-ai/claude-code/cli.js"
    )
    (box.legacy / "bin" / "opencode").write_text("x")

    box.run("6")

    for path in doomed:
        assert not path.exists(), path
    assert not (modules / "@anthropic-ai" / "claude-code").exists()
    assert not (modules / "@anthropic-ai" / ".claude-code-Xa9fQ2").exists()
    assert not (box.legacy / "bin" / "claude").is_symlink()
    assert not (box.legacy / "bin" / "opencode").exists()
    for path in kept:
        assert path.exists(), f"{path} was not the cleanup's to remove"


def test_large_npm_caches_go_and_small_ones_stay(box: Box) -> None:
    big = box.home / "sessions" / "alice" / ".npm" / "_cacache"
    small = box.home / "sessions" / "bob" / ".npm" / "_cacache"
    shared = box.home / ".npm" / "_cacache"
    beside = box.home / "sessions" / "alice" / ".npm" / "_logs" / "debug.log"
    for d in (big, small, shared, beside.parent):
        d.mkdir(parents=True)
    (big / "blob").write_bytes(os.urandom(256 * 1024))
    (shared / "blob").write_bytes(os.urandom(256 * 1024))
    (small / "blob").write_bytes(os.urandom(8 * 1024))
    beside.write_text("x")

    box.run("6", MOONPHASE_NPM_CACHE_LIMIT_KB="64")

    assert not big.exists()
    assert not shared.exists()
    assert (small / "blob").exists()
    assert beside.exists(), "only the cache itself is removed"


def test_old_versions_are_pruned_keeping_current_and_previous(box: Box) -> None:
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        box.run("6", "force", version=version)

    for slug in (CLAUDE, OPENCODE):
        assert box.current(slug) == "3.0.0"
        assert box.previous(slug) == "2.0.0"
        assert box.versions(slug) == {"2.0.0", "3.0.0"}


def test_an_old_version_still_running_is_kept(box: Box) -> None:
    box.run("6", "force", version="1.0.0")
    # A session started two releases ago and never restarted.
    running = box.proc / "4242"
    running.mkdir()
    (running / "exe").symlink_to(box.root / CLAUDE / "1.0.0" / "bin" / "claude")

    box.run("6", "force", version="2.0.0")
    box.run("6", "force", version="3.0.0")

    assert box.versions(CLAUDE) == {"1.0.0", "2.0.0", "3.0.0"}
    assert box.versions(OPENCODE) == {"2.0.0", "3.0.0"}


# --- the launcher -------------------------------------------------------------------


def _stub_harness(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s|%s|%s\\n" {label} "$*" "$DISABLE_AUTOUPDATER" "$FROM_PROFILE" '
        '> "$HOME/ran"\n'
    )
    path.chmod(0o755)


def _run_launcher(tmp_path: Path, monkeypatch, *, extra_path: Path | None = None) -> str:
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    monkeypatch.setattr(harness_updates, "INSTALL_ROOT", str(tmp_path / "opt"))
    monkeypatch.setattr(harness_updates, "IMAGE_BIN_DIR", str(tmp_path / "image"))
    space = SessionSpace(home=str(tmp_path / "home"), workdir=str(tmp_path / "work"))
    Path(space.home).mkdir()
    Path(space.workdir).mkdir()
    # A profile trying to switch the updater back on must not win.
    Path(space.env_file).write_text("DISABLE_AUTOUPDATER=0\nFROM_PROFILE=yes\n")

    harness = registry.get("claude_code")
    spec = harness.launch_spec(resume=True)
    launcher = tmp_path / "launch.sh"
    launcher.write_text(sessions._launcher_script(spec.command, space, harness, spec.env))
    launcher.chmod(0o755)

    path = os.environ["PATH"]
    if extra_path is not None:
        path = f"{extra_path}:{path}"
    subprocess.run(
        ["bash", str(launcher)],
        env={"PATH": path},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return (Path(space.home) / "ran").read_text().strip()


def test_launcher_prefers_moonphases_own_copy(tmp_path: Path, monkeypatch) -> None:
    _stub_harness(tmp_path / "opt" / CLAUDE / "1.0.0" / "bin" / "claude", "managed")
    (tmp_path / "opt" / CLAUDE / "current").symlink_to("1.0.0")
    _stub_harness(tmp_path / "image" / "claude", "image")

    ran = _run_launcher(tmp_path, monkeypatch)

    assert ran == "managed|--continue|1|yes"


def test_launcher_falls_back_to_the_images_copy(tmp_path: Path, monkeypatch) -> None:
    _stub_harness(tmp_path / "image" / "claude", "image")
    # A `current` left dangling by a pruned or failed install is not executable.
    (tmp_path / "opt" / CLAUDE).mkdir(parents=True)
    (tmp_path / "opt" / CLAUDE / "current").symlink_to("9.9.9")

    ran = _run_launcher(tmp_path, monkeypatch)

    assert ran == "image|--continue|1|yes"


def test_launcher_falls_back_to_the_bare_name(tmp_path: Path, monkeypatch) -> None:
    _stub_harness(tmp_path / "path" / "claude", "path")

    ran = _run_launcher(tmp_path, monkeypatch, extra_path=tmp_path / "path")

    assert ran == "path|--continue|1|yes"


def test_launcher_exports_managed_env_after_the_env_file() -> None:
    harness = registry.get("claude_code")
    script = sessions._launcher_script(["claude"], SessionSpace(), harness, {"EXTRA": "a b"})

    sourced = script.index(". /home/dev/.moonphase-env")
    assert script.index("export DISABLE_AUTOUPDATER=1") > sourced
    assert script.index("export EXTRA='a b'") > sourced


def test_launcher_leaves_an_unmanaged_harness_alone() -> None:
    harness = registry.get("pydantic_ai")
    spec = harness.launch_spec()
    script = sessions._launcher_script(spec.command, SessionSpace(), harness, spec.env)

    assert "harness_bin" not in script
    assert "AUTOUPDATE" not in script
    assert f"\n{' '.join(spec.command)}\n" in script


def test_every_npm_harness_turns_its_own_updater_off() -> None:
    """Otherwise it installs a second copy into the home volume behind
    Moonphase's back, which is the problem this replaced."""
    for harness in registry.available():
        if harness.managed_package:
            assert harness.managed_binary
            assert harness.managed_env(), harness.kind


# --- triggers ------------------------------------------------------------------------


class _FakeSSH:
    def __init__(self, *, running: bool, fail_update: bool = False) -> None:
        self.running = running
        self.fail_update = fail_update
        self.commands: list[tuple[str, str | None]] = []

    async def run(self, conn, command, *, timeout: float = 60.0, stdin=None):
        self.commands.append((command, stdin))
        if "tmux has-session" in command:
            return CommandResult(0 if self.running else 1, "", "")
        if self.fail_update and "-u root" in command:
            return CommandResult(1, "", "Error response from daemon: container is paused")
        return CommandResult(0, "", "")

    def root_commands(self) -> list[str]:
        return [c for c, _ in self.commands if "-u root" in c]


async def test_a_new_session_starts_an_update_in_the_background(monkeypatch) -> None:
    fake = _FakeSSH(running=False)
    monkeypatch.setattr(ssh, "run", fake.run)

    created = await sessions.ensure_session(None, "mp-c", harness_kind="claude_code")

    assert created is True
    write, start = fake.root_commands()
    assert "docker exec -i -u root mp-c" in write
    assert harness_updates.SCRIPT_PATH in write
    script = next(stdin for c, stdin in fake.commands if c == write)
    assert script == harness_updates.update_script()
    assert start.startswith(f"docker exec -d -u root mp-c {harness_updates.SCRIPT_PATH} 6 ")
    assert start.endswith(" latest")
    launcher = next(stdin for c, stdin in fake.commands if "launch-moonphase.sh" in c)
    assert "export DISABLE_AUTOUPDATER=1" in launcher


async def test_an_already_running_session_does_not(monkeypatch) -> None:
    fake = _FakeSSH(running=True)
    monkeypatch.setattr(ssh, "run", fake.run)

    created = await sessions.ensure_session(None, "mp-c", harness_kind="claude_code")

    assert created is False
    assert fake.root_commands() == []


async def test_a_failed_update_never_costs_the_session(monkeypatch) -> None:
    fake = _FakeSSH(running=False, fail_update=True)
    monkeypatch.setattr(ssh, "run", fake.run)

    created = await sessions.ensure_session(None, "mp-c", harness_kind="claude_code")

    assert created is True
    assert fake.root_commands(), "the update was at least attempted"


async def test_zero_hours_disables_the_session_trigger(monkeypatch) -> None:
    fake = _FakeSSH(running=False)
    monkeypatch.setattr(ssh, "run", fake.run)
    monkeypatch.setattr(get_settings(), "moonphase_harness_update_hours", 0.0)

    await sessions.ensure_session(None, "mp-c", harness_kind="claude_code")

    assert fake.root_commands() == []


class _NullSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture
def sweep(monkeypatch):
    """A monitor over two servers, a hand-wound clock, and a record of triggers."""
    clock = {"now": 1000.0}
    triggered: list[str] = []

    async def fake_servers(_conn):
        return [
            {"id": "srv-1", "name": "one", "status": "online"},
            {"id": "srv-2", "name": "two", "status": "offline"},
        ]

    async def fake_projects(_conn):
        return [
            {"id": "p1", "server_id": "srv-1", "status": "running", "container_name": "c1"},
            {"id": "p2", "server_id": "srv-1", "status": "stopped", "container_name": "c2"},
            {"id": "p3", "server_id": "srv-2", "status": "running", "container_name": "c3"},
            {"id": "p4", "server_id": "srv-1", "status": "running", "container_name": None},
            {"id": "p5", "server_id": "srv-1", "status": "running", "container_name": "c5"},
        ]

    async def fake_target(_row):
        return object()

    async def fake_pool_get(_target):
        return object()

    async def fake_trigger(_conn, container):
        triggered.append(container)
        return True

    monitor = SessionMonitor()
    monkeypatch.setattr("moonphase.monitor.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    monkeypatch.setattr("moonphase.monitor.queries.list_servers", fake_servers)
    monkeypatch.setattr("moonphase.monitor.queries.list_projects", fake_projects)
    monkeypatch.setattr("moonphase.monitor.ssh.pool.get", fake_pool_get)
    monkeypatch.setattr("moonphase.monitor.harness_updates.trigger", fake_trigger)
    monkeypatch.setattr(monitor, "_target_for", fake_target)
    return monitor, clock, triggered


async def test_sweep_asks_running_containers_once_per_interval(sweep) -> None:
    monitor, clock, triggered = sweep

    assert await monitor.sweep_harnesses() == 2
    assert triggered == ["c1", "c5"], "only running projects on online servers"

    clock["now"] += 5 * 3600
    assert await monitor.sweep_harnesses() == 0

    clock["now"] += 3600
    assert await monitor.sweep_harnesses() == 2
    assert triggered == ["c1", "c5", "c1", "c5"]


async def test_sweep_is_off_at_zero_hours(sweep, monkeypatch) -> None:
    monitor, _clock, triggered = sweep
    monkeypatch.setattr(get_settings(), "moonphase_harness_update_hours", 0.0)

    assert await monitor.sweep_harnesses() == 0
    assert triggered == []


async def test_sweep_survives_an_unreachable_container(sweep, monkeypatch) -> None:
    monitor, _clock, triggered = sweep

    async def flaky_trigger(_conn, container):
        if container == "c1":
            raise ssh.SSHError("connection refused")
        triggered.append(container)
        return True

    monkeypatch.setattr("moonphase.monitor.harness_updates.trigger", flaky_trigger)

    assert await monitor.sweep_harnesses() == 1
    assert triggered == ["c5"]
