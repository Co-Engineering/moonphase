"""Server onboarding.

Three trust modes, all converging on the same end state: Moonphase holds an
ed25519 key it generated itself, scoped to one server, revocable without
touching anything else.

  password_bootstrap  log in with a password once, install our key, verify it,
                      then destroy the password. Best UX, and the credential we
                      keep is one we made.
  managed_key         generate the pair, show the user the public half, wait for
                      them to install it. Nothing of theirs ever reaches us.
  provided_key        user pastes their own key. Instant, but the blast radius
                      is every machine that key opens, so it is stored
                      encrypted and flagged in the UI.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass

import asyncssh

from . import docker_remote, ssh
from .ssh import SSHError, SSHTarget

log = logging.getLogger(__name__)

AUTHORIZED_KEYS_MARKER = "moonphase"


@dataclass
class BootstrapResult:
    status: str  # 'online' | 'error' | 'awaiting_key_install'
    detail: str | None = None
    host_key_fingerprint: str | None = None
    docker_version: str | None = None
    docker_installed: bool = False
    docker_usable: bool = False
    # Set when we generated a key the caller must persist.
    generated_private_key: str | None = None
    generated_public_key: str | None = None
    # True once key-only login is confirmed, so the password may be destroyed.
    password_can_be_discarded: bool = False


def authorized_keys_line(public_key: str, server_name: str) -> str:
    """Tag our key so a user auditing authorized_keys can see where it came from."""
    safe_name = "".join(c for c in server_name if c.isalnum() or c in "-_.")[:40]
    return f"{public_key.strip()} {AUTHORIZED_KEYS_MARKER}:{safe_name or 'server'}"


async def install_public_key(
    conn: asyncssh.SSHClientConnection, public_key: str, server_name: str
) -> None:
    """Append our public key to authorized_keys, idempotently.

    The script (and the key inside it) is piped to `sh -s` over stdin, so the
    key never appears in the remote process list. `grep -qxF` keeps repeated
    bootstraps of the same server from stacking duplicate lines, and the chmods
    are the permissions sshd refuses to work without.
    """
    line = authorized_keys_line(public_key, server_name)
    script = f"""set -e
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
key={shlex.quote(line)}
if ! grep -qxF "$key" "$HOME/.ssh/authorized_keys"; then
  printf '%s\\n' "$key" >> "$HOME/.ssh/authorized_keys"
fi
"""
    result = await ssh.run(conn, "sh -s", timeout=30, stdin=script)
    result.check("Installing the Moonphase public key")


async def remove_public_key(
    conn: asyncssh.SSHClientConnection, public_key: str, server_name: str
) -> None:
    """Revoke our access. Called when a server is deleted."""
    line = authorized_keys_line(public_key, server_name)
    script = f"""set -e
f="$HOME/.ssh/authorized_keys"
[ -f "$f" ] || exit 0
key={shlex.quote(line)}
grep -vxF "$key" "$f" > "$f.moonphase-tmp" || true
mv "$f.moonphase-tmp" "$f"
chmod 600 "$f"
"""
    await ssh.run(conn, "sh -s", timeout=30, stdin=script)


async def verify_key_login(target: SSHTarget) -> tuple[bool, str | None, str | None]:
    """Prove key-only login works before we throw the password away.

    Returns (ok, host_key_fingerprint, error). Deliberately opens a brand new
    connection with `password=None` — reusing the pooled one would prove
    nothing, since it authenticated with the password we are about to delete.
    """
    probe = SSHTarget(
        server_id=target.server_id,
        host=target.host,
        port=target.port,
        username=target.username,
        private_key=target.private_key,
        passphrase=target.passphrase,
        password=None,
        known_host_key_fp=target.known_host_key_fp,
    )
    try:
        conn, fingerprint = await ssh.connect(probe)
    except SSHError as exc:
        return False, None, str(exc)
    try:
        whoami = await ssh.run(conn, "whoami", timeout=15)
        if not whoami.ok:
            return False, fingerprint, "Key login succeeded but the shell did not respond."
        return True, fingerprint, None
    finally:
        conn.close()


async def ensure_docker(
    conn: asyncssh.SSHClientConnection,
    ssh_user: str,
    *,
    auto_install: bool,
) -> docker_remote.DockerInfo:
    """Probe Docker, optionally installing it."""
    info = await docker_remote.probe(conn)
    if info.installed and info.usable_by_user:
        return info
    if not info.installed and auto_install:
        return await docker_remote.install(conn, ssh_user)
    return info


async def bootstrap(
    *,
    server_id: str,
    server_name: str,
    host: str,
    port: int,
    ssh_user: str,
    auth_mode: str,
    password: str | None = None,
    provided_private_key: str | None = None,
    provided_passphrase: str | None = None,
    existing_private_key: str | None = None,
    existing_public_key: str | None = None,
    known_host_key_fp: str | None = None,
    auto_install_docker: bool = True,
) -> BootstrapResult:
    """Take a server from "user filled in a form" to "ready to run projects".

    Pure with respect to the database: it returns what it learned and what it
    generated, and the caller persists. That keeps the transaction boundary in
    the route handler and makes this testable against a throwaway VM.
    """
    generated_private: str | None = None
    generated_public: str | None = None

    # --- work out what credential opens the door right now -----------------
    if auth_mode == "provided_key":
        if not provided_private_key:
            raise SSHError("A private key is required for this auth mode.")
        connect_key: str | None = provided_private_key
        connect_passphrase = provided_passphrase
        connect_password = None

    elif auth_mode == "managed_key":
        # Either we already generated a pair (retry after the user installed
        # it), or this is the first call and we mint one now.
        if existing_private_key:
            connect_key = existing_private_key
            generated_public = existing_public_key
        else:
            generated_private, generated_public = ssh.generate_keypair(
                f"moonphase@{server_name}"
            )
            connect_key = generated_private
        connect_passphrase = None
        connect_password = None

    elif auth_mode == "password_bootstrap":
        if not password:
            raise SSHError("A password is required to bootstrap this server.")
        connect_key = None
        connect_passphrase = None
        connect_password = password

    else:
        raise SSHError(f"Unknown SSH auth mode {auth_mode!r}.")

    target = SSHTarget(
        server_id=server_id,
        host=host,
        port=port,
        username=ssh_user,
        private_key=connect_key,
        passphrase=connect_passphrase,
        password=connect_password,
        known_host_key_fp=known_host_key_fp,
    )

    # --- first contact ------------------------------------------------------
    try:
        conn, fingerprint = await ssh.connect(target)
    except SSHError as exc:
        if auth_mode == "managed_key":
            # Expected on the first pass: the user has not installed the key
            # yet. Hand back the public half so the UI can display it.
            return BootstrapResult(
                status="awaiting_key_install",
                detail=(
                    "Install the public key below on the server, then press "
                    "Retry. Moonphase could not log in yet."
                ),
                host_key_fingerprint=None,
                docker_version=None,
                generated_private_key=generated_private,
                generated_public_key=generated_public,
            )
        return BootstrapResult(
            status="error",
            detail=str(exc),
            generated_private_key=generated_private,
            generated_public_key=generated_public,
        )

    try:
        # --- password bootstrap: install our own key and prove it works -----
        if auth_mode == "password_bootstrap":
            generated_private, generated_public = ssh.generate_keypair(
                f"moonphase@{server_name}"
            )
            await install_public_key(conn, generated_public, server_name)

            ok, key_fp, error = await verify_key_login(
                SSHTarget(
                    server_id=server_id,
                    host=host,
                    port=port,
                    username=ssh_user,
                    private_key=generated_private,
                    known_host_key_fp=fingerprint,
                )
            )
            if not ok:
                return BootstrapResult(
                    status="error",
                    detail=(
                        "Installed the Moonphase key but could not log in with it: "
                        f"{error}. The password was not stored."
                    ),
                    host_key_fingerprint=fingerprint,
                    generated_private_key=None,
                    generated_public_key=None,
                )
            fingerprint = key_fp or fingerprint

        # --- docker ---------------------------------------------------------
        docker_info = await ensure_docker(conn, ssh_user, auto_install=auto_install_docker)

        if docker_info.installed and not docker_info.usable_by_user:
            # Group membership from a fresh install only applies to new
            # sessions, so drop the pooled connection and probe once more.
            await ssh.pool.drop(server_id)
            retry_target = SSHTarget(
                server_id=server_id,
                host=host,
                port=port,
                username=ssh_user,
                private_key=generated_private or connect_key,
                passphrase=connect_passphrase,
                password=None if generated_private else connect_password,
                known_host_key_fp=fingerprint,
            )
            try:
                retry_conn, _ = await ssh.connect(retry_target)
                try:
                    docker_info = await docker_remote.probe(retry_conn)
                finally:
                    retry_conn.close()
            except SSHError:
                pass

    finally:
        conn.close()

    if not docker_info.installed:
        return BootstrapResult(
            status="error",
            detail=docker_info.detail or "Docker is not installed on this server.",
            host_key_fingerprint=fingerprint,
            docker_installed=False,
            docker_usable=False,
            generated_private_key=generated_private,
            generated_public_key=generated_public,
            password_can_be_discarded=auth_mode == "password_bootstrap",
        )

    if not docker_info.usable_by_user:
        return BootstrapResult(
            status="error",
            detail=docker_info.detail or "Docker is installed but not usable by this user.",
            host_key_fingerprint=fingerprint,
            docker_version=docker_info.server_version,
            docker_installed=True,
            docker_usable=False,
            generated_private_key=generated_private,
            generated_public_key=generated_public,
            password_can_be_discarded=auth_mode == "password_bootstrap",
        )

    return BootstrapResult(
        status="online",
        detail=None,
        host_key_fingerprint=fingerprint,
        docker_version=docker_info.server_version,
        docker_installed=True,
        docker_usable=True,
        generated_private_key=generated_private,
        generated_public_key=generated_public,
        password_can_be_discarded=auth_mode == "password_bootstrap",
    )
