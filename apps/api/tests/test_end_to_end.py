"""End-to-end exercise of the chain the whole product depends on.

Drives the real `provision`, `docker_remote` and `sessions` code against a
throwaway sshd container standing in for a managed server, with the host Docker
socket mounted so project containers land on the host daemon — the same
sibling-container topology as a real box.

Run with a Docker daemon available:

    cd apps/api && .venv/bin/python -m pytest tests/test_end_to_end.py -v -s

Skipped automatically when Docker is unreachable.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import pytest

from moonphase import docker_remote, provision, sessions, ssh  # noqa: E402
from moonphase.ssh import SSHTarget  # noqa: E402

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 22222
SSH_USER = "deploy"
SSH_PASSWORD = "moonphase-test"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check
    )


def _docker_available() -> bool:
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", check=False).returncode == 0
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon is not reachable"
)


@pytest.fixture(scope="module")
def fake_server() -> str:
    """A container running sshd that Moonphase will treat as a managed server."""
    name = f"moonphase-test-server-{uuid.uuid4().hex[:8]}"
    _docker("rm", "-f", name, check=False)
    _docker(
        "run", "-d", "--name", name,
        "-p", f"127.0.0.1:{SSH_PORT}:22",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        FAKE_SERVER_IMAGE,
    )

    # Wait for sshd to bind rather than sleeping a fixed amount.
    deadline = time.time() + 45
    while time.time() < deadline:
        logs = _docker("logs", name, check=False)
        if "Server listening on 0.0.0.0" in (logs.stdout + logs.stderr):
            break
        time.sleep(0.4)
    else:
        logs = _docker("logs", name, check=False)
        _docker("rm", "-f", name, check=False)
        pytest.fail(f"fake server never started:\n{logs.stdout}\n{logs.stderr}")

    yield name

    _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_full_chain(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-test-{uuid.uuid4().hex[:8]}"
    workspace_volume = f"{container}-workspace"
    home_volume = f"{container}-home"

    try:
        # --- 1. bootstrap: password once, then key-only ---------------------
        result = await provision.bootstrap(
            server_id=server_id,
            server_name="test-server",
            host="127.0.0.1",
            port=SSH_PORT,
            ssh_user=SSH_USER,
            auth_mode="password_bootstrap",
            password=SSH_PASSWORD,
            auto_install_docker=False,
        )

        assert result.status == "online", f"bootstrap failed: {result.detail}"
        assert result.generated_private_key, "no key was generated"
        assert result.generated_public_key
        assert result.host_key_fingerprint
        assert result.host_key_fingerprint.startswith("SHA256:")
        assert result.docker_installed and result.docker_usable
        assert result.password_can_be_discarded
        print(f"\n  bootstrap ok — docker {result.docker_version}")
        print(f"  host key    {result.host_key_fingerprint}")

        # The generated key must work on its own, with no password anywhere.
        key_only = SSHTarget(
            server_id=server_id,
            host="127.0.0.1",
            port=SSH_PORT,
            username=SSH_USER,
            private_key=result.generated_private_key,
            known_host_key_fp=result.host_key_fingerprint,
        )
        conn = await ssh.pool.get(key_only)
        whoami = await ssh.run(conn, "whoami")
        assert whoami.stdout.strip() == SSH_USER
        print(f"  key-only login ok as {whoami.stdout.strip()}")

        # --- 2. host key pinning actually refuses a changed key -------------
        wrong = SSHTarget(
            server_id="bogus",
            host="127.0.0.1",
            port=SSH_PORT,
            username=SSH_USER,
            private_key=result.generated_private_key,
            known_host_key_fp="SHA256:" + "A" * 43,
        )
        with pytest.raises(ssh.HostKeyMismatch):
            await ssh.connect(wrong)
        print("  host key mismatch correctly refused")

        # --- 3. provision a project container -------------------------------
        await docker_remote.volume_create(conn, workspace_volume)
        await docker_remote.volume_create(conn, home_volume)
        container_id = await docker_remote.run_container(
            conn,
            name=container,
            image=RUNTIME_IMAGE,
            workspace_volume=workspace_volume,
            home_volume=home_volume,
        )
        assert container_id
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        info = await docker_remote.inspect(conn, container)
        assert info is not None and info.state == "running"
        print(f"  container {container} running ({container_id[:12]})")

        # --- 4. credentials land in the container ---------------------------
        from moonphase.harness import HarnessAuthMode, HarnessCredential
        from moonphase.harness import get as get_harness

        harness = get_harness("claude_code")
        await sessions.apply_credential(
            conn,
            container,
            harness,
            HarnessCredential(mode=HarnessAuthMode.API_KEY, api_key="sk-ant-test-not-real"),
        )
        assert await sessions.is_authenticated(conn, container, harness)
        perms = await docker_remote.exec_capture(
            conn, container, ["stat", "-c", "%a", sessions.ENV_FILE]
        )
        assert perms.stdout.strip() == "600", f"env file mode was {perms.stdout.strip()}"
        print("  credential written, mode 600, auth probe passes")

        # --- 4b. first-run config is seeded, then never clobbered -----------
        await sessions.seed_config(conn, container, harness)
        seeded = await docker_remote.exec_capture(
            conn, container, ["cat", "/home/dev/.claude.json"]
        )
        assert '"hasCompletedOnboarding": true' in seeded.stdout.replace(
            '"hasCompletedOnboarding":true', '"hasCompletedOnboarding": true'
        ), f"seed missing: {seeded.stdout!r}"

        # Simulate the harness recording its own state, then re-seed. The
        # user's data must survive: this file holds trust decisions and history.
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", "printf '%s' '{\"userState\":\"precious\"}' > /home/dev/.claude.json"],
        )
        await sessions.seed_config(conn, container, harness)
        preserved = await docker_remote.exec_capture(
            conn, container, ["cat", "/home/dev/.claude.json"]
        )
        assert "precious" in preserved.stdout, (
            f"re-seeding destroyed harness state: {preserved.stdout!r}"
        )
        print("  first-run config seeded and preserved across re-seed")

        # --- 5. tmux session with the harness inside ------------------------
        created = await sessions.ensure_session(
            conn, container, harness_kind="claude_code"
        )
        assert created is True
        assert await sessions.session_exists(conn, container)

        # Idempotency is what makes "open a project" safe to call repeatedly.
        created_again = await sessions.ensure_session(
            conn, container, harness_kind="claude_code"
        )
        assert created_again is False
        print("  tmux session created, second ensure_session was a no-op")

        # --- 6. the harness owns the terminal foreground group --------------
        # If the wrapper shell is still the pane's foreground process, it did
        # not enable job control, and Ctrl-C would kill the wrapper along with
        # the harness — taking the tmux session with it.
        deadline = time.time() + 40
        pane_command = ""
        while time.time() < deadline:
            listing = await docker_remote.exec_capture(
                conn, container,
                ["tmux", "list-panes", "-t", sessions.DEFAULT_SESSION,
                 "-F", "#{pane_current_command}"],
            )
            pane_command = listing.stdout.strip()
            if pane_command and pane_command not in {"sh", "bash", "launch-moonphase.sh"}:
                break
            await asyncio.sleep(1)
        assert pane_command not in {"sh", "bash", "launch-moonphase.sh"}, (
            f"wrapper shell is still the foreground process ({pane_command!r}); "
            "Ctrl-C would destroy the session"
        )
        print(f"  pane foreground process: {pane_command}")

        pane = await sessions.capture_pane(conn, container, lines=60)
        assert pane, "captured pane was empty"
        print(f"  captured {len(pane.splitlines())} lines from the pane")

        # --- 6b. Ctrl-C must not destroy the session ------------------------
        for _ in range(3):
            await docker_remote.exec_capture(
                conn, container,
                ["tmux", "send-keys", "-t", sessions.DEFAULT_SESSION, "C-c"],
            )
            await asyncio.sleep(0.4)
        await asyncio.sleep(1.5)
        assert await sessions.session_exists(conn, container), (
            "Ctrl-C killed the tmux session — the wrapper is taking terminal signals"
        )
        print("  session survived repeated Ctrl-C")

        # --- 7. attach a real PTY, exactly as the WebSocket bridge does -----
        attach = sessions.attach_command(container)
        process = await conn.create_process(
            attach, term_type="xterm-256color", term_size=(120, 32), encoding=None
        )
        try:
            chunk = await asyncio.wait_for(process.stdout.read(8192), timeout=20)
            assert chunk, "attached PTY produced no output"
            print(f"  PTY attach produced {len(chunk)} bytes")

            process.change_terminal_size(100, 40)
            await asyncio.sleep(0.5)
        finally:
            process.close()

        # --- 8. detaching must not kill the session -------------------------
        await asyncio.sleep(1.0)
        assert await sessions.session_exists(conn, container), (
            "session died when the client detached — this breaks the entire premise"
        )
        print("  session survived detach")

        # --- 9. send_keys reaches a session ---------------------------------
        # Against a plain shell, not the harness: Claude Code is a full-screen
        # TUI that consumes input into its own widgets, so keystrokes sent to
        # it are invisible to capture-pane. A scratch session tests the
        # transport honestly — that is the part Moonphase owns.
        probe_session = "probe"
        await docker_remote.exec_capture(
            conn, container,
            ["tmux", "new-session", "-d", "-s", probe_session, "-c", "/workspace", "bash"],
        )
        await asyncio.sleep(0.5)
        await sessions.send_keys(
            conn, container, "echo moonphase-test-marker", session=probe_session
        )
        await asyncio.sleep(1.5)
        after = await sessions.capture_pane(
            conn, container, session=probe_session, lines=80
        )
        assert "moonphase-test-marker" in after, (
            f"send_keys did not reach the pane; captured:\n{after[-500:]}"
        )
        print("  send_keys reached the pane")
        await sessions.kill_session(conn, container, probe_session)

        # The harness session must be untouched by all of that.
        assert await sessions.session_exists(conn, container)

        # --- 10. survives a container restart -------------------------------
        await docker_remote.stop(conn, container)
        await docker_remote.start(conn, container)
        await asyncio.sleep(2.0)
        assert not await sessions.session_exists(conn, container), (
            "tmux should be gone after a container restart"
        )
        recreated = await sessions.ensure_session(
            conn, container, harness_kind="claude_code"
        )
        assert recreated is True
        print("  session recreated cleanly after container restart")

    finally:
        cleanup = SSHTarget(
            server_id=server_id,
            host="127.0.0.1",
            port=SSH_PORT,
            username=SSH_USER,
            password=SSH_PASSWORD,
        )
        try:
            cleanup_conn, _ = await ssh.connect(cleanup)
            await docker_remote.remove(cleanup_conn, container)
            await docker_remote.volume_remove(cleanup_conn, workspace_volume)
            await docker_remote.volume_remove(cleanup_conn, home_volume)
            cleanup_conn.close()
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask failures
            print(f"  cleanup warning: {exc}")
        await ssh.pool.close_all()
