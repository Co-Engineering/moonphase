"""The preview proxy, against two real servers in one container.

The case forwarding cannot handle: a frontend and an API in the same container,
where the frontend's code asks for `http://localhost:8000`. Forwarded to a free
local port, that request leaves the browser and hits the *client's* port 8000.
Renumbering does not help — the address is the application's choice.

So this stands up a container with something on 8000 and something on 5173,
points a SOCKS client at the proxy, and checks that both names resolve to the
right process inside the container. Including a service on port 80, which no
amount of local forwarding could offer without root.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import httpx
import pytest

from moonphase import docker_remote, provision, socks, ssh
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 22822
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


# --- the protocol, without needing a container ------------------------------


def test_relay_command_tries_both_families_for_loopback() -> None:
    command = socks.relay_command("box", "127.0.0.1", 5173)
    assert "TCP4:127.0.0.1:5173" in command
    # Node binds `localhost` to ::1, so an IPv4-only relay hangs against Vite.
    # A browser asking for 127.0.0.1 means "this machine", not "this family".
    assert "TCP6:[::1]:5173" in command


def test_relay_command_tries_both_families_for_a_name() -> None:
    # socat resolves a name once and connects to the first address it gets; it
    # does not try the other family. `localhost` resolving to 127.0.0.1 first
    # would then miss a server listening only on ::1.
    command = socks.relay_command("box", "localhost", 5173)
    assert "TCP4:127.0.0.1:5173" in command and "TCP6:[::1]:5173" in command

    command = socks.relay_command("box", "db", 5432)
    assert "TCP4:db:5432" in command
    # Brackets are only for literal addresses; a name must not be wrapped.
    assert "TCP6:db:5432" in command
    assert "[db]" not in command


def test_relay_command_takes_a_routable_address_at_face_value() -> None:
    command = socks.relay_command("box", "10.1.2.3", 5432)
    assert "TCP4:10.1.2.3:5432" in command
    assert "TCP6" not in command


def test_relay_command_quotes_a_hostile_host() -> None:
    command = socks.relay_command("box", "a; rm -rf /", 80)
    assert "rm -rf /" in command
    assert "; rm -rf /;" not in command.replace("'", "")


# --- against a real container -----------------------------------------------


@pytest.fixture(scope="module")
def fake_server():
    name = f"moonphase-socks-{uuid.uuid4().hex[:8]}"
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
async def test_the_container_is_reachable_by_its_own_addresses(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-socks-{uuid.uuid4().hex[:8]}"

    try:
        result = await provision.bootstrap(
            server_id=server_id, server_name="socks-test", host="127.0.0.1",
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
            # Nothing published. The point is that nothing needs to be.
        )
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        # A frontend and an API, exactly as a project would have them: both on
        # loopback inside the container, on the ports their code expects.
        # Content first, and only then the servers. Creating directories in a
        # backgrounded job that a second backgrounded job immediately cd's into
        # is a race, and losing it means the API is simply not running — which
        # then looks exactly like a broken proxy.
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c",
             "mkdir -p /workspace/web /workspace/api && "
             "echo '<h1>frontend</h1>' > /workspace/web/index.html && "
             "echo '{\"ok\":true}' > /workspace/api/data.json && echo ready"],
            timeout=60,
        )
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c",
             "cd /workspace/web && nohup python3 -m http.server 5173 --bind 127.0.0.1 "
             ">/tmp/web.log 2>&1 & sleep 1; echo started"],
            timeout=60,
        )
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c",
             "cd /workspace/api && nohup python3 -m http.server 8000 --bind ::1 "
             ">/tmp/api.log 2>&1 & sleep 1; echo started"],
            timeout=60,
        )
        listening = await docker_remote.exec_capture(
            conn, container, ["sh", "-c", "ss -ltn | grep -c ':5173\\|:8000'"], timeout=30
        )
        assert listening.stdout.strip() == "2", (
            f"both servers should be up before proxying; saw {listening.stdout!r}"
        )
        # And something on a privileged port, which local forwarding could not
        # offer without root on the client.
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c",
             "mkdir -p /workspace/root && "
             "echo '<h1>port eighty</h1>' > /workspace/root/index.html && "
             "cd /workspace/root && nohup python3 -m http.server 80 --bind 127.0.0.1 "
             ">/tmp/eighty.log 2>&1 & sleep 1; echo started"],
            user="root", timeout=60,
        )

        proxy = await socks.registry.ensure(
            project_id="socks-test", container=container, target=target,
            bind="127.0.0.1",
        )
        assert proxy.local_port > 0
        print(f"\n  proxy listening on 127.0.0.1:{proxy.local_port}")

        transport = httpx.AsyncHTTPTransport(
            proxy=httpx.Proxy(f"socks5://127.0.0.1:{proxy.local_port}")
        )
        async with httpx.AsyncClient(transport=transport, timeout=30) as client:
            # The exact address a frontend's code would ask for.
            page = await client.get("http://localhost:5173/index.html")
            assert page.status_code == 200 and "frontend" in page.text
            print("  localhost:5173 reached the frontend")

            # The one that made forwarding useless: same name, different port,
            # and here it is bound to IPv6 loopback only.
            api = await client.get("http://localhost:8000/data.json")
            assert api.status_code == 200 and api.json() == {"ok": True}
            print("  localhost:8000 reached the API, on ::1")

            # 127.0.0.1 written out longhand must behave the same.
            explicit = await client.get("http://127.0.0.1:5173/index.html")
            assert explicit.status_code == 200 and "frontend" in explicit.text
            print("  127.0.0.1 works the same as localhost")

            # Privileged, and reachable without any privilege on this side.
            eighty = await client.get("http://localhost/index.html")
            assert eighty.status_code == 200 and "port eighty" in eighty.text
            print("  port 80 works with no root on the client")

            # Several at once, since a page load is never one request.
            responses = await asyncio.gather(
                *[client.get("http://localhost:5173/index.html") for _ in range(8)]
            )
            assert all(r.status_code == 200 for r in responses)
            print("  eight concurrent requests all served")

            # Something not listening must fail promptly, not hang.
            with pytest.raises(httpx.HTTPError):
                await client.get("http://localhost:9999/", timeout=20)
            print("  a closed port is refused rather than left hanging")

        # Nothing was published to make any of that work.
        info = await docker_remote.inspect(conn, container)
        assert info is not None
        print("  and the container published no ports at all")

        await socks.registry.close("socks-test")
        assert socks.registry.get("socks-test") is None

    finally:
        await socks.registry.close_all()
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
        await ssh.pool.drop(server_id)
