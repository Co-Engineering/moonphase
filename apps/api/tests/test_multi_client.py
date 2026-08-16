"""Two devices on one project.

"Open it on my laptop and my phone" is the whole premise, so the behaviour when
two clients attach at once is a product question, not an implementation detail.
These pin down what actually happens: both see the same live session, either can
type, and the window follows whichever client is being used rather than
collapsing to the smaller one forever.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import pytest

from moonphase import docker_remote, provision, sessions, ssh
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 22922
SSH_USER = "deploy"
SSH_PASSWORD = "moonphase-test"

DESKTOP = (200, 50)
PHONE = (60, 30)


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
    name = f"moonphase-multi-{uuid.uuid4().hex[:8]}"
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
async def test_two_devices_share_one_session(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-multi-{uuid.uuid4().hex[:8]}"

    try:
        result = await provision.bootstrap(
            server_id=server_id,
            server_name="multi-test",
            host="127.0.0.1",
            port=SSH_PORT,
            ssh_user=SSH_USER,
            auth_mode="password_bootstrap",
            password=SSH_PASSWORD,
            auto_install_docker=False,
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

        # A plain shell rather than the harness: this is about tmux client
        # semantics, and a TUI redrawing would only add noise.
        await docker_remote.exec_capture(
            conn, container,
            ["tmux", "new-session", "-d", "-s", sessions.DEFAULT_SESSION,
             "-x", "200", "-y", "50", "bash"],
        )

        attach = sessions.attach_command(container)

        async def attach_client(size: tuple[int, int]):
            """Attach and read back the tty the wrapper announces."""
            process = await conn.create_process(
                attach, term_type="xterm-256color", term_size=size, encoding=None
            )
            buffer = b""
            deadline = time.time() + 10
            while sessions.TTY_MARKER.encode() not in buffer and time.time() < deadline:
                buffer += await asyncio.wait_for(process.stdout.read(512), timeout=5)
            marker = sessions.TTY_MARKER.encode()
            index = buffer.find(marker)
            assert index != -1, f"attach wrapper printed no tty marker: {buffer[:200]!r}"
            tty = buffer[index + len(marker) : buffer.find(b"\n", index)].decode().strip()
            return process, tty

        # Device one: a desktop.
        desktop, desktop_tty = await attach_client(DESKTOP)
        assert desktop_tty.startswith("/dev/"), desktop_tty
        await asyncio.sleep(2)

        clients = await docker_remote.exec_capture(
            conn, container,
            ["tmux", "list-clients", "-t", sessions.DEFAULT_SESSION,
             "-F", "#{client_width}x#{client_height}"],
        )
        assert len(clients.stdout.split()) == 1
        print(f"\n  one client: {clients.stdout.strip()}")

        # Device two: a phone, much narrower.
        phone, phone_tty = await attach_client(PHONE)
        assert phone_tty != desktop_tty
        await asyncio.sleep(2)

        clients = await docker_remote.exec_capture(
            conn, container,
            ["tmux", "list-clients", "-t", sessions.DEFAULT_SESSION,
             "-F", "#{client_width}x#{client_height}"],
        )
        sizes = clients.stdout.split()
        assert len(sizes) == 2, f"expected both devices attached, saw {sizes}"
        print(f"  two clients: {' and '.join(sizes)}")

        # Both are attached to the *same* session — not a second copy.
        session_count = await docker_remote.exec_capture(
            conn, container, ["tmux", "list-sessions", "-F", "#{session_name}"]
        )
        assert session_count.stdout.split() == [sessions.DEFAULT_SESSION], (
            f"attaching created extra sessions: {session_count.stdout.split()}"
        )
        print("  both attached to the same session, not a copy")

        # Typing on one device must be visible to the other: that is what
        # "the same session from anywhere" has to mean.
        marker = "marker-from-phone"
        phone.stdin.write(f"echo {marker}\n".encode())
        await asyncio.sleep(2)

        pane = await sessions.capture_pane(conn, container, lines=40)
        assert marker in pane, f"input from one device did not reach the session:\n{pane}"
        print("  input from one device is visible in the shared session")

        # `window-size latest` means geometry follows the client in use rather
        # than collapsing to the smallest forever, which is what would make a
        # phone attaching in the background ruin the desktop.
        option = await docker_remote.exec_capture(
            conn, container, ["tmux", "show-options", "-g", "window-size"]
        )
        assert "latest" in option.stdout, f"unexpected sizing policy: {option.stdout!r}"

        width = await docker_remote.exec_capture(
            conn, container,
            ["tmux", "list-windows", "-t", sessions.DEFAULT_SESSION,
             "-F", "#{window_width}x#{window_height}"],
        )
        print(f"  window sized to the latest client: {width.stdout.strip()}")

        # Closing the channel alone does NOT remove the client: `docker exec`
        # leaves the process it started running inside the container. This was
        # a real leak — every disconnect left a phantom client, and because
        # tmux sizes the window to the most recent one, a single phone visit
        # pinned the desktop narrow forever.
        phone.close()
        await asyncio.sleep(3)
        lingering = await sessions.list_clients(conn, container)
        assert phone_tty in lingering, (
            "test assumption wrong: docker exec now reaps its process, so the "
            "explicit detach below may no longer be needed"
        )
        print(f"  closing the channel leaves a phantom client ({len(lingering)} attached)")

        # Which is why the bridge detaches itself explicitly on the way out.
        await sessions.detach_client(conn, container, phone_tty)
        await asyncio.sleep(1)

        remaining = await sessions.list_clients(conn, container)
        assert phone_tty not in remaining, f"explicit detach failed: {remaining}"
        assert desktop_tty in remaining, "detaching one device dropped the other"
        print("  explicit detach removes exactly that client, leaving the other")

        # The session itself is untouched: detaching is not stopping.
        assert await sessions.session_exists(conn, container)

        # And the escape hatch clears everything without killing the session.
        cleared = await sessions.detach_all_clients(conn, container)
        assert cleared >= 1
        assert await sessions.list_clients(conn, container) == []
        assert await sessions.session_exists(conn, container)
        print("  detach-all clears every client and keeps the session alive")

        desktop.close()

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
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask failures
            print(f"  cleanup warning: {exc}")
        await ssh.pool.close_all()
