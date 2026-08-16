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


@pytest.mark.asyncio(loop_scope="module")
async def test_client_counts_and_phantom_reaping(fake_server: str) -> None:
    """The two things that let phantom clients take a project down unseen.

    `client_counts` is what the UI reads to say "2 devices attached". It passed
    `-a` to `list-clients`, which that command does not accept, so tmux replied
    "unknown flag" and every count came back zero — no error anywhere, just a
    number that stayed at 0 while stale clients piled up behind it. Each one
    holds an SSH channel, sshd allows ten by default, and past that the project
    is unreachable including to whatever would have cleaned it up.
    """
    server_id = str(uuid.uuid4())
    container = f"mp-counts-{uuid.uuid4().hex[:8]}"
    name = "counted"

    try:
        result = await provision.bootstrap(
            server_id=server_id, server_name="counts-test", host="127.0.0.1",
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
        await docker_remote.exec_capture(
            conn, container,
            ["tmux", "new-session", "-d", "-s", name, "-x", "200", "-y", "50", "bash"],
        )

        assert (await sessions.client_counts(conn, container)).get(name) == 0

        attach = sessions.attach_command(container, name)
        processes = [
            await conn.create_process(
                attach, term_type="xterm-256color", term_size=(100, 30), encoding=None
            )
            for _ in range(2)
        ]
        await asyncio.sleep(2)

        counts = await sessions.client_counts(conn, container)
        assert counts.get(name) == 2, f"attached devices were not counted: {counts}"
        print(f"\n  two attached devices counted as {counts[name]}")

        # One belongs to a bridge we still own; the other is a phantom.
        ttys = await sessions.list_clients(conn, container, name)
        assert len(ttys) == 2
        sessions.register_client(container, ttys[0])

        reaped = await sessions.reap_phantom_clients(conn, container, name)
        assert reaped == 1, f"expected one phantom to be reaped, got {reaped}"
        assert await sessions.list_clients(conn, container, name) == [ttys[0]], (
            "the reaper detached a client a live bridge was using"
        )
        print("  the unowned one was detached and the live one left alone")

        # A restarted backend owns nothing, so everything it finds is stale.
        sessions.release_client(container, ttys[0])
        assert await sessions.reap_phantom_clients(conn, container, name) == 1
        assert (await sessions.client_counts(conn, container)).get(name) == 0
        assert await sessions.session_exists(conn, container, name), (
            "reaping clients must never touch the session itself"
        )
        print("  a restarted backend clears the lot, session untouched")

        for process in processes:
            process.close()
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
        # Drop this module's pooled connection rather than leaving it for a
        # later module to trip over: the loop it was made on dies with this
        # module, and closing it from another one raises.
        await ssh.pool.drop(server_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_channel_capacity_survives_more_than_one_connections_worth(
    fake_server: str,
) -> None:
    """The ceiling that made the terminal unreliable.

    sshd allows ten concurrent channels per connection, and asyncssh puts
    everything on one. So an attached terminal, a feed following a transcript,
    the activity monitor, port detection and a preview tunnel — which takes a
    channel for every TCP connection it carries, six of them for one page load
    — all competed for the same ten. Past that, opening a channel fails with
    "open failed" and the terminal stops working, with no clue as to why.

    The pool spreads channels over several connections instead. This holds open
    more than one connection's worth at once, which is the case that used to
    fail outright.
    """
    server_id = str(uuid.uuid4())
    target = SSHTarget(
        server_id=server_id, host="127.0.0.1", port=SSH_PORT,
        username=SSH_USER, password=SSH_PASSWORD,
    )

    # One connection's limit, established rather than assumed.
    single = await ssh.connect(target)
    conn_one = single[0]
    held = []
    try:
        for _ in range(30):
            try:
                held.append(await conn_one.create_process("sleep 60"))
            except Exception:
                break
        per_connection = len(held)
        print(f"\n  one connection allows {per_connection} concurrent channels")
        assert per_connection <= 16, "sshd is unusually permissive; test assumes a cap"
    finally:
        for process in held:
            process.close()
        conn_one.close()

    processes = []
    try:
        # Past what the fixed pool holds, so the growth path is exercised too:
        # under real load the answer to "every connection is full" has to be
        # another connection, not a broken terminal.
        wanted = per_connection * ssh.CONNECTIONS_PER_SERVER + 2
        for _ in range(wanted):
            processes.append(
                await ssh.pool.create_process(target, "sleep 60", encoding=None)
            )
        print(
            f"  the pool held {len(processes)} at once — "
            f"{wanted // per_connection}x a single connection"
        )
        assert len(processes) == wanted

        # And ordinary commands still get through with all of that outstanding.
        conn = await ssh.pool.get(target)
        result = await ssh.run(conn, "echo still-working", timeout=30)
        assert result.stdout.strip() == "still-working", (
            "a busy server stopped answering ordinary commands"
        )
        print("  and ordinary commands still went through")
    finally:
        for process in processes:
            process.close()
        await ssh.pool.drop(server_id)
