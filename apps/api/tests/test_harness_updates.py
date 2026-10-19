"""Keeping the harness CLIs current inside a project container.

The update script is the part that runs unattended as root in every container,
so it is run for real here — with `sh`, against temporary directories, and with
a stub `npm` on PATH standing in for the registry. The launcher is run for real
too, since "which binary did it start, with what environment" is a question
about a shell script, not about the Python that wrote it.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from moonphase import harness as registry
from moonphase import harness_updates, sessions, ssh
from moonphase.config import get_settings
from moonphase.harness import SessionSpace
from moonphase.monitor import SessionMonitor
from moonphase.ssh import CommandResult

CLAUDE = "anthropic-ai-claude-code"
OPENCODE = "opencode-ai"

# Answers `view pkg@latest` with $STUB_VERSION, `view pkg@other` only for the
# specs listed in $STUB_KNOWN (a 404 like npm's otherwise), and `install -g
# --prefix P ... pkg@ver` by creating P/bin/<binary> printing the version — or,
# per $STUB_MODE, cannot reach the registry, fails the install, or installs a
# binary that does not run.
STUB_NPM = r"""#!/bin/sh
echo "npm $* prefix=${npm_config_prefix:-unset} cwd=$(pwd)" >> "$STUB_LOG"
case $1 in
    view)
        [ "${STUB_MODE:-}" = offline ] && exit 1
        [ -n "${STUB_SLEEP:-}" ] && sleep "$STUB_SLEEP"
        tag=${2##*@}
        if [ "$tag" = latest ]; then
            echo "$STUB_VERSION"
        else
            case " ${STUB_KNOWN:-} " in
                *" $2 "*) echo "$tag" ;;
                *)
                    echo "npm error code E404" >&2
                    echo "npm error 404 No match found for version $tag" >&2
                    exit 1 ;;
            esac
        fi ;;
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
        # A stand-in for /etc/profile.d, which a test run must never write
        # to, even as root.
        self.profile = base / "profile.d" / "moonphase-harness.sh"
        self.npm_log = base / "npm.log"
        self.script = base / "moonphase-harness-update"
        for d in (self.home, self.proc, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        self.script.write_text(harness_updates.update_script())
        npm = self.bin / "npm"
        npm.write_text(STUB_NPM)
        npm.chmod(0o755)

    def run(
        self,
        *args: str,
        version: str = "1.0.0",
        mode: str = "",
        cwd: Path | None = None,
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        full_env = {
            "PATH": os.environ["PATH"],
            "MOONPHASE_HARNESS_ROOT": str(self.root),
            "MOONPHASE_STATE_DIR": str(self.state),
            "MOONPHASE_LEGACY_PREFIX": str(self.legacy),
            "MOONPHASE_DEV_HOME": str(self.home),
            "MOONPHASE_PROC": str(self.proc),
            "MOONPHASE_PROFILE_FILE": str(self.profile),
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
            cwd=cwd,
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

    def process(self, pid: int, argv: list[str], exe: Path | None = None) -> None:
        """A fake /proc/<pid>. Without `exe` it is what root sees for another
        user's process when it lacks CAP_SYS_PTRACE, as under Docker's
        default capabilities: a cmdline it can read and an exe it cannot."""
        d = self.proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
        if exe is not None:
            (d / "exe").symlink_to(exe)


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


def _break_current(box: Box) -> Path:
    """Leave `current` pointing at a version whose binary no longer runs."""
    binary = box.root / CLAUDE / "1.0.0" / "bin" / "claude"
    binary.chmod(0o644)
    return binary


def _tree(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")}


def test_a_broken_current_survives_a_failed_reinstall(box: Box) -> None:
    """Reinstalling over the directory `current` points at, and then failing,
    would leave `current` dangling."""
    box.run("6")
    _break_current(box)
    before = _tree(box.root / CLAUDE)

    result = box.run("6", "force", mode="fail")

    assert result.returncode != 0
    assert box.current() == "1.0.0"
    assert (box.root / CLAUDE / "current").resolve() == (box.root / CLAUDE / "1.0.0").resolve()
    assert _tree(box.root / CLAUDE) == before, "something was deleted or left behind"


def test_a_broken_current_is_reinstalled_beside_itself(box: Box, monkeypatch) -> None:
    box.run("6")
    broken = _break_current(box)

    result = box.run("6", "force")

    assert result.returncode == 0, (box.state / "harness-update.log").read_text()
    fresh = box.current()
    assert fresh is not None and fresh.startswith("1.0.0.r"), fresh
    assert box.previous() == "1.0.0"
    assert broken.exists(), "previous is kept, broken or not"
    _stub_harness(box.base / "image" / "claude", "image")
    launched = _launch(box.base, monkeypatch)
    assert launched.stdout.splitlines()[0] == "1.0.0", "the launcher ran the reinstall"

    # The sibling counts as that version: nothing more to do.
    box.npm_log.unlink()
    assert box.run("6", "force").returncode == 0
    assert box.current() == fresh
    assert not [c for c in box.npm_calls() if c.startswith("npm install")]


def test_a_leftover_at_the_versions_path_is_never_installed_over(box: Box) -> None:
    """An install killed halfway leaves the directory behind; something there
    may be in use, so the release goes beside it and prune clears it."""
    box.run("6")
    (box.root / CLAUDE / "2.0.0" / "lib" / "half").mkdir(parents=True)

    result = box.run("6", "force", version="2.0.0")

    assert result.returncode == 0
    fresh = box.current()
    assert fresh is not None and fresh.startswith("2.0.0.r"), fresh
    assert box.previous() == "1.0.0"
    assert box.versions() == {"1.0.0", fresh}
    assert box.current(OPENCODE) == "2.0.0"


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


def test_a_channel_one_package_lacks_falls_back_to_latest_for_it(box: Box) -> None:
    """One channel names both packages, so a Claude Code version number would
    otherwise leave OpenCode unable to update for as long as it is set."""
    result = box.run(
        "6", "2.1.290", version="1.5.0", STUB_KNOWN="@anthropic-ai/claude-code@2.1.290"
    )

    assert result.returncode == 0, (box.state / "harness-update.log").read_text()
    assert box.current(CLAUDE) == "2.1.290"
    assert box.current(OPENCODE) == "1.5.0"
    status = box.status()
    assert status["result"] == "ok"
    claude = status["packages"]["@anthropic-ai/claude-code"]
    assert claude["channel"] == "2.1.290"
    assert "fallback_from" not in claude
    opencode = status["packages"]["opencode-ai"]
    assert opencode["result"] == "ok"
    assert opencode["channel"] == "latest"
    assert opencode["fallback_from"] == "2.1.290"


def test_an_unreachable_registry_is_not_a_reason_to_leave_the_channel(box: Box) -> None:
    box.run("6", "2.1.290", STUB_KNOWN="@anthropic-ai/claude-code@2.1.290 opencode-ai@2.1.290")
    box.npm_log.unlink()

    result = box.run("6", "2.1.290", "force", mode="offline")

    assert result.returncode != 0
    assert not [c for c in box.npm_calls() if "@latest" in c]
    assert box.current(CLAUDE) == "2.1.290"
    assert "fallback_from" not in box.status()["packages"]["opencode-ai"]


def test_npm_runs_from_the_root_not_the_callers_directory(box: Box) -> None:
    """`docker exec` starts in the image's WORKDIR, the user's repo, whose
    .npmrc would pick the registry, auth and log location for a root job."""
    repo = box.base / "workspace"
    repo.mkdir()
    (repo / ".npmrc").write_text("registry=http://registry.invalid/\n")

    result = box.run("6", cwd=repo)

    assert result.returncode == 0
    calls = box.npm_calls()
    assert calls
    for call in calls:
        assert call.endswith(" cwd=/"), call


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


def test_the_interval_counts_from_when_the_last_run_started(box: Box) -> None:
    """Counted from the end, every interval would also stretch by however long
    the run took, and an ask landing one interval after the start would find
    it not yet due."""
    hours = f"{2 / 3600:.6f}"  # two seconds
    box.run(hours, STUB_SLEEP="1")  # one second per package: the run takes two

    box.npm_log.unlink()
    result = box.run(hours, version="2.0.0")

    assert result.returncode == 0
    assert box.npm_calls(), "skipped although an interval had passed since the last start"
    assert box.current() == "2.0.0"


def test_the_stamp_is_written_before_any_npm_work(box: Box) -> None:
    box.state.mkdir(parents=True)
    before = int(time.time())

    box.run("6", STUB_SLEEP="1")

    stamp = int((box.state / "harness-update.stamp").read_text())
    assert before <= stamp < before + 2


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


def test_a_running_version_is_kept_when_only_its_cmdline_can_be_read(box: Box) -> None:
    box.run("6", "force", version="1.0.0")
    # A native binary the launcher started by its versioned path.
    box.process(4242, [str(box.root / CLAUDE / "1.0.0" / "bin" / "claude"), "--continue"])
    # A node script: the interpreter is the exe, the versioned path an argument.
    script = box.root / OPENCODE / "1.0.0" / "lib" / "node_modules" / "opencode-ai" / "bin"
    box.process(4343, ["node", str(script / "opencode")], exe=Path("/usr/local/bin/node"))

    box.run("6", "force", version="2.0.0")
    box.run("6", "force", version="3.0.0")

    assert box.versions(CLAUDE) == {"1.0.0", "2.0.0", "3.0.0"}
    assert box.versions(OPENCODE) == {"1.0.0", "2.0.0", "3.0.0"}
    assert "still running" in (box.state / "harness-update.log").read_text()


def test_a_version_nothing_references_is_pruned(box: Box) -> None:
    box.run("6", "force", version="1.0.0")
    claude = box.root / CLAUDE
    # Near misses, none of them running 1.0.0 itself.
    box.process(10, [str(claude / "current" / "bin" / "claude")])
    box.process(11, [str(claude / "1.0.0-rc" / "bin" / "claude")])
    box.process(12, ["claude", "--continue"])
    box.process(13, [f"--prefix={claude}/1.0.0"], exe=Path("/usr/bin/env"))
    box.process(14, [])  # a kernel thread's cmdline is empty
    box.process(15, [str(claude / "1.0.0")])  # the directory itself, not inside it

    box.run("6", "force", version="2.0.0")
    box.run("6", "force", version="3.0.0")

    for slug in (CLAUDE, OPENCODE):
        assert box.versions(slug) == {"2.0.0", "3.0.0"}
        assert box.current(slug) == "3.0.0"
        assert box.previous(slug) == "2.0.0"


def test_current_and_previous_are_kept_with_nothing_running_them(box: Box) -> None:
    for version in ("1.0.0", "2.0.0"):
        box.run("6", "force", version=version)
    box.process(20, ["sleep", "infinity"])

    box.run("6", "force", version="2.0.0")

    assert box.versions(CLAUDE) == {"1.0.0", "2.0.0"}
    assert box.current() == "2.0.0"
    assert box.previous() == "1.0.0"


def test_prune_keeps_what_previous_resolves_to_however_it_is_spelled(box: Box) -> None:
    for version in ("1.0.0", "2.0.0"):
        box.run("6", "force", version=version)
    previous = box.root / CLAUDE / "previous"
    previous.unlink()
    previous.symlink_to(box.root / CLAUDE / "1.0.0")

    box.run("6", "force", version="2.0.0")

    assert box.versions(CLAUDE) == {"1.0.0", "2.0.0"}


def test_a_session_the_launcher_started_keeps_its_version(box: Box, monkeypatch) -> None:
    """The two halves together: what the launcher puts in argv is what prune
    finds, with no readable exe to fall back on."""
    _stub_harness(box.root / CLAUDE / "1.0.0" / "bin" / "claude", "managed")
    (box.root / CLAUDE / "current").symlink_to("1.0.0")
    _run_launcher(box.base, monkeypatch)
    argv0 = (box.home / "argv0").read_text().strip()
    box.process(4242, [argv0, "--continue"])

    box.run("6", "force", version="2.0.0")
    box.run("6", "force", version="3.0.0")

    assert box.versions(CLAUDE) == {"1.0.0", "2.0.0", "3.0.0"}


# --- the rest of the container --------------------------------------------------


def test_the_image_copy_is_never_touched() -> None:
    """It is the fallback when Moonphase's copy is missing or broken, and a
    link over it makes root's own `npm i -g` fail with EEXIST."""
    script = harness_updates.update_script()

    mentions = [line for line in script.splitlines() if harness_updates.IMAGE_BIN_DIR in line]
    assert mentions and all(line.startswith("PATH=") for line in mentions), mentions


def _login(box: Box, command: str, path: str) -> str:
    """What `command` prints in a login shell, which sources the profile."""
    out = subprocess.run(
        ["sh", "-c", f'. "$1"; {command}', "sh", str(box.profile)],
        env={"PATH": path}, capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip()


def test_login_shells_get_the_updater_switches(box: Box) -> None:
    """A tmux window or the pane's fallback `bash -l` is a login shell that
    never saw the launcher's exports."""
    box.run("6")

    lines = box.profile.read_text().splitlines()
    code = [line for line in lines if line and not line.startswith("#")]
    assert code[:2] == ["export DISABLE_AUTOUPDATER=1", "export OPENCODE_DISABLE_AUTOUPDATE=1"]
    assert code[-1] == "export PATH"
    assert box.profile.stat().st_mode & 0o777 == 0o644
    switches = _login(box, 'echo "$DISABLE_AUTOUPDATER$OPENCODE_DISABLE_AUTOUPDATE"', "/bin")
    assert switches == "11"


def test_login_shells_find_moonphases_copy_first(box: Box) -> None:
    image = box.base / "image"
    _stub_harness(image / "claude", "image")
    _stub_harness(image / "opencode", "image")
    box.run("6", version="2.0.0")
    path = f"{image}:/usr/bin:/bin"

    for slug, binary in ((CLAUDE, "claude"), (OPENCODE, "opencode")):
        found = _login(box, f"command -v {binary}", path)
        assert found == str(box.root / slug / "current" / "bin" / binary)
        assert _login(box, binary, path) == "2.0.0"


@pytest.mark.parametrize("breakage", ["dangling", "not executable"])
def test_login_shells_fall_back_to_the_images_copy(box: Box, breakage: str) -> None:
    """Checked at login rather than baked in, so a broken `current` costs a
    shell nothing — the image's copy is still where it always was."""
    image = box.base / "image"
    _stub_harness(image / "claude", "image")
    box.run("6")
    if breakage == "dangling":
        (box.root / CLAUDE / "current").unlink()
        (box.root / CLAUDE / "current").symlink_to("9.9.9")
    else:
        _break_current(box)
    path = f"{image}:/usr/bin:/bin"

    assert _login(box, "command -v claude", path) == str(image / "claude")
    assert _login(box, 'echo "$PATH"', path) == (
        f"{box.root / OPENCODE / 'current' / 'bin'}:{path}"
    ), "nothing added for the broken one"


# --- the launcher -------------------------------------------------------------------


def _stub_harness(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s|%s|%s\\n" {label} "$*" "$DISABLE_AUTOUPDATER" "$FROM_PROFILE" '
        '> "$HOME/ran"\n'
        'printf "%s\\n" "$0" > "$HOME/argv0"\n'
    )
    path.chmod(0o755)


def _copies_under(tmp_path: Path, monkeypatch) -> None:
    """Point Moonphase's install root and the image's bin dir into `tmp_path`."""
    monkeypatch.setattr(harness_updates, "INSTALL_ROOT", str(tmp_path / "opt"))
    monkeypatch.setattr(harness_updates, "IMAGE_BIN_DIR", str(tmp_path / "image"))


def _launch(
    tmp_path: Path, monkeypatch, *, extra_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    _copies_under(tmp_path, monkeypatch)
    space = SessionSpace(home=str(tmp_path / "home"), workdir=str(tmp_path / "work"))
    Path(space.home).mkdir(exist_ok=True)
    Path(space.workdir).mkdir(exist_ok=True)
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
    return subprocess.run(
        ["bash", str(launcher)],
        env={"PATH": path},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_launcher(tmp_path: Path, monkeypatch, *, extra_path: Path | None = None) -> str:
    _launch(tmp_path, monkeypatch, extra_path=extra_path)
    return (tmp_path / "home" / "ran").read_text().strip()


def test_launcher_prefers_moonphases_own_copy(tmp_path: Path, monkeypatch) -> None:
    _stub_harness(tmp_path / "opt" / CLAUDE / "1.0.0" / "bin" / "claude", "managed")
    (tmp_path / "opt" / CLAUDE / "current").symlink_to("1.0.0")
    _stub_harness(tmp_path / "image" / "claude", "image")

    ran = _run_launcher(tmp_path, monkeypatch)

    assert ran == "managed|--continue|1|yes"


def test_launcher_starts_the_managed_copy_by_its_versioned_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Root cannot read a session's /proc/<pid>/exe under Docker's default
    capabilities, so its argv is what tells prune which version it runs."""
    real = tmp_path / "opt" / CLAUDE / "1.0.0" / "bin" / "claude"
    _stub_harness(real, "managed")
    (tmp_path / "opt" / CLAUDE / "current").symlink_to("1.0.0")

    ran = _run_launcher(tmp_path, monkeypatch)

    assert ran == "managed|--continue|1|yes"
    assert (tmp_path / "home" / "argv0").read_text().strip() == os.path.realpath(real)


def test_launcher_runs_the_link_itself_when_it_cannot_resolve_it(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_harness(tmp_path / "opt" / CLAUDE / "1.0.0" / "bin" / "claude", "managed")
    (tmp_path / "opt" / CLAUDE / "current").symlink_to("1.0.0")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "readlink").write_text("#!/bin/sh\nexit 1\n")
    (broken / "readlink").chmod(0o755)

    ran = _run_launcher(tmp_path, monkeypatch, extra_path=broken)

    assert ran == "managed|--continue|1|yes"
    assert (tmp_path / "home" / "argv0").read_text().strip() == str(
        tmp_path / "opt" / CLAUDE / "current" / "bin" / "claude"
    )


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


# --- Moonphase's own calls ------------------------------------------------------------


def _copies(tmp_path: Path, monkeypatch, *, managed: bool) -> Path:
    """The image's claude, and Moonphase's when `managed`. Returns a HOME."""
    _copies_under(tmp_path, monkeypatch)
    _stub_harness(tmp_path / "image" / "claude", "image")
    if managed:
        _stub_harness(tmp_path / "opt" / CLAUDE / "1.0.0" / "bin" / "claude", "managed")
        (tmp_path / "opt" / CLAUDE / "current").symlink_to("1.0.0")
    home = tmp_path / "home"
    home.mkdir()
    return home


def _ran(argv: list[str], home: Path) -> str:
    """Run `argv` as `docker exec` would, from a PATH with no claude on it."""
    subprocess.run(
        argv, env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
    )
    return (home / "ran").read_text().strip()


@pytest.mark.parametrize("managed", [True, False])
def test_own_calls_run_what_a_session_would(tmp_path: Path, monkeypatch, managed: bool) -> None:
    home = _copies(tmp_path, monkeypatch, managed=managed)
    argv = harness_updates.managed_command(registry.get("claude_code"), ["claude", "a b", "c"])

    label = "managed" if managed else "image"
    assert _ran(argv, home) == f"{label}|a b c|1|"


def test_own_calls_leave_other_commands_alone() -> None:
    harness = registry.get("claude_code")
    assert harness_updates.managed_command(harness, ["tmux", "ls"]) == ["tmux", "ls"]
    assert harness_updates.managed_command(registry.get("pydantic_ai"), ["clai"]) == ["clai"]


@pytest.mark.parametrize("managed", [True, False])
async def test_mcp_list_runs_what_a_session_would(
    tmp_path: Path, monkeypatch, managed: bool
) -> None:
    from moonphase import docker_remote, runtime
    from moonphase.harness import HarnessKind
    from moonphase.routers import mcp_oauth

    home = _copies(tmp_path, monkeypatch, managed=managed)
    captured: list[list[str]] = []
    ctx = SimpleNamespace(
        harness=HarnessKind.CLAUDE_CODE, target=object(), container="mp-c", project={}
    )

    def returning(value):
        async def fake(*_a, **_k):
            return value
        return fake

    async def exec_capture(_conn, _container, command, **_kw):
        captured.append(command)
        return CommandResult(0, "", "")

    monkeypatch.setattr(runtime, "load_project_context", returning(ctx))
    monkeypatch.setattr(
        runtime, "load_session_space", returning((SessionSpace(), {"is_mine": True}))
    )
    monkeypatch.setattr(runtime, "load_session_profile", returning(None))
    monkeypatch.setattr(ssh.pool, "get", returning(object()))
    monkeypatch.setattr(sessions, "ensure_session", returning(False))
    monkeypatch.setattr(docker_remote, "exec_capture", exec_capture)

    await mcp_oauth._check(uuid4(), "mine", SimpleNamespace(claims={}))

    label = "managed" if managed else "image"
    assert _ran(captured[0], home) == f"{label}|mcp list|1|"


@pytest.mark.parametrize("managed", [True, False])
async def test_mcp_login_runs_what_a_session_would(
    tmp_path: Path, monkeypatch, managed: bool
) -> None:
    from moonphase import mcp_login

    home = _copies(tmp_path, monkeypatch, managed=managed)
    commands: list[str] = []

    async def run(_conn, command, *, timeout: float = 60.0, stdin=None):
        commands.append(command)
        if "capture-pane" in command:
            return CommandResult(0, "Visit https://auth.example/authorize?x=1\n", "")
        return CommandResult(0, "", "")

    real_sleep = asyncio.sleep
    monkeypatch.setattr(ssh, "run", run)
    monkeypatch.setattr(mcp_login.asyncio, "sleep", lambda _s: real_sleep(0))
    monkeypatch.setattr(mcp_login, "SESSION_TTL_SECONDS", 0)
    session = mcp_login.McpLoginSession(
        id="s1", org_id="o", project_id="p", session_name="mine", server_name="srv",
        home=str(home), container="mp-c", tmux_session="moonphase-mcp-login-s1",
    )

    await mcp_login._prepare(None, session)

    assert session.state == "awaiting_paste", session.detail
    start = next(c for c in commands if " new-session " in c)
    pane_command = shlex.split(start)[-1]
    label = "managed" if managed else "image"
    assert _ran(["sh", "-c", pane_command], home) == f"{label}|mcp login srv --no-browser|1|"


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


async def test_sweep_asks_running_containers_hourly_and_leaves_the_interval_to_them(
    sweep,
) -> None:
    monitor, clock, triggered = sweep

    assert await monitor.sweep_harnesses() == 2
    assert triggered == ["c1", "c5"], "only running projects on online servers"

    clock["now"] += 30 * 60
    assert await monitor.sweep_harnesses() == 0

    clock["now"] += 30 * 60
    assert await monitor.sweep_harnesses() == 2
    assert triggered == ["c1", "c5", "c1", "c5"]


@pytest.mark.parametrize("hours", [1.5, 0.25])
async def test_an_update_is_at_most_a_quarter_interval_late(
    sweep, monkeypatch, tmp_path: Path, hours: float
) -> None:
    """The container's last update can start at any moment relative to the
    asks — a session start, an API restart, a new container. Asking once an
    hour put 1.5h updates two hours apart, and asking once an interval let a
    short one slip by nearly a whole extra interval. The real script decides,
    on a clock shared with the monitor's."""
    if shutil.which("flock") is None:
        pytest.skip("flock is not installed")
    monitor, clock, _triggered = sweep
    monkeypatch.setattr(get_settings(), "moonphase_harness_update_hours", hours)
    interval = hours * 3600
    poll = get_settings().moonphase_monitor_interval
    real_date = shutil.which("date")

    for phase in (0.0, 0.3, 0.6, 0.9, 0.99):
        box = Box(tmp_path / f"phase-{phase}")
        date = box.bin / "date"
        date.write_text(
            '#!/bin/sh\n[ "$1" = +%s ] && { echo "$FAKE_NOW"; exit 0; }\n'
            f'exec {real_date} "$@"\n'
        )
        date.chmod(0o755)
        starts: list[int] = []

        def update(box: Box = box, starts: list[int] = starts) -> None:
            box.run(f"{hours:g}", FAKE_NOW=str(int(clock["now"])))
            stamp = int((box.state / "harness-update.stamp").read_text())
            if stamp not in starts:
                starts.append(stamp)

        async def trigger(_conn, container, update=update):
            if container == "c1":
                update()
            return True

        monkeypatch.setattr("moonphase.monitor.harness_updates.trigger", trigger)
        monitor._harnesses_checked.clear()

        # A session start runs one; the monitor first asks `phase` later.
        update()
        clock["now"] += phase * interval
        deadline = clock["now"] + 2 * interval
        while len(starts) < 2 and clock["now"] < deadline:
            await monitor.sweep_harnesses()
            clock["now"] += poll

        assert len(starts) == 2, (phase, starts)
        assert interval * 0.9 <= starts[1] - starts[0] <= interval * 1.25, (phase, starts)


async def test_sweep_is_off_at_zero_hours(sweep, monkeypatch) -> None:
    monitor, _clock, triggered = sweep
    monkeypatch.setattr(get_settings(), "moonphase_harness_update_hours", 0.0)

    assert await monitor.sweep_harnesses() == 0
    assert triggered == []


def _second_server(monkeypatch) -> None:
    """srv-1 as in the fixture, plus srv-3, online, running c6."""
    async def servers(_conn):
        return [
            {"id": "srv-1", "name": "one", "status": "online"},
            {"id": "srv-3", "name": "three", "status": "online"},
        ]

    async def projects(_conn):
        return [
            {"id": "p1", "server_id": "srv-1", "status": "running", "container_name": "c1"},
            {"id": "p5", "server_id": "srv-1", "status": "running", "container_name": "c5"},
            {"id": "p6", "server_id": "srv-3", "status": "running", "container_name": "c6"},
        ]

    monkeypatch.setattr("moonphase.monitor.queries.list_servers", servers)
    monkeypatch.setattr("moonphase.monitor.queries.list_projects", projects)


async def test_sweep_leaves_a_server_alone_while_it_is_backing_off(sweep) -> None:
    """The activity sweep's back-off already knows the server is not answering."""
    monitor, clock, triggered = sweep
    monitor._retry_after["srv-1"] = clock["now"] + 60

    assert await monitor.sweep_harnesses() == 0
    assert triggered == []
    assert monitor._harnesses_checked == {}

    clock["now"] += 61
    assert await monitor.sweep_harnesses() == 2
    assert triggered == ["c1", "c5"]


async def test_an_unreachable_server_costs_one_connect_per_pass(sweep, monkeypatch) -> None:
    monitor, clock, triggered = sweep
    _second_server(monkeypatch)
    connects: list[str] = []

    async def target_for(row):
        return row["server_id"]

    async def pool_get(server_id):
        connects.append(server_id)
        if server_id == "srv-1":
            raise ssh.SSHError("Could not connect to one:22: timed out")
        return object()

    monkeypatch.setattr(monitor, "_target_for", target_for)
    monkeypatch.setattr("moonphase.monitor.ssh.pool.get", pool_get)

    assert await monitor.sweep_harnesses() == 1
    assert connects == ["srv-1", "srv-3"], "one connect timeout for srv-1, not one per container"
    assert triggered == ["c6"]
    assert monitor._retry_after["srv-1"] > clock["now"]
    assert set(monitor._harnesses_checked) == {"c6"}

    # Backing off: not even a connect attempt.
    clock["now"] += 1
    assert await monitor.sweep_harnesses() == 0
    assert connects == ["srv-1", "srv-3"]

    # Back after the back-off, both its containers are asked at once rather
    # than an interval after the attempt that never reached them.
    clock["now"] += 120

    async def reachable(server_id):
        connects.append(server_id)
        return object()

    monkeypatch.setattr("moonphase.monitor.ssh.pool.get", reachable)
    assert await monitor.sweep_harnesses() == 2
    assert triggered == ["c6", "c1", "c5"]
    assert "srv-1" not in monitor._retry_after
    assert "srv-1" not in monitor._failures


@pytest.mark.parametrize(
    "error",
    [
        ssh.CommandFailed("Writing the script into c1 failed (exit 1): container is paused"),
        ssh.SSHError("Command timed out after 60.0s: docker exec -i -u root c1"),
        ssh.SSHError("The server is at its limit of concurrent SSH channels (open failed)."),
        ssh.SSHError("Command failed to execute: Connection lost"),
    ],
    ids=["refused", "timeout", "channels", "asyncssh"],
)
async def test_one_container_failing_holds_up_only_its_server_and_only_this_pass(
    sweep, monkeypatch, error: ssh.SSHError
) -> None:
    """Connecting worked, so the server is reachable: backing it off would
    also silence the activity sweep for it. Its other containers wait for the
    next pass, and the one that failed for its next ask."""
    monitor, clock, triggered = sweep
    _second_server(monkeypatch)
    attempts: list[str] = []
    backed_off: list[Any] = []

    async def trigger(_conn, container):
        attempts.append(container)
        if container == "c1":
            raise error
        triggered.append(container)
        return True

    monkeypatch.setattr("moonphase.monitor.harness_updates.trigger", trigger)
    monkeypatch.setattr(monitor, "_back_off", lambda *a: backed_off.append(a))

    assert await monitor.sweep_harnesses() == 1
    assert attempts == ["c1", "c6"], "the rest of srv-1 waits; srv-3 does not"
    assert backed_off == []
    assert monitor._retry_after == {}
    assert set(monitor._harnesses_checked) == {"c1", "c6"}

    clock["now"] += get_settings().moonphase_monitor_interval
    assert await monitor.sweep_harnesses() == 1
    assert attempts == ["c1", "c6", "c5"], "c1 is not retried on every pass"

    clock["now"] += 3600  # the next ask, at the default six hours
    await monitor.sweep_harnesses()
    assert attempts[3:] == ["c1", "c6"]
    assert backed_off == []
