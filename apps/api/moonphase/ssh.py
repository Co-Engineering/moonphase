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


class SSHPool:
    """Per-server connection cache with single-flight connect."""

    def __init__(self) -> None:
        self._conns: dict[str, asyncssh.SSHClientConnection] = {}
        self._fingerprints: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, server_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(server_id, asyncio.Lock())

    async def get(self, target: SSHTarget) -> asyncssh.SSHClientConnection:
        existing = self._conns.get(target.server_id)
        if existing is not None and not existing.is_closed():
            return existing

        lock = await self._lock_for(target.server_id)
        async with lock:
            # Re-check: another waiter may have connected while we queued.
            existing = self._conns.get(target.server_id)
            if existing is not None and not existing.is_closed():
                return existing
            conn, fp = await connect(target)
            self._conns[target.server_id] = conn
            self._fingerprints[target.server_id] = fp
            return conn

    def observed_fingerprint(self, server_id: str) -> str | None:
        return self._fingerprints.get(server_id)

    async def drop(self, server_id: str) -> None:
        conn = self._conns.pop(server_id, None)
        self._fingerprints.pop(server_id, None)
        if conn is not None and not conn.is_closed():
            conn.close()
            # A half-dead socket must not turn shutdown into an error.
            with suppress(Exception):
                await conn.wait_closed()

    async def close_all(self) -> None:
        for server_id in list(self._conns):
            await self.drop(server_id)


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
    """Run a command and collect its output. Never raises on non-zero exit."""
    try:
        result = await asyncio.wait_for(
            conn.run(command, check=False, input=stdin), timeout=timeout
        )
    except TimeoutError as exc:
        raise SSHError(f"Command timed out after {timeout}s: {command[:120]}") from exc
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
