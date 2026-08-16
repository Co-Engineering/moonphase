"""Automatic preview tunnels.

Asking a user to declare which port their dev server will use is backwards:
they usually do not know yet, it changes when a framework picks the next free
port, and it is one more thing to get wrong before anything works. Moonphase
looks instead.

Detection reads the listening sockets inside the container. Because it runs in
the container's own network namespace, it sees ports bound to `127.0.0.1` —
which is what Vite, Next and Rails bind by default — not just published ones.

Reaching them uses the same trick. Rather than republishing container ports
(impossible without knowing them at `docker run` time), each connection opens
`docker exec -i … socat` into the container and pipes bytes over the existing
SSH connection. That works for any bind address, needs no `-p`, and survives
the dev server restarting on a different port.

Each shared port gets its own listener on the backend rather than a path under
a shared origin. A dev server behind a path prefix serves absolute asset URLs
that 404, and its HMR websocket connects to the wrong place; a dedicated origin
has neither problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shlex
from dataclasses import dataclass, field

import asyncssh

from . import docker_remote, ssh
from .config import get_settings
from .ssh import SSHError, SSHTarget

log = logging.getLogger(__name__)

# Ports that are never a preview: the harness's own tooling and common
# infrastructure the user did not start to look at in a browser.
IGNORED_PORTS = {22, 25, 53}



@dataclass
class DetectedPort:
    port: int
    bind: str
    process: str | None = None

    @property
    def loopback_only(self) -> bool:
        return self.bind in {"127.0.0.1", "::1", "localhost"}


def _relay_command(container: str, port: int) -> str:
    """Pipe a connection into the container, over IPv4 or IPv6.

    Trying both is not belt-and-braces: Node binds `localhost` to ::1 on a
    modern system, so Vite — the single most likely thing behind a preview —
    listens on IPv6 loopback only. `TCP:127.0.0.1` cannot reach it and hangs
    rather than failing, which reads as a broken preview.

    A `::` wildcard listener accepts IPv4-mapped connections, so IPv4 first
    covers almost everything and the fallback only runs for a genuinely
    IPv6-only bind. socat exits without touching stdio when it cannot connect,
    so the second attempt starts clean.
    """
    inner = (
        f"socat -T 3600 STDIO TCP4:127.0.0.1:{port} 2>/dev/null "
        f"|| exec socat -T 3600 STDIO TCP6:[::1]:{port}"
    )
    return (
        f"docker exec -i {shlex.quote(container)} sh -c " + shlex.quote(inner)
    )


@dataclass
class Tunnel:
    """A backend-local listener that forwards into a container port."""

    project_id: str
    container: str
    container_port: int
    target: SSHTarget
    local_port: int = 0
    _server: asyncio.AbstractServer | None = field(default=None, repr=False)
    _connections: set[asyncio.Task] = field(default_factory=set, repr=False)

    async def start(self, bind: str) -> None:
        self._server = await asyncio.start_server(self._handle, bind, 0)
        sock = self._server.sockets[0]
        self.local_port = sock.getsockname()[1]
        log.info(
            "preview tunnel %s:%s -> %s:%s",
            bind, self.local_port, self.container, self.container_port,
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Bridge one TCP connection into the container."""
        # A page load opens several TCP connections at once and each takes a
        # channel for as long as it lives, which makes previews the largest
        # consumer of a server's channel budget by some margin. Going through
        # the pool spreads them and retries a refusal on another connection.
        command = _relay_command(self.container, self.container_port)
        try:
            process = await ssh.pool.create_process(
                self.target, command, encoding=None
            )
        except (SSHError, asyncssh.Error) as exc:
            log.warning("preview: could not open channel: %s", exc)
            writer.close()
            return

        async def to_container() -> None:
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    process.stdin.write(data)
                    await process.stdin.drain()
            except (ConnectionResetError, BrokenPipeError, asyncssh.Error):
                pass
            finally:
                with contextlib.suppress(Exception):
                    process.stdin.write_eof()

        async def to_client() -> None:
            try:
                while True:
                    data = await process.stdout.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, asyncssh.Error):
                pass

        pump = asyncio.gather(to_container(), to_client(), return_exceptions=True)
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            await pump
        finally:
            if task is not None:
                self._connections.discard(task)
            with contextlib.suppress(Exception):
                process.close()
            with contextlib.suppress(Exception):
                writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for task in list(self._connections):
            task.cancel()
        self._connections.clear()


class TunnelRegistry:
    def __init__(self) -> None:
        self._tunnels: dict[tuple[str, int], Tunnel] = {}
        self._lock = asyncio.Lock()

    async def ensure(
        self, *, project_id: str, container: str, port: int, target: SSHTarget
    ) -> Tunnel:
        key = (project_id, port)
        async with self._lock:
            existing = self._tunnels.get(key)
            if existing is not None:
                return existing
            settings = get_settings()
            tunnel = Tunnel(
                project_id=project_id,
                container=container,
                container_port=port,
                target=target,
            )
            await tunnel.start(settings.moonphase_preview_bind)
            self._tunnels[key] = tunnel
            return tunnel

    def get(self, project_id: str, port: int) -> Tunnel | None:
        return self._tunnels.get((project_id, port))

    def for_project(self, project_id: str) -> dict[int, Tunnel]:
        return {
            port: tunnel
            for (pid, port), tunnel in self._tunnels.items()
            if pid == project_id
        }

    async def close(self, project_id: str, port: int) -> None:
        async with self._lock:
            tunnel = self._tunnels.pop((project_id, port), None)
        if tunnel is not None:
            await tunnel.stop()

    async def close_project(self, project_id: str) -> None:
        async with self._lock:
            keys = [k for k in self._tunnels if k[0] == project_id]
            tunnels = [self._tunnels.pop(k) for k in keys]
        for tunnel in tunnels:
            await tunnel.stop()

    async def close_all(self) -> None:
        async with self._lock:
            tunnels = list(self._tunnels.values())
            self._tunnels.clear()
        for tunnel in tunnels:
            await tunnel.stop()


registry = TunnelRegistry()


def _parse_hex_address(value: str) -> tuple[str, int] | None:
    """Decode a /proc/net/tcp local_address field into (ip, port)."""
    if ":" not in value:
        return None
    raw_ip, raw_port = value.rsplit(":", 1)
    try:
        port = int(raw_port, 16)
    except ValueError:
        return None

    if len(raw_ip) == 8:
        octets = [int(raw_ip[i : i + 2], 16) for i in (6, 4, 2, 0)]
        return ".".join(str(o) for o in octets), port
    if len(raw_ip) == 32:
        if raw_ip == "0" * 32:
            return "::", port
        if raw_ip.endswith("01000000") and raw_ip[:24] == "0" * 24:
            return "::1", port
        return "::", port
    return None


def _parse_proc_net(text: str) -> list[DetectedPort]:
    """Parse /proc/net/tcp{,6}, keeping only sockets in LISTEN (state 0A)."""
    found: list[DetectedPort] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A":
            continue
        parsed = _parse_hex_address(parts[1])
        if parsed is None:
            continue
        ip, port = parsed
        found.append(DetectedPort(port=port, bind=ip))
    return found


def _parse_ss(text: str) -> list[DetectedPort]:
    """Parse `ss -ltnpH`, which also tells us which process is listening."""
    found: list[DetectedPort] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        if ":" not in local:
            continue
        host, _, raw_port = local.rpartition(":")
        try:
            port = int(raw_port)
        except ValueError:
            continue
        process = None
        match = re.search(r'users:\(\("([^"]+)"', line)
        if match:
            process = match.group(1)
        found.append(DetectedPort(port=port, bind=host.strip("[]") or "0.0.0.0", process=process))
    return found


# Asked of every detected port, inside the container, in one round trip.
# Sequential probing of a handful of ports at a second apiece is long enough to
# feel broken, hence the thread pool.
_PROBE_SCRIPT = r"""
import concurrent.futures, json, re, socket, sys

TIMEOUT = 1.2
REQUEST = b"GET / HTTP/1.0\r\nHost: localhost\r\nUser-Agent: moonphase-probe\r\n\r\n"


def ask(port):
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        try:
            sock.connect((address, port))
            sock.sendall(REQUEST)
            data = b""
            while len(data) < 8192:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                data += chunk
        except Exception:
            continue
        finally:
            try:
                sock.close()
            except Exception:
                pass
        if data:
            return classify(data)
    return {"kind": "unknown", "title": None}


def classify(data):
    head, _, body = data.partition(b"\r\n\r\n")
    headers = head.lower()
    lowered = body.lower()
    kind = "unknown"
    if b"text/html" in headers or b"<html" in lowered or b"<!doctype html" in lowered:
        kind = "page"
    elif b"application/json" in headers:
        kind = "api"
    title = None
    match = re.search(rb"<title[^>]*>(.{0,200}?)</title>", body, re.I | re.S)
    if match:
        text = " ".join(match.group(1).decode("utf-8", "replace").split())
        title = text[:60] or None
    return {"kind": kind, "title": title}


ports = [int(value) for value in sys.argv[1:]]
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    results = dict(zip(ports, pool.map(ask, ports)))
print(json.dumps({str(port): value for port, value in results.items()}))
"""


async def probe_services(
    conn: asyncssh.SSHClientConnection, container: str, ports: list[int]
) -> dict[int, dict[str, str | None]]:
    """Ask each port what it serves, rather than guessing from its number.

    Opening the lowest-numbered port is a guess that happens to be right when
    the frontend's number sorts below the API's and wrong the moment it does
    not — a page of JSON where the app should be. One HTTP request answers the
    question directly: whatever returns HTML is the thing a person meant to
    open, and its <title> is a better label than a port number.
    """
    if not ports:
        return {}
    result = await docker_remote.exec_capture(
        conn,
        container,
        ["python3", "-c", _PROBE_SCRIPT, *[str(port) for port in ports]],
        timeout=30,
    )
    if not result.ok or not result.stdout.strip():
        log.debug("preview: could not probe services: %s", result.stderr[:200])
        return {}
    try:
        raw = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return {int(port): value for port, value in raw.items()}


# A file server's index is HTML and therefore a "page", but it is plainly not
# the application — and this is what its title always looks like.
_AUTOINDEX = re.compile(r"^(directory listing|index of)\b", re.IGNORECASE)


def rank(port: int, kind: str | None, title: str | None = None) -> tuple[int, ...]:
    """Sort key for "which of these did the user mean to open".

    A page beats anything; an API loses to everything, because landing on raw
    JSON looks like the app is broken.

    Among pages, a directory listing loses. It is HTML and so passes the first
    test, but nobody previews a project to look at a file index — and when two
    things both serve HTML, falling through to the lower port number is exactly
    the guess this ranking exists to replace.

    Port 80 sinks slightly: it is usually infrastructure someone else put there
    rather than the thing being built.
    """
    order = {"page": 0, "unknown": 1, "api": 2}.get(kind or "unknown", 1)
    autoindex = 1 if title and _AUTOINDEX.match(title.strip()) else 0
    return (order, autoindex, 1 if port == 80 else 0, port)


async def detect_ports(
    conn: asyncssh.SSHClientConnection, container: str
) -> list[DetectedPort]:
    """What is listening inside the container right now.

    Runs as root so `ss` can attribute sockets to processes; falls back to
    parsing /proc directly when iproute2 is missing from an older image.
    """
    result = await docker_remote.exec_capture(
        conn, container, ["ss", "-ltnpH"], user="root", timeout=30
    )
    detected: list[DetectedPort]
    if result.ok and result.stdout.strip():
        detected = _parse_ss(result.stdout)
    else:
        combined = await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"],
            user="root", timeout=30,
        )
        detected = _parse_proc_net(combined.stdout) if combined.ok else []

    # Collapse the dual-stack case: a server on both 0.0.0.0 and :: is one
    # thing to the user, and preferring the IPv4 row keeps the display honest.
    best: dict[int, DetectedPort] = {}
    for item in detected:
        if item.port in IGNORED_PORTS:
            continue
        current = best.get(item.port)
        if current is None:
            best[item.port] = item
            continue
        if current.process is None and item.process is not None:
            best[item.port] = item
        elif not current.loopback_only and item.loopback_only:
            # Keep the broader bind; it is the one that matters for reachability.
            continue

    return sorted(best.values(), key=lambda p: p.port)
