"""The record has to match the machine.

A server reboots, or someone stops a container by hand, and Moonphase goes on
saying the project is running: it offers a terminal, a Stop button and a green
dot for something that no longer exists. Nothing else looks at every project
regularly, so if the monitor does not correct this, nothing does.

Run against the fake server, which gives the same sibling-container topology a
real managed server has.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from moonphase import docker_remote, provision, ssh
from moonphase import monitor as monitor_module
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 22922
SSH_USER = "deploy"
SSH_PASSWORD = "moonphase-test"


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


@pytest.fixture(scope="module")
def fake_server():
    name = f"moonphase-reconcile-{uuid.uuid4().hex[:8]}"
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
async def test_the_record_follows_the_machine(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-recon-{uuid.uuid4().hex[:8]}"
    written: list[tuple[str, str | None]] = []

    try:
        result = await provision.bootstrap(
            server_id=server_id, server_name="reconcile-test", host="127.0.0.1",
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

        await docker_remote.volume_create(conn, f"{container}-workspace")
        await docker_remote.volume_create(conn, f"{container}-home")
        await docker_remote.run_container(
            conn, name=container, image=RUNTIME_IMAGE,
            workspace_volume=f"{container}-workspace",
            home_volume=f"{container}-home",
        )
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        monitor = monitor_module.SessionMonitor()

        # Capture what it decides rather than writing to a database: the
        # decision is the thing under test.
        async def record(row, *, status, detail):
            written.append((status, detail))

        monitor._reconcile_project = record  # type: ignore[method-assign]

        async def settle(row, snapshot):
            return None

        monitor._settle = settle  # type: ignore[method-assign]

        row = {
            "id": "p1", "org_id": "o1", "name": "recon", "server_id": server_id,
            "harness": "claude_code", "container_name": container,
            "project_status": "running", "status_detail": None,
            "session_id": "s1", "tmux_session": "moonphase", "user_id": "u1",
            "activity": "working", "pane_digest": None, "notified_state": None,
        }

        # 1. Container up, no tmux in it — a host reboot exactly.
        await monitor._check_container(conn, container, [row])
        status, detail = written[-1]
        assert status == "running"
        assert detail is not None and "restarted" in detail, detail
        print(f"\n  after a restart with no agents: {status} — {detail}")

        # 2. Start a session; the project is simply running.
        await docker_remote.exec_capture(
            conn, container,
            ["tmux", "new-session", "-d", "-s", "moonphase", "-x", "200", "-y", "50", "bash"],
        )
        await monitor._check_container(conn, container, [row])
        status, detail = written[-1]
        assert (status, detail) == ("running", None), written[-1]
        print(f"  with an agent running: {status}")

        # 3. Stop the container, as a reboot or a careless hand would.
        await docker_remote.stop(conn, container)
        await monitor._check_container(conn, container, [row])
        status, detail = written[-1]
        assert status == "stopped"
        assert detail is not None and "container" in detail.lower(), detail
        print(f"  once stopped: {status} — {detail}")

        # 4. Remove it entirely. The project must not claim to be running.
        await docker_remote.remove(conn, container)
        await monitor._check_container(conn, container, [row])
        status, detail = written[-1]
        assert status == "stopped"
        assert detail is not None and "gone" in detail.lower(), detail
        print(f"  once removed: {status} — {detail}")

    finally:
        try:
            cleanup = SSHTarget(
                server_id=server_id, host="127.0.0.1", port=SSH_PORT,
                username=SSH_USER, password=SSH_PASSWORD,
            )
            conn_c, _ = await ssh.connect(cleanup)
            await docker_remote.remove(conn_c, container)
            await docker_remote.volume_remove(conn_c, f"{container}-workspace")
            await docker_remote.volume_remove(conn_c, f"{container}-home")
            conn_c.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  cleanup warning: {exc}")
        await ssh.pool.drop(server_id)
