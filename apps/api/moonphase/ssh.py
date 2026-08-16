"""SSH connectivity to managed servers.

One pooled `asyncssh` connection per server, shared by every request that needs
that machine. Connections are reference-free and closed lazily; asyncssh
multiplexes channels over a single TCP connection, so running twenty `docker`
commands and three attached terminals against one server costs one handshake.

Host keys are pinned on first successful connect. A mismatch afterwards raises
rather than reconnecting, because the alternative is silently handing an SSH
private key to whoever now answers on that address.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import asyncssh

from .config import get_settings

log = logging.getLogger(__name__)


class SSHError(RuntimeError):
    """Any failure reaching or authenticating to a managed server."""


class HostKeyMismatch(SSHError):
    """The server presented a different host key than the one we pinned."""


@dataclass
class SSHTarget:
    """Everything needed to open one connection, with secrets already decrypted."""

    server_id: str
    host: str
    port: int
    username: str
    private_key: str | None = None
    passphrase: str | None = None
    password: str | None = None
    known_host_key_fp: str | None = None


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    def check(self, what: str) -> CommandResult:
        if not self.ok:
            detail = (self.stderr or self.stdout).strip()
            raise SSHError(f"{what} failed (exit {self.exit_status}): {detail[:600]}")
        return self


def fingerprint(key: asyncssh.SSHKey) -> str:
    """OpenSSH-style SHA256 fingerprint, matching `ssh-keygen -lf`."""
    digest = hashlib.sha256(key.public_data).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class _PinnedHostKeyPolicy(asyncssh.SSHClient):
    """Captures the presented host key and enforces a pin when we have one."""

    def __init__(self, expected_fp: str | None) -> None:
        self.expected_fp = expected_fp
        self.observed_fp: str | None = None
        self.mismatch: str | None = None

    def validate_host_public_key(self, host: str, addr: str, port: int, key: Any) -> bool:
        del host, addr, port
        self.observed_fp = fingerprint(key)
        if self.expected_fp is None:
            return True
        if self.observed_fp != self.expected_fp:
            self.mismatch = (
                f"expected {self.expected_fp}, server presented {self.observed_fp}"
            )
            return False
        return True


# sshd allows ten concurrent channels per connection by default
# (`MaxSessions 10`), and asyncssh multiplexes everything over one TCP
# connection. Everything Moonphase does against a server therefore competes for
# those ten: an attached terminal, a feed following a transcript, the activity
# monitor, port detection — and one channel for every TCP connection carried by
# a preview tunnel, so a single page load can take six on its own.
#
# Past ten, `create_process` fails with ChannelOpenError("open failed") and the
# terminal simply stops working. Rather than reconfigure sshd on someone's
# machine, spread the load over several connections: the limit is per
# connection, so a handful of them is several times the headroom, and the cost
# is one extra handshake each.
CONNECTIONS_PER_SERVER = 4

# Under sustained load — several projects on one machine, each with a terminal,
# a feed and a preview — even four can fill. Rather than fail, open more, up to
# a ceiling that exists only so a bug cannot open connections forever.
MAX_CONNECTIONS_PER_SERVER = 12


class SSHPool:
    """Per-server connection cache with single-flight connect.

    Several connections per server, handed out round-robin. See
    CONNECTIONS_PER_SERVER for why more than one.
    """

    def __init__(self) -> None:
        self._conns: dict[str, list[asyncssh.SSHClientConnection]] = {}
        self._fingerprints: dict[str, str] = {}
        self._next: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        # Which server each pooled connection belongs to. `run()` is handed a
        # connection rather than a target, so without this it cannot ask for a
        # different one when the one it has is full.
        self._owner: dict[int, SSHTarget] = {}

    async def _lock_for(self, server_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(server_id, asyncio.Lock())

    def _live(self, server_id: str) -> list[asyncssh.SSHClientConnection]:
        conns = [c for c in self._conns.get(server_id, []) if not c.is_closed()]
        self._conns[server_id] = conns
        return conns

    async def get(self, target: SSHTarget) -> asyncssh.SSHClientConnection:
        """A connection to this server, round-robin over the pooled set."""
        conns = self._live(target.server_id)
        if len(conns) >= CONNECTIONS_PER_SERVER:
            return self._rotate(target.server_id, conns)

        lock = await self._lock_for(target.server_id)
        async with lock:
            # Re-check: another waiter may have connected while we queued.
            conns = self._live(target.server_id)
            if len(conns) >= CONNECTIONS_PER_SERVER:
                return self._rotate(target.server_id, conns)
            conn, fp = await connect(target)
            conns.append(conn)
            self._conns[target.server_id] = conns
            self._fingerprints[target.server_id] = fp
            self._owner[id(conn)] = target
            return conn

    def _rotate(
        self, server_id: str, conns: list[asyncssh.SSHClientConnection]
    ) -> asyncssh.SSHClientConnection:
        index = self._next.get(server_id, 0) % len(conns)
        self._next[server_id] = index + 1
        return conns[index]

    async def create_process(
        self, target: SSHTarget, command: str, **kwargs: Any
    ) -> asyncssh.SSHClientProcess:
        """Open a long-lived channel, trying every connection before giving up.

        Round-robin spreads channels, but it does not know how many each
        connection is already carrying — a terminal and a feed can happen to
        land on the same one. So a refusal is not fatal here: it means that
        connection is full, and another almost certainly is not.
        """
        last: Exception | None = None
        for _ in range(CONNECTIONS_PER_SERVER):
            conn = await self.get(target)
            try:
                return await conn.create_process(command, **kwargs)
            except asyncssh.ChannelOpenError as exc:
                last = exc
                log.debug("channel refused on a connection to %s: %s", target.host, exc)

        # Every pooled connection is full. Grow rather than refuse: the caller
        # is a terminal or a preview, and "your session stopped working because
        # something else was busy" is not an answer anyone can act on.
        while len(self._live(target.server_id)) < MAX_CONNECTIONS_PER_SERVER:
            conn, fp = await connect(target)
            self._conns[target.server_id].append(conn)
            self._fingerprints[target.server_id] = fp
            self._owner[id(conn)] = target
            log.info(
                "opened an extra connection to %s (%d in the pool)",
                target.host,
                len(self._conns[target.server_id]),
            )
            try:
                return await conn.create_process(command, **kwargs)
            except asyncssh.ChannelOpenError as exc:
                last = exc

        raise SSHError(
            f"Could not open a channel to {target.host}: "
            f"{MAX_CONNECTIONS_PER_SERVER} connections are all at their limit "
            f"({last}). Close some terminals or previews, or raise MaxSessions "
            "in the server's sshd config."
        )

    async def another(
        self, conn: asyncssh.SSHClientConnection
    ) -> asyncssh.SSHClientConnection | None:
        """A different connection to the same server, opening one if needed.

        For callers that hold a connection rather than a target and have just
        been refused a channel on it.
        """
        target = self._owner.get(id(conn))
        if target is None:
            return None
        conns = self._live(target.server_id)
        if len(conns) < MAX_CONNECTIONS_PER_SERVER:
            fresh, fp = await connect(target)
            conns.append(fresh)
            self._conns[target.server_id] = conns
            self._fingerprints[target.server_id] = fp
            self._owner[id(fresh)] = target
            return fresh
        others = [c for c in conns if c is not conn]
        return self._rotate(target.server_id, others) if others else None

    def observed_fingerprint(self, server_id: str) -> str | None:
        return self._fingerprints.get(server_id)

    async def drop(self, server_id: str) -> None:
        conns = self._conns.pop(server_id, [])
        self._fingerprints.pop(server_id, None)
        self._next.pop(server_id, None)
        for conn in conns:
            self._owner.pop(id(conn), None)
            if not conn.is_closed():
                conn.close()
                # A half-dead socket must not turn shutdown into an error.
                with suppress(Exception):
                    await conn.wait_closed()

    async def close_all(self) -> None:
        """Close every pooled connection, and let none of them stop the rest.

        This runs at shutdown and in test teardown, where a connection can
        already be unusable — its event loop gone, its socket dead. Raising
        here would abandon the connections after it in the list, which is the
        opposite of what closing everything is for.
        """
        for server_id in list(self._conns):
            try:
                await self.drop(server_id)
            except Exception as exc:  # noqa: BLE001
                log.debug("could not close pooled connection %s: %s", server_id, exc)
                self._conns.pop(server_id, None)


async def connect(target: SSHTarget) -> tuple[asyncssh.SSHClientConnection, str]:
    """Open a single connection and return it alongside the host key fingerprint."""
    settings = get_settings()

    client_keys: list[asyncssh.SSHKey] = []
    if target.private_key:
        try:
            client_keys = [
                asyncssh.import_private_key(target.private_key, passphrase=target.passphrase)
            ]
        except asyncssh.KeyImportError as exc:
            raise SSHError(f"Could not read the private key: {exc}") from exc
        except asyncssh.KeyEncryptionError as exc:
            raise SSHError("Private key passphrase is wrong or missing.") from exc

    if not client_keys and not target.password:
        raise SSHError("No SSH credential available for this server.")

    policy = _PinnedHostKeyPolicy(target.known_host_key_fp)

    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                host=target.host,
                port=target.port,
                username=target.username,
                client_keys=client_keys or None,
                password=target.password,
                client_factory=lambda: policy,
                # An empty trust tuple, NOT None. `known_hosts=None` disables
                # host key checking outright and never calls the policy above,
                # which would silently make our pinning inert. An empty
                # (known_hosts, ca_keys, revoked_keys) leaves the trust set
                # present but empty, so every key falls through to
                # validate_host_public_key and we decide.
                known_hosts=([], [], []),
                keepalive_interval=settings.moonphase_ssh_keepalive_interval,
                keepalive_count_max=3,
            ),
            timeout=settings.moonphase_ssh_connect_timeout,
        )
    except TimeoutError as exc:
        raise SSHError(
            f"Timed out connecting to {target.host}:{target.port} after "
            f"{settings.moonphase_ssh_connect_timeout}s."
        ) from exc
    except asyncssh.PermissionDenied as exc:
        raise SSHError(
            f"Authentication failed for {target.username}@{target.host}."
        ) from exc
    except asyncssh.HostKeyNotVerifiable as exc:
        if policy.mismatch:
            raise HostKeyMismatch(
                f"Host key for {target.host} changed: {policy.mismatch}. "
                "Moonphase refused to connect. If this was intentional, remove the "
                "pinned fingerprint on the server before retrying."
            ) from exc
        raise SSHError(f"Host key for {target.host} could not be verified.") from exc
    except (OSError, asyncssh.Error) as exc:
        raise SSHError(f"Could not connect to {target.host}:{target.port}: {exc}") from exc

    observed = policy.observed_fp or ""
    return conn, observed


async def run(
    conn: asyncssh.SSHClientConnection,
    command: str,
    *,
    timeout: float = 60.0,
    stdin: str | None = None,
) -> CommandResult:
    """Run a command and collect its output. Never raises on non-zero exit.

    A refused channel gets one retry. Short commands are the ones that collide
    with a burst — a page load through a preview tunnel, or several sessions
    being probed at once — and by the time a retry lands the burst has usually
    passed. `create_process` on the pool handles the same problem for
    long-lived channels by moving to a different connection; this cannot, since
    it is handed a connection rather than a target.
    """
    for attempt in range(3):
        try:
            result = await asyncio.wait_for(
                conn.run(command, check=False, input=stdin), timeout=timeout
            )
            break
        except TimeoutError as exc:
            raise SSHError(
                f"Command timed out after {timeout}s: {command[:120]}"
            ) from exc
        except asyncssh.ChannelOpenError as exc:
            # This connection is carrying its ten channels. Ask the pool for
            # another one before giving up — a refusal here surfaces as a
            # broken terminal or an empty port list, with nothing to suggest
            # that something unrelated was merely busy.
            other = await pool.another(conn) if attempt < 2 else None
            if other is None:
                raise SSHError(
                    "The server is at its limit of concurrent SSH channels "
                    f"({exc}). Close a terminal or a preview and try again."
                ) from exc
            conn = other
        except asyncssh.Error as exc:
            raise SSHError(f"Command failed to execute: {exc}") from exc

    return CommandResult(
        exit_status=result.exit_status if result.exit_status is not None else -1,
        stdout=_as_text(result.stdout),
        stderr=_as_text(result.stderr),
    )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def generate_keypair(comment: str) -> tuple[str, str]:
    """Generate an ed25519 keypair for Moonphase to authenticate with.

    Returns (private_key_openssh, public_key_openssh). The private half is
    encrypted before it touches the database; the public half is stored in
    plain text so the UI can show it for manual installation.
    """
    key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
    private = key.export_private_key("openssh").decode()
    public = key.export_public_key("openssh").decode().strip()
    return private, public


pool = SSHPool()
