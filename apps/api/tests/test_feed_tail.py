"""Tailing a transcript from a real container.

Pure parsing is covered elsewhere; this is about the cursor. A feed that
duplicates events is annoying, one that drops them is a bug you only notice
when the message you were waiting for never arrives, and a partial line written
mid-poll is the case that produces both.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
import uuid

import pytest

from moonphase import docker_remote, provision, ssh, transcript
from moonphase.harness import get as get_harness
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 23022
SSH_USER = "deploy"
SSH_PASSWORD = "moonphase-test"

TRANSCRIPT_DIR = "/home/dev/.claude/projects/-workspace"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _docker_available() -> bool:
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", check=False).returncode == 0
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon is not reachable"
)


def _line(uid: str, text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uid,
            "timestamp": "2026-08-17T10:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


@pytest.fixture(scope="module")
def fake_server():
    name = f"moonphase-feed-{uuid.uuid4().hex[:8]}"
    _docker("rm", "-f", name, check=False)
    _docker(
        "run", "-d", "--name", name,
        "-p", f"127.0.0.1:{SSH_PORT}:22",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        FAKE_SERVER_IMAGE,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        logs = _docker("logs", name, check=False)
        if "Server listening on 0.0.0.0" in (logs.stdout + logs.stderr):
            break
        time.sleep(0.4)
    else:
        _docker("rm", "-f", name, check=False)
        pytest.fail("fake server never started")
    yield name
    _docker("rm", "-f", name, check=False)


@pytest.mark.asyncio(loop_scope="module")
async def test_tailing_never_loses_or_repeats_a_line(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-feed-{uuid.uuid4().hex[:8]}"
    harness = get_harness("claude_code")

    try:
        result = await provision.bootstrap(
            server_id=server_id, server_name="feed-test", host="127.0.0.1",
            port=SSH_PORT, ssh_user=SSH_USER, auth_mode="password_bootstrap",
            password=SSH_PASSWORD, auto_install_docker=False,
        )
        assert result.status == "online", result.detail

        target = SSHTarget(
            server_id=server_id, host="127.0.0.1", port=SSH_PORT, username=SSH_USER,
            private_key=result.generated_private_key,
            known_host_key_fp=result.host_key_fingerprint,
        )
        conn = await ssh.pool.get(target)

        await docker_remote.volume_create(conn, f"{container}-w")
        await docker_remote.volume_create(conn, f"{container}-h")
        await docker_remote.run_container(
            conn, name=container, image=RUNTIME_IMAGE,
            workspace_volume=f"{container}-w", home_volume=f"{container}-h",
        )
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        path = f"{TRANSCRIPT_DIR}/session-one.jsonl"

        async def append(text: str) -> None:
            await docker_remote.exec_capture(
                conn, container,
                ["sh", "-c",
                 f"mkdir -p {TRANSCRIPT_DIR} && "
                 f"printf '%s\\n' {shlex.quote(text)} >> {shlex.quote(path)}"],
                timeout=30,
            )

        # --- nothing written yet -------------------------------------------
        page = await transcript.read(conn, container, harness)
        assert page.available is False
        print("\n  no transcript yet reports unavailable")

        # --- cold open takes the tail ---------------------------------------
        for i in range(3):
            await append(_line(f"a{i}", f"message {i}"))

        page = await transcript.read(conn, container, harness)
        assert [e.text for e in page.events] == ["message 0", "message 1", "message 2"]
        assert page.cursor
        print(f"  cold open read {len(page.events)} events")

        # --- nothing new means nothing returned -----------------------------
        again = await transcript.read(conn, container, harness, cursor=page.cursor)
        assert again.events == []
        assert again.cursor == page.cursor
        print("  polling with no new lines returns nothing")

        # --- incremental ------------------------------------------------------
        await append(_line("a3", "message 3"))
        step = await transcript.read(conn, container, harness, cursor=page.cursor)
        assert [e.text for e in step.events] == ["message 3"], "expected only the new line"
        print("  a new line arrives exactly once")

        # --- a half-written line must not be consumed -------------------------
        # The harness appends as it streams; reading a partial line would either
        # drop it or, worse, parse a truncated record.
        # Split a genuine line so the two halves really do reassemble; a
        # hand-written prefix silently produces malformed JSON instead.
        full = _line("a4", "message 4")
        head, rest = full[:20], full[20:]

        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", f"printf '%s' {shlex.quote(head)} >> {shlex.quote(path)}"],
            timeout=30,
        )
        partial = await transcript.read(conn, container, harness, cursor=step.cursor)
        assert partial.events == [], "a partial line should be left for the next poll"

        # Completing it makes it readable, with nothing lost.
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", f"printf '%s\\n' {shlex.quote(rest)} >> {shlex.quote(path)}"],
            timeout=30,
        )
        completed = await transcript.read(conn, container, harness, cursor=partial.cursor)
        assert [e.text for e in completed.events] == ["message 4"], (
            f"partial line was lost or mangled: {[e.text for e in completed.events]}"
        )
        print("  a partial line is deferred, then read whole")

        # --- a new harness session starts a new file --------------------------
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c",
             f"sleep 1; printf '%s\\n' {shlex.quote(_line('b0', 'new session'))} "
             f"> {TRANSCRIPT_DIR}/session-two.jsonl"],
            timeout=30,
        )
        rotated = await transcript.read(conn, container, harness, cursor=completed.cursor)
        assert [e.text for e in rotated.events] == ["new session"], (
            "a newer transcript file should be picked up from its start"
        )
        assert "session-two" in rotated.cursor
        print("  a new transcript file is followed from its beginning")

    finally:
        try:
            cleanup = SSHTarget(
                server_id=server_id, host="127.0.0.1", port=SSH_PORT,
                username=SSH_USER, password=SSH_PASSWORD,
            )
            conn_c, _ = await ssh.connect(cleanup)
            await docker_remote.remove(conn_c, container)
            await docker_remote.volume_remove(conn_c, f"{container}-w")
            await docker_remote.volume_remove(conn_c, f"{container}-h")
            conn_c.close()
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask failures
            print(f"  cleanup warning: {exc}")
        await ssh.pool.close_all()
