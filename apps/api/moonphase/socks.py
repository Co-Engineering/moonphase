"""A SOCKS5 proxy whose every connection lands inside one project container.

Forwarding ports one at a time does not work, and the reason is worth stating
because it looks like it should.

A project runs a frontend on 5173 and an API on 8000. Forward each to a free
local port and the frontend is reachable — but the page runs in a browser on
the *client* machine, so when its code asks for `http://localhost:8000` it gets
the client's port 8000, which is not the API and is quite possibly something
else. Renumbering cannot fix it: the app decides what address to ask for, and
it asks for the one it was written with. Preserving the numbers only works
while they happen to be free, breaks for a second project, and is impossible
for anything below 1024.

The fix is to stop translating addresses and change what `localhost` means. A
browser pointed at this proxy resolves every name and port inside the
container, so `localhost:8000` *is* the API, the page's origin *is*
`http://localhost:5173`, and a CORS allowlist written for local development
matches because nothing is being faked. Nothing is asked of the application:
absolute URLs, hardcoded ports, websockets and a service on port 80 all behave
exactly as they would if the browser were running on the same machine as the
code — which, as far as the network is concerned, it now is.

Each connection is terminated by the same `docker exec socat` relay the port
tunnels use, so container-internal loopback is reachable without publishing
anything, and the IPv4-then-IPv6 fallback that Vite needs comes along with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import shlex
import socket
import struct

import asyncssh

from . import ssh
from .ssh import SSHError, SSHTarget

log = logging.getLogger(__name__)

SOCKS_VERSION = 5
NO_AUTHENTICATION = 0x00
NO_ACCEPTABLE_METHODS = 0xFF

CMD_CONNECT = 0x01

ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04

REPLY_SUCCEEDED = 0x00
REPLY_GENERAL_FAILURE = 0x01
REPLY_HOST_UNREACHABLE = 0x04
REPLY_COMMAND_NOT_SUPPORTED = 0x07
REPLY_ADDRESS_TYPE_NOT_SUPPORTED = 0x08

# Long enough for a websocket to idle between frames; short enough that a
# forgotten relay does not sit in the container for a day.
RELAY_TIMEOUT_SECONDS = 3600

# A handshake is four small reads. Anything slower is not a browser.
HANDSHAKE_TIMEOUT_SECONDS = 15.0


def relay_command(container: str, host: str, port: int) -> str:
    """Shell command that connects to `host:port` from inside the container.

    Names are resolved in there too, so a container that knows `db` by name
    behaves the same for the browser as it does for the code.

    Address family is the fiddly part, and getting it wrong looks like the app
    being down. socat resolves a name once and connects to the first address it
    gets; it does not try the other family. Node binds `localhost` to ::1 on a
    modern system, so Vite listens on IPv6 loopback only — and a request for
    `localhost` that resolves to 127.0.0.1 first simply fails. Hence trying both
    for a name.

    Loopback literals get the same treatment on purpose: a browser asking for
    `127.0.0.1:5173` means "the local machine", and refusing because the server
    happens to be on ::1 would be correct and useless. Any other address is
    taken at face value.
    """
    quoted = shlex.quote(host)
    family = _family(host)

    if family == "ipv6":
        attempts = [f"TCP6:[{quoted}]:{port}"]
    elif family == "ipv4":
        attempts = [f"TCP4:{quoted}:{port}"]
    elif family == "loopback":
        attempts = [f"TCP4:127.0.0.1:{port}", f"TCP6:[::1]:{port}"]
    else:  # a name; let the container resolve it, in either family
        attempts = [f"TCP4:{quoted}:{port}", f"TCP6:{quoted}:{port}"]

    parts = [
        f"socat -T {RELAY_TIMEOUT_SECONDS} STDIO {address} 2>/dev/null"
        for address in attempts[:-1]
    ]
    parts.append(f"exec socat -T {RELAY_TIMEOUT_SECONDS} STDIO {attempts[-1]}")
    inner = " || ".join(parts)
    return f"docker exec -i {shlex.quote(container)} sh -c " + shlex.quote(inner)


def _family(host: str) -> str:
    """'loopback', 'ipv4', 'ipv6' or 'name'."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "loopback" if host.lower() == "localhost" else "name"
    if address.is_loopback:
        return "loopback"
    return "ipv6" if address.version == 6 else "ipv4"


class ProjectProxy:
    """One SOCKS5 listener, bound to loopback, for one container.

    Deliberately unauthenticated and bound to 127.0.0.1: the same trust model
    as the port tunnels, which any local process can already reach. Auth would
    also be theatre here, since Chromium does not implement SOCKS5
    username/password.
    """

    def __init__(self, project_id: str, container: str, target: SSHTarget) -> None:
        self.project_id = project_id
        self.container = container
        self.target = target
        self.local_port = 0
        self._server: asyncio.AbstractServer | None = None
        self._connections = 0

    async def start(self, bind: str = "127.0.0.1") -> int:
        self._server = await asyncio.start_server(self._handle, bind, 0)
        self.local_port = self._server.sockets[0].getsockname()[1]
        log.info(
            "socks proxy for %s on %s:%d -> %s",
            self.project_id, bind, self.local_port, self.container,
        )
        return self.local_port

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    # --- the protocol -------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await asyncio.wait_for(
                self._negotiate(reader, writer), timeout=HANDSHAKE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            log.debug("socks: handshake timed out")
            _close(writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            _close(writer)
        except Exception as exc:  # noqa: BLE001 — one bad client is not fatal
            log.warning("socks: connection failed: %s", exc)
            _close(writer)

    async def _negotiate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        version, count = struct.unpack("!BB", await reader.readexactly(2))
        if version != SOCKS_VERSION:
            _close(writer)
            return
        methods = await reader.readexactly(count)
        if NO_AUTHENTICATION not in methods:
            writer.write(struct.pack("!BB", SOCKS_VERSION, NO_ACCEPTABLE_METHODS))
            await writer.drain()
            _close(writer)
            return
        writer.write(struct.pack("!BB", SOCKS_VERSION, NO_AUTHENTICATION))
        await writer.drain()

        version, command, _reserved, address_type = struct.unpack(
            "!BBBB", await reader.readexactly(4)
        )
        if version != SOCKS_VERSION:
            _close(writer)
            return
        if command != CMD_CONNECT:
            # BIND and UDP ASSOCIATE have no meaning here: there is nothing to
            # listen on behalf of, and the relay is a byte stream.
            await _reply(writer, REPLY_COMMAND_NOT_SUPPORTED)
            _close(writer)
            return

        host = await _read_address(reader, address_type)
        if host is None:
            await _reply(writer, REPLY_ADDRESS_TYPE_NOT_SUPPORTED)
            _close(writer)
            return
        (port,) = struct.unpack("!H", await reader.readexactly(2))

        await self._connect(reader, writer, host, port)

    async def _connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
    ) -> None:
        try:
            process = await ssh.pool.create_process(
                self.target,
                relay_command(self.container, host, port),
                encoding=None,
            )
        except (SSHError, asyncssh.Error) as exc:
            log.warning("socks: could not reach %s:%d: %s", host, port, exc)
            await _reply(writer, REPLY_HOST_UNREACHABLE)
            _close(writer)
            return

        await _reply(writer, REPLY_SUCCEEDED)
        self._connections += 1
        try:
            await _pump(reader, writer, process)
        finally:
            self._connections -= 1
            with contextlib.suppress(Exception):
                process.close()
            _close(writer)


async def _pump(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    process: asyncssh.SSHClientProcess,
) -> None:
    """Copy in both directions until both halves are done.

    Waiting for both rather than the first to finish is the difference between
    a working proxy and one that hangs up mid-response: a client that has sent
    its request stops reading from its own socket, so the direction carrying
    the request completes long before the reply has been delivered. Tearing the
    pair down at that point closes the connection with nothing sent, which the
    client reports as the server disconnecting.
    """

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

    await asyncio.gather(to_container(), to_client(), return_exceptions=True)


async def _read_address(reader: asyncio.StreamReader, address_type: int) -> str | None:
    if address_type == ATYP_IPV4:
        return socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
    if address_type == ATYP_IPV6:
        return socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    if address_type == ATYP_DOMAIN:
        (length,) = struct.unpack("!B", await reader.readexactly(1))
        raw = await reader.readexactly(length)
        # Names arrive as they will be resolved — already punycode if they were
        # ever unicode — and the container does the resolving, so this only has
        # to survive the trip. Decoding via the `idna` codec was wrong twice:
        # it re-interprets a name that needs no interpreting, and it rejects an
        # error handler, so one odd byte took the whole connection down with
        # "Malformed reply" on the client and no clue as to why.
        return raw.decode("utf-8", errors="replace")
    return None


async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
    """Minimal reply. The bound address is ignored by every client we care about."""
    writer.write(
        struct.pack("!BBBB", SOCKS_VERSION, code, 0x00, ATYP_IPV4)
        + socket.inet_aton("0.0.0.0")
        + struct.pack("!H", 0)
    )
    with contextlib.suppress(Exception):
        await writer.drain()


def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()


class ProxyRegistry:
    """One proxy per project, started on demand."""

    def __init__(self) -> None:
        self._proxies: dict[str, ProjectProxy] = {}
        self._lock = asyncio.Lock()

    async def ensure(
        self, *, project_id: str, container: str, target: SSHTarget, bind: str
    ) -> ProjectProxy:
        async with self._lock:
            existing = self._proxies.get(project_id)
            if existing is not None and existing._server is not None:
                # The container can be recreated under a project without the
                # proxy noticing, and relaying into a name that no longer
                # exists fails in a way that looks like the app is down.
                if existing.container == container:
                    return existing
                await existing.stop()

            proxy = ProjectProxy(project_id, container, target)
            await proxy.start(bind)
            self._proxies[project_id] = proxy
            return proxy

    def get(self, project_id: str) -> ProjectProxy | None:
        return self._proxies.get(project_id)

    async def close(self, project_id: str) -> None:
        proxy = self._proxies.pop(project_id, None)
        if proxy is not None:
            await proxy.stop()

    async def close_all(self) -> None:
        for project_id in list(self._proxies):
            await self.close(project_id)


registry = ProxyRegistry()
