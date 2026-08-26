"""Where the updater runs Compose, and why it matters.

Compose is run from inside a container, and the daemon it talks to resolves
bind-mount sources against the *host's* filesystem. A relative source like
`./docker/Caddyfile` became `/project/docker/Caddyfile` — the updater's own
view — which does not exist out there, so Docker created it as an empty
directory and the proxy died trying to mount a directory onto a file. The auth
container went the same way, and the instance was unreachable.

So Compose is run in a throwaway container holding the project at the same
path the host uses, which makes every relative path mean the same thing in
both places.

Exercised here with a stub `docker` on PATH, which records how it was called.
The behaviour against a real daemon was checked separately: with the fix a
file bind mount survives an update; without it, the host grows an empty
directory where the file should be and the service crash-loops.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
UPDATER = REPO / "docker" / "updater.sh"


@pytest.fixture(scope="module")
def stub_docker(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("stub")
    """A `docker` that answers `inspect` and logs everything else."""
    calls = tmp_path / "calls.log"
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        # `docker run ... docker compose ...` re-enters this script with the
        # inner command, which is how one stub answers both.
        'case "$*" in\n'
        '  *"config --services"*) printf "api\\nproxy\\nupdater\\n" ;;\n'
        '  *" ps --format"*) printf "api running\\nproxy running\\n" ;;\n'
        'esac\n'
        'case "$1" in\n'
        '  inspect) echo "/srv/moonphase" ;;\n'
        '  run) shift; exec "$0" "$@" ;;\n'
        'esac\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return calls


def _run_update_once(tmp_path: Path, stub_calls: Path) -> str:
    """Drive one pass of the updater's request loop."""
    project = tmp_path / "project"
    (project / "docker").mkdir(parents=True)
    (project / "docker-compose.yml").write_text("services: {}\n")
    updates = tmp_path / "updates"
    updates.mkdir()

    # Written after it starts, deliberately: whatever is already in the file
    # when the updater comes up is treated as already dealt with, so that a
    # restart does not re-run the update that caused it.
    env = {
        **os.environ,
        "PATH": f"{stub_calls.parent / 'bin'}:{os.environ['PATH']}",
        "MOONPHASE_PROJECT_DIR": str(project),
        "MOONPHASE_UPDATE_POLL": "1",
    }
    script = UPDATER.read_text().replace("/updates", str(updates))
    runner = tmp_path / "updater.sh"
    runner.write_text(script)

    # It watches forever by design, so it is started, given a request, and
    # then stopped once the pass has had time to finish.
    import time

    process = subprocess.Popen(
        ["sh", str(runner)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        time.sleep(2)
        (updates / "request").write_text("nonce\n")
        time.sleep(16)
    finally:
        process.terminate()
        process.wait(timeout=10)
    return stub_calls.read_text() if stub_calls.exists() else ""


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory, stub_docker: Path) -> str:
    """One pass of the loop, shared: it takes real seconds to run."""
    return _run_update_once(tmp_path_factory.mktemp("run"), stub_docker)


def test_compose_runs_with_the_project_at_its_host_path(calls: str) -> None:

    assert calls, "the updater never invoked docker"
    # The host path the stub reports, mounted onto itself and used as the
    # working directory — which is what makes relative binds resolve.
    assert "-v /srv/moonphase:/srv/moonphase" in calls
    assert "-w /srv/moonphase" in calls


def test_the_updater_does_not_bring_up_itself(calls: str) -> None:
    """Recreating the updater stops the command it is running. That is what
    left a half-updated stack the first time."""

    up_lines = [line for line in calls.splitlines() if " up -d" in line]
    assert up_lines, "no `up -d` was issued"
    for line in up_lines:
        assert " updater" not in f" {line} ", line
