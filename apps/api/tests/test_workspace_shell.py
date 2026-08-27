"""A login shell's logout script must not become the command's answer.

`workspaces._run` uses `bash -lc` so the harness's toolchain is on the PATH
the profile sets. A login shell also runs `~/.bash_logout` on the way out, and
Debian's — copied into every home — calls `clear_console`, which fails when
there is no console. A `docker exec` without a TTY has none.

With `set -e` still in force, that failure became the shell's exit status. A
script that ran perfectly reported exit 1 with nothing on either stream, which
is what "Restart harness" showed: an error banner with nothing after the colon.

It only bit a script that exits explicitly, so `ensure_worktree` failed on
exactly the path where it had nothing left to do — the worktree already
existed — which is every session that had been started once before.

Reproduced here with a throwaway HOME whose logout script fails, which is what
the container's does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs bash to reproduce a login shell"
)


@pytest.fixture
def home_with_failing_logout(tmp_path: Path) -> Path:
    """A home like the container's: a logout script that exits non-zero."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_logout").write_text("false\n")
    # Sourced by a login shell; empty is fine and keeps the shell quiet.
    (home / ".bash_profile").write_text("")
    return home


def _login_shell(home: Path, script: str) -> int:
    return subprocess.run(
        ["bash", "-lc", script],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHLVL": "1"},
        capture_output=True,
    ).returncode


def test_the_bug_this_guards_against_is_real(home_with_failing_logout: Path) -> None:
    """Without the wrapper, a successful script reports failure — and says
    nothing, because the logout script is quiet about it."""
    assert _login_shell(home_with_failing_logout, "set -e\nexit 0\n") != 0


def test_a_script_that_exits_cleanly_reports_success(
    home_with_failing_logout: Path,
) -> None:
    from moonphase.workspaces import wrap_for_login_shell

    wrapped = wrap_for_login_shell("set -e\nexit 0\n")

    assert _login_shell(home_with_failing_logout, wrapped) == 0


def test_a_real_failure_still_reports_it(home_with_failing_logout: Path) -> None:
    """The wrapper must not swallow failures along with the noise."""
    from moonphase.workspaces import wrap_for_login_shell

    assert _login_shell(home_with_failing_logout, wrap_for_login_shell("exit 3\n")) == 3
    assert (
        _login_shell(home_with_failing_logout, wrap_for_login_shell("set -e\nfalse\n"))
        == 1
    )


def test_output_survives_the_wrapper(home_with_failing_logout: Path) -> None:
    """An error message is the whole point of running these; the wrapper must
    not eat what the script wrote."""
    from moonphase.workspaces import wrap_for_login_shell

    result = subprocess.run(
        ["bash", "-lc", wrap_for_login_shell("echo out; echo err >&2; exit 1\n")],
        env={"HOME": str(home_with_failing_logout), "PATH": "/usr/bin:/bin", "SHLVL": "1"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "out" in result.stdout
    assert "err" in result.stderr
