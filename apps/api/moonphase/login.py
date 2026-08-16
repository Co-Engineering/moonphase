"""Relayed interactive harness sign-in.

Claude Code's `setup-token` is an interactive PKCE flow: it prints an
authorization URL, waits for the user to approve in a browser, then reads a
code back from its own stdin. That is fine on a laptop and useless on a server
you never shell into.

This module drives that flow on the user's behalf. It runs the command on a PTY
inside a throwaway container, scrapes the URL out of the pane, hands it to the
UI, accepts the code the user pastes back, types it into the same PTY, and
harvests whatever credential files the flow produced. The result is stored
once, at organization level, and applied to every project container.

Nothing here parses the token itself. Capturing the harness's own credential
files verbatim means the flow keeps working when the token format changes.

Sessions live in memory: they last a couple of minutes, and a restart mid-login
is recoverable by starting again. Persisting a half-finished OAuth handshake
would be more state than it is worth.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shlex
import time
from dataclasses import dataclass, field

import asyncssh

from . import docker_remote, ssh
from .harness import Harness
from .ssh import SSHError

log = logging.getLogger(__name__)

# Long enough to find your phone and approve; short enough that an abandoned
# container does not linger.
SESSION_TTL_SECONDS = 600
CONTAINER_PREFIX = "mp-login-"


@dataclass
class LoginSession:
    id: str
    org_id: str
    harness_kind: str
    server_id: str
    container: str
    state: str = "starting"  # starting | awaiting_code | verifying | complete | error
    url: str | None = None
    detail: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    # Captured on success: {path: contents} of the harness's credential files.
    credential_files: dict[str, str] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > SESSION_TTL_SECONDS


_sessions: dict[str, LoginSession] = {}


def get(session_id: str) -> LoginSession | None:
    session = _sessions.get(session_id)
    if session is not None and session.expired and session.state != "complete":
        session.state = "error"
        session.detail = "Sign-in timed out. Start again."
    return session


def _prune() -> None:
    for key in [k for k, s in _sessions.items() if s.expired and s.state == "complete"]:
        _sessions.pop(key, None)


async def start(
    conn: asyncssh.SSHClientConnection,
    *,
    org_id: str,
    server_id: str,
    harness: Harness,
    image: str,
) -> LoginSession:
    """Begin a sign-in and return once the authorization URL is known."""
    _prune()

    command = harness.login_command()
    if command is None:
        raise SSHError(f"{harness.display_name} does not support interactive sign-in.")

    session_id = secrets.token_urlsafe(16)
    container = f"{CONTAINER_PREFIX}{session_id[:12].lower()}"
    session = LoginSession(
        id=session_id,
        org_id=org_id,
        harness_kind=str(harness.kind),
        server_id=server_id,
        container=container,
    )
    _sessions[session_id] = session

    # A throwaway container, not a project one: signing in must not depend on
    # having created a project first, and must not disturb a running session.
    run = await ssh.run(
        conn,
        " ".join(
            shlex.quote(a)
            for a in [
                "docker", "run", "-d",
                "--name", container,
                "--label", "moonphase.login=1",
                image, "sleep", str(SESSION_TTL_SECONDS + 60),
            ]
        ),
        timeout=300,
    )
    if not run.ok:
        session.state = "error"
        session.detail = (run.stderr or run.stdout).strip()[:300]
        return session

    # Skip the first-run wizard, or it swallows the login prompt.
    for path, contents in harness.seed_config_files().items():
        quoted = shlex.quote(path)
        await ssh.run(
            conn,
            f"docker exec -i -u dev {shlex.quote(container)} sh -c "
            + shlex.quote(f"mkdir -p $(dirname {quoted}) && cat > {quoted}"),
            timeout=60,
            stdin=contents,
        )

    # tmux gives the command a real PTY and lets us read the pane and type into
    # it from separate requests, which a single exec channel would not.
    start_cmd = [
        "tmux", "new-session", "-d", "-s", "login", "-x", "200", "-y", "50",
        " ".join(shlex.quote(a) for a in command) + "; sleep 60",
    ]
    started = await docker_remote.exec_capture(conn, container, start_cmd, timeout=60)
    if not started.ok:
        session.state = "error"
        session.detail = (started.stderr or started.stdout).strip()[:300]
        await cleanup(conn, session)
        return session

    pattern = re.compile(harness.login_url_pattern() or r"https://\S+")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        pane = await _capture(conn, container)
        # The pane hard-wraps long URLs, so rejoin before matching.
        unwrapped = pane.replace("\n", "")
        match = pattern.search(unwrapped)
        if match:
            session.url = match.group(0).rstrip(")]},.")
            session.state = "awaiting_code"
            log.info("login %s: authorization url ready", session_id)
            return session

    session.state = "error"
    session.detail = "The harness did not produce an authorization URL in time."
    await cleanup(conn, session)
    return session


async def submit_code(
    conn: asyncssh.SSHClientConnection,
    session: LoginSession,
    harness: Harness,
    code: str,
) -> LoginSession:
    """Type the pasted code into the waiting prompt and harvest credentials."""
    if session.state != "awaiting_code":
        raise SSHError(f"This sign-in is {session.state}, not waiting for a code.")

    session.state = "verifying"

    # `-l` sends the string literally, so a code containing '#' or ';' is not
    # reinterpreted as a tmux key name.
    await docker_remote.exec_capture(
        conn, session.container,
        ["tmux", "send-keys", "-t", "login", "-l", code.strip()],
        timeout=30,
    )
    await docker_remote.exec_capture(
        conn, session.container, ["tmux", "send-keys", "-t", "login", "Enter"], timeout=30
    )

    paths = getattr(harness, "credential_paths", lambda: [])()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        await asyncio.sleep(2)

        captured: dict[str, str] = {}
        for path in paths:
            result = await docker_remote.exec_capture(
                conn, session.container,
                ["sh", "-c", f"cat {shlex.quote(path)} 2>/dev/null"],
                timeout=30,
            )
            if result.ok and result.stdout.strip():
                captured[path] = result.stdout

        if captured:
            session.credential_files = captured
            session.state = "complete"
            session.detail = None
            log.info("login %s: credentials captured", session.id)
            await cleanup(conn, session)
            return session

        pane = await _capture(conn, session.container)
        lowered = pane.lower()
        if "invalid" in lowered or "expired" in lowered or "failed" in lowered:
            session.state = "error"
            session.detail = "The harness rejected that code. Start again."
            await cleanup(conn, session)
            return session

    session.state = "error"
    session.detail = "Timed out waiting for the harness to store a credential."
    await cleanup(conn, session)
    return session


async def _capture(conn: asyncssh.SSHClientConnection, container: str) -> str:
    result = await docker_remote.exec_capture(
        conn, container, ["tmux", "capture-pane", "-p", "-t", "login", "-S", "-200"],
        timeout=30,
    )
    return result.stdout if result.ok else ""


async def pane(conn: asyncssh.SSHClientConnection, session: LoginSession) -> str:
    """The raw pane, so the UI can show progress or an unexpected prompt."""
    return await _capture(conn, session.container)


async def cleanup(
    conn: asyncssh.SSHClientConnection, session: LoginSession
) -> None:
    """Destroy the throwaway container. Best effort — never masks a result."""
    try:
        await ssh.run(
            conn, f"docker rm -f {shlex.quote(session.container)}", timeout=60
        )
    except SSHError as exc:
        log.warning("could not remove login container %s: %s", session.container, exc)


async def sweep_stale(conn: asyncssh.SSHClientConnection) -> int:
    """Remove login containers left behind by a crashed backend."""
    result = await ssh.run(
        conn, "docker ps -aq --filter label=moonphase.login=1", timeout=30
    )
    if not result.ok:
        return 0
    ids = result.stdout.split()
    for container_id in ids:
        await ssh.run(conn, f"docker rm -f {shlex.quote(container_id)}", timeout=60)
    return len(ids)
