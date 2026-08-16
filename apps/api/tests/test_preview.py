"""Port auto-detection and tunnelling.

The promise is that a user never declares a port. These tests hold that to
account against a real container running a real server, including the case the
naive implementation gets wrong: a dev server bound to 127.0.0.1 inside the
container, which is what Vite and Rails do by default and which no amount of
`docker run -p` will reach.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import httpx
import pytest

from moonphase import docker_remote, preview, provision, ssh
from moonphase.preview import _parse_proc_net, _parse_ss
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 22522
SSH_USER = "deploy"
SSH_PASSWORD = "moonphase-test"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _docker_available() -> bool:
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", check=False).returncode == 0
    except FileNotFoundError:
        return False


# --- pure parsing, no Docker needed ----------------------------------------


def test_parse_ss_extracts_port_bind_and_process() -> None:
    line = (
        'LISTEN 0      511          127.0.0.1:5173       0.0.0.0:*    '
        'users:(("node",pid=42,fd=21))'
    )
    ports = _parse_ss(line)
    assert len(ports) == 1
    assert ports[0].port == 5173
    assert ports[0].bind == "127.0.0.1"
    assert ports[0].process == "node"
    assert ports[0].loopback_only is True


def test_parse_ss_handles_ipv6_wildcard() -> None:
    ports = _parse_ss("LISTEN 0 4096 [::]:3000 [::]:*")
    assert ports[0].port == 3000
    assert ports[0].loopback_only is False


def test_parse_proc_net_keeps_only_listening_sockets() -> None:
    # 0100007F = 127.0.0.1 little-endian, 1F90 = 8080. 0A is LISTEN, 01 is
    # ESTABLISHED and must be ignored.
    text = (
        "  sl  local_address rem_address   st\n"
        "   0: 0100007F:1F90 00000000:0000 0A\n"
        "   1: 0100007F:1F91 0100007F:ABCD 01\n"
    )
    ports = _parse_proc_net(text)
    assert [(p.port, p.bind) for p in ports] == [(8080, "127.0.0.1")]


# --- against a real container ----------------------------------------------

pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon is not reachable"
)


@pytest.fixture(scope="module")
def fake_server():
    name = f"moonphase-preview-{uuid.uuid4().hex[:8]}"
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
async def test_detects_and_tunnels_a_loopback_server(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-preview-{uuid.uuid4().hex[:8]}"

    try:
        result = await provision.bootstrap(
            server_id=server_id,
            server_name="preview-test",
            host="127.0.0.1",
            port=SSH_PORT,
            ssh_user=SSH_USER,
            auth_mode="password_bootstrap",
            password=SSH_PASSWORD,
            auto_install_docker=False,
        )
        assert result.status == "online", result.detail

        target = SSHTarget(
            server_id=server_id,
            host="127.0.0.1",
            port=SSH_PORT,
            username=SSH_USER,
            private_key=result.generated_private_key,
            known_host_key_fp=result.host_key_fingerprint,
        )
        conn = await ssh.pool.get(target)

        await docker_remote.volume_create(conn, f"{container}-workspace")
        await docker_remote.volume_create(conn, f"{container}-home")
        await docker_remote.run_container(
            conn,
            name=container,
            image=RUNTIME_IMAGE,
            workspace_volume=f"{container}-workspace",
            home_volume=f"{container}-home",
            # Deliberately no published ports: the whole point is reaching a
            # service that was never declared.
        )
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        # A server bound to loopback *inside* the container. Unreachable from
        # the host by any amount of port publishing.
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c",
             "echo '<h1>moonphase-preview-ok</h1>' > /workspace/index.html && "
             "cd /workspace && nohup python3 -m http.server 4321 --bind 127.0.0.1 "
             ">/tmp/http.log 2>&1 & echo started"],
            timeout=60,
        )

        # --- detection -----------------------------------------------------
        detected = []
        deadline = time.time() + 30
        while time.time() < deadline:
            await asyncio.sleep(1)
            detected = await preview.detect_ports(conn, container)
            if any(p.port == 4321 for p in detected):
                break

        found = next((p for p in detected if p.port == 4321), None)
        assert found is not None, f"port 4321 not detected; saw {detected}"
        assert found.loopback_only is True, f"expected loopback bind, got {found.bind}"
        print(f"\n  detected {found.port} bound to {found.bind} ({found.process})")

        # Nothing the user declared should show up as required config.
        assert 22 not in [p.port for p in detected], "ssh should be filtered out"

        # --- tunnelling ----------------------------------------------------
        tunnel = await preview.registry.ensure(
            project_id="test-project", container=container, port=4321, target=target
        )
        assert tunnel.local_port > 0
        print(f"  tunnel listening on 127.0.0.1:{tunnel.local_port}")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"http://127.0.0.1:{tunnel.local_port}/index.html")
        assert response.status_code == 200, response.text
        assert "moonphase-preview-ok" in response.text
        print("  fetched page through the tunnel")

        # Repeated requests must reuse the listener, not leak channels.
        async with httpx.AsyncClient(timeout=20) as client:
            for _ in range(3):
                again = await client.get(f"http://127.0.0.1:{tunnel.local_port}/index.html")
                assert again.status_code == 200

        # ensure() is idempotent: the same port must not open a second listener.
        same = await preview.registry.ensure(
            project_id="test-project", container=container, port=4321, target=target
        )
        assert same.local_port == tunnel.local_port

        await preview.registry.close("test-project", 4321)
        assert preview.registry.get("test-project", 4321) is None
        print("  tunnel closed cleanly")

        # --- an IPv6-only listener ------------------------------------------
        # Node binds `localhost` to ::1 on a modern system, so Vite — the most
        # likely thing behind a preview — listens on IPv6 loopback only. A
        # relay hardcoded to TCP:127.0.0.1 cannot reach it, and hangs rather
        # than failing, which reads as a broken preview.
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c",
             "echo '<h1>ipv6-only</h1>' > /workspace/six.html && cd /workspace && "
             "nohup python3 -m http.server 4322 --bind ::1 >/tmp/six.log 2>&1 & echo ok"],
            timeout=60,
        )

        found_six = None
        deadline = time.time() + 30
        while time.time() < deadline:
            await asyncio.sleep(1)
            found_six = next(
                (p for p in await preview.detect_ports(conn, container) if p.port == 4322),
                None,
            )
            if found_six:
                break
        assert found_six is not None, "IPv6-only listener was not detected"
        assert found_six.bind in ("::1", "::"), f"unexpected bind {found_six.bind}"
        print(f"  detected an IPv6-only listener on {found_six.bind}")

        six_tunnel = await preview.registry.ensure(
            project_id="test-project", container=container, port=4322, target=target
        )
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"http://127.0.0.1:{six_tunnel.local_port}/six.html"
            )
        assert response.status_code == 200, response.text
        assert "ipv6-only" in response.text
        print("  reached it through the tunnel")
        await preview.registry.close("test-project", 4322)

    finally:
        await preview.registry.close_all()
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
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask failures
            print(f"  cleanup warning: {exc}")
        await ssh.pool.close_all()
