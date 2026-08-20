"""Relayed interactive harness sign-in.

Claude Code's sign-in is an interactive PKCE flow: it prints an authorization
URL, waits for the user to approve in a browser, then reads a code back from
its own stdin. That is fine on a laptop and useless on a server you never shell
into.

This module drives that flow on the user's behalf. It runs the command on a PTY
inside a throwaway container, scrapes the URL out of the pane, hands it to the
UI, accepts the code the user pastes back, types it into the same PTY, and
harvests whatever credential the flow produced.

Three things are deliberate:

* **Every step is non-blocking.** `submit_code` types and returns; progress is
  made one poll at a time. An HTTP request that sat for ninety seconds waiting
  for an OAuth exchange looked exactly like a hang, because functionally it was
  one.
* **Harvesting accepts any of three outcomes.** Depending on mode and version
  the flow may write a credentials file, print a long-lived token to copy, or
  simply leave the harness authenticated. Insisting on one of them is how a
  working sign-in gets reported as a failure.
* **The pane is always exposed.** If automation cannot recognise the outcome,
  the user still sees the terminal and can tell us what it said, rather than
  staring at a spinner.
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

from . import docker_remote, imagebuild, ssh
from .harness import Harness, SessionSpace
from .ssh import SSHError

log = logging.getLogger(__name__)

# Long enough to find your phone and approve, and to outlive the exchange.
SESSION_TTL_SECONDS = 900
CONTAINER_PREFIX = "mp-login-"

# States: starting -> awaiting_code -> verifying -> complete | error
State = str

# Tokens the flow may print for the user to copy. Deliberately loose: the
# prefix has changed before, and a missed match only falls back to the other
# harvest strategies.
_TOKEN_PATTERNS = [
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bCLAUDE_CODE_OAUTH_TOKEN\s*=\s*['\"]?([A-Za-z0-9_\-\.]{20,})"),
]


@dataclass
class LoginSession:
    id: str
    org_id: str
    harness_kind: str
    server_id: str
    container: str
    state: State = "starting"
    url: str | None = None
    detail: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    verifying_since: float | None = None
    # Whichever the flow produced.
    oauth_blob: str | None = None
    oauth_token: str | None = None
    # Last pane contents, so the UI can show what the harness is doing.
    pane: str = ""
    cleaned_up: bool = False

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > SESSION_TTL_SECONDS

    @property
    def has_credential(self) -> bool:
        return bool(self.oauth_blob or self.oauth_token)


_sessions: dict[str, LoginSession] = {}


def get(session_id: str) -> LoginSession | None:
    session = _sessions.get(session_id)
    if session is None:
        return None
    if session.expired and session.state not in {"complete", "error"}:
        session.state = "error"
        session.detail = "Sign-in timed out. Start again."
    return session


def forget(session_id: str) -> None:
    _sessions.pop(session_id, None)


def _prune() -> None:
    for key in [
        k for k, s in _sessions.items() if s.expired or s.state == "complete"
    ]:
        if time.monotonic() - _sessions[key].created_at > SESSION_TTL_SECONDS:
            _sessions.pop(key, None)


def _scrape_token(pane: str) -> str | None:
    for pattern in _TOKEN_PATTERNS:
        match = pattern.search(pane)
        if match:
            return match.group(match.lastindex or 0).strip()
    return None


async def start(
    conn: asyncssh.SSHClientConnection,
    *,
    org_id: str,
    server_id: str,
    harness: Harness,
    image: str,
    base_image: str | None = None,
    setup_script: str | None = None,
) -> LoginSession:
    """Begin a sign-in. Returns immediately; poll the session for progress."""
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

    # Returned before any of the work happens. Preparing a sign-in means
    # building a container image on a server that may never have run one,
    # starting it, and waiting for the harness to print a URL — minutes on a
    # cold machine. Holding the request open for that is what made this button
    # report a network error while working perfectly well underneath.
    #
    # The client already polls this session, so there is nothing to wait for.
    task = asyncio.create_task(
        _prepare(
            conn,
            session,
            harness=harness,
            command=command,
            image=image,
            base_image=base_image,
            setup_script=setup_script,
        )
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return session


# Held so the loop does not collect work nothing is awaiting.
_tasks: set[asyncio.Task] = set()


async def _prepare(
    conn: asyncssh.SSHClientConnection,
    session: LoginSession,
    *,
    harness: Harness,
    command: list[str],
    image: str,
    base_image: str | None,
    setup_script: str | None,
) -> None:
    """Everything between asking to sign in and having a URL to open."""
    session_id = session.id
    container = session.container
    try:
        # Signing in must not depend on having created a project first — but it did,
        # because the image it runs in is only ever built by creating one. On a
        # freshly added server `docker run` failed with "Unable to find image
        # locally" and the button appeared to do nothing.
        #
        # Built with the same recipe and tag a project would use, so the work is
        # shared: whichever happens first pays for it and the other finds it there.
        if base_image:
            session.detail = "Preparing the container image. First time only."
            try:
                built = await imagebuild.ensure_image(
                    conn, tag=image, base_image=base_image, setup_script=setup_script
                )
                if built:
                    log.info("built %s for a sign-in on %s", image, session.server_id)
            except SSHError as exc:
                session.state = "error"
                session.detail = f"Could not prepare the container image: {exc}"[:300]
                return

        # Before adding one of our own, so finished attempts do not pile up on
        # the server. Best effort: a sign-in should not fail because tidying up
        # did.
        try:
            await sweep_stale(conn)
        except SSHError as exc:
            log.warning("could not sweep abandoned login containers: %s", exc)

        session.detail = "Starting a container to sign in from."
        run = await ssh.run(
            conn,
            " ".join(
                shlex.quote(a)
                for a in [
                    "docker", "run", "-d",
                    "--name", container,
                    "--label", "moonphase.login=1",
                    image, "sleep", str(SESSION_TTL_SECONDS + 120),
                ]
            ),
            timeout=300,
        )
        if not run.ok:
            session.state = "error"
            session.detail = (run.stderr or run.stdout).strip()[:300]
            return

        # Skip the first-run wizard, or it swallows the login prompt.
        # The relay runs in a throwaway container of its own, so the plain
        # single-user layout is the right one here.
        for path, contents in harness.seed_config_files(SessionSpace()).items():
            quoted = shlex.quote(path)
            await ssh.run(
                conn,
                f"docker exec -i -u dev {shlex.quote(container)} sh -c "
                + shlex.quote(f"mkdir -p $(dirname {quoted}) && cat > {quoted}"),
                timeout=60,
                stdin=contents,
            )

        # tmux gives the command a real PTY and lets us read the pane and type into
        # it from separate requests, which a single exec channel would not. The
        # trailing sleep keeps the pane (and anything it printed) alive after the
        # command exits, so a token on the final screen is still harvestable.
        inner = " ".join(shlex.quote(a) for a in command)
        start_cmd = [
            "tmux", "new-session", "-d", "-s", "login", "-x", "200", "-y", "50",
            f"{inner}; echo '[moonphase] login command exited'; sleep {SESSION_TTL_SECONDS}",
        ]
        started = await docker_remote.exec_capture(conn, container, start_cmd, timeout=60)
        if not started.ok:
            session.state = "error"
            session.detail = (started.stderr or started.stdout).strip()[:300]
            await cleanup(conn, session)
            return

        session.detail = "Waiting for the sign-in link."
        pattern = re.compile(harness.login_url_pattern() or r"https://\S+")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            session.pane = await capture(conn, container)
            # The pane hard-wraps long URLs, so rejoin before matching.
            match = pattern.search(session.pane.replace("\n", ""))
            if match:
                session.url = match.group(0).rstrip(")]},.")
                session.state = "awaiting_code"
                log.info("login %s: authorization url ready", session_id)
                return

        session.state = "error"
        session.detail = "The harness did not produce an authorization URL in time."
        await cleanup(conn, session)
        return
    except Exception as exc:  # noqa: BLE001 — the session is the only report
        log.warning("login %s failed while preparing: %s", session.id, exc)
        session.state = "error"
        session.detail = str(exc)[:300]


async def submit_code(
    conn: asyncssh.SSHClientConnection, session: LoginSession, code: str
) -> LoginSession:
    """Type the pasted code into the waiting prompt and return immediately.

    Returning without waiting is the point: the OAuth exchange takes as long as
    it takes, and the caller polls `advance` for the outcome.
    """
    if session.state not in {"awaiting_code", "verifying"}:
        raise SSHError(f"This sign-in is {session.state}, not waiting for a code.")

    # `-l` sends the string literally, so a code containing '#' or ';' is not
    # reinterpreted as a tmux key name.
    await docker_remote.exec_capture(
        conn, session.container,
        ["tmux", "send-keys", "-t", "login", "-l", code.strip()],
        timeout=30,
    )
    await asyncio.sleep(0.3)
    await docker_remote.exec_capture(
        conn, session.container, ["tmux", "send-keys", "-t", "login", "Enter"], timeout=30
    )

    session.state = "verifying"
    session.verifying_since = time.monotonic()
    session.detail = None
    return session


async def advance(
    conn: asyncssh.SSHClientConnection, session: LoginSession, harness: Harness
) -> LoginSession:
    """Perform one non-blocking check on a verifying session."""
    if session.state != "verifying":
        return session

    session.pane = await capture(conn, session.container)

    # 1. A credentials file is the strongest signal: the harness persisted it.
    for path in getattr(harness, "credential_paths", lambda: [])():
        result = await docker_remote.exec_capture(
            conn, session.container,
            ["sh", "-c", f"cat {shlex.quote(path)} 2>/dev/null"],
            timeout=30,
        )
        if result.ok and result.stdout.strip():
            session.oauth_blob = result.stdout
            break

    # 2. A printed token, which is what setup-token mode produces.
    if not session.oauth_blob:
        token = _scrape_token(session.pane)
        if token:
            session.oauth_token = token

    # 3. Failing both, ask the harness whether it considers itself signed in.
    #    Without a captured credential that is useless to us for other
    #    containers, but it tells the user the flow itself worked.
    authenticated = False
    if not session.has_credential:
        script = harness.auth_status_script()
        if script:
            status = await docker_remote.exec_capture(
                conn, session.container, ["sh", "-c", script], timeout=30
            )
            authenticated = '"loggedIn":true' in status.stdout.replace(" ", "")

    if session.has_credential:
        session.state = "complete"
        session.detail = None
        log.info(
            "login %s: captured %s",
            session.id,
            "credentials file" if session.oauth_blob else "token",
        )
        await cleanup(conn, session)
        return session

    lowered = session.pane.lower()
    if any(word in lowered for word in ("invalid code", "expired", "authentication failed")):
        session.state = "error"
        session.detail = "The harness rejected that code. Start again."
        await cleanup(conn, session)
        return session

    if authenticated:
        session.state = "error"
        session.detail = (
            "The harness signed in but did not expose a credential Moonphase can "
            "copy to other containers. Use an API key instead, and please report "
            "the terminal output below."
        )
        await cleanup(conn, session)
        return session

    if session.verifying_since and time.monotonic() - session.verifying_since > 180:
        session.state = "error"
        session.detail = (
            "Timed out waiting for the harness to finish. The terminal output "
            "below shows where it stopped."
        )
        await cleanup(conn, session)

    return session


async def capture(conn: asyncssh.SSHClientConnection, container: str) -> str:
    result = await docker_remote.exec_capture(
        conn, container, ["tmux", "capture-pane", "-p", "-t", "login", "-S", "-200"],
        timeout=30,
    )
    return result.stdout if result.ok else ""


async def cleanup(conn: asyncssh.SSHClientConnection, session: LoginSession) -> None:
    """Destroy the throwaway container. Best effort — never masks a result."""
    if session.cleaned_up:
        return
    session.cleaned_up = True
    try:
        await ssh.run(conn, f"docker rm -f {shlex.quote(session.container)}", timeout=60)
    except SSHError as exc:
        log.warning("could not remove login container %s: %s", session.container, exc)


async def sweep_stale(conn: asyncssh.SSHClientConnection) -> int:
    """Remove login containers that have finished and been left behind.

    A sign-in is tracked in memory, so restarting the API — an upgrade, say —
    forgets every attempt in flight while their containers stay on the server.
    Nothing ever removed them, so they accumulated one per abandoned attempt for
    as long as the machine stayed up.

    Only ones that have exited. A container that is still running is either a
    sign-in someone is part-way through or one whose own timeout has not
    expired yet — and it might not be ours at all: nothing stops two Moonphase
    instances from sharing a server, and this process's idea of which sessions
    are live says nothing about the other's. Every abandoned container exits on
    its own when its sleep runs out, so waiting for that costs a quarter of an
    hour and removes the chance of killing a sign-in someone is using.
    """
    result = await ssh.run(
        conn,
        "docker ps -a --filter label=moonphase.login=1 --filter status=exited "
        "--format '{{.Names}}'",
        timeout=30,
    )
    if not result.ok:
        return 0

    names = result.stdout.split()
    for name in names:
        await ssh.run(conn, f"docker rm -f {shlex.quote(name)}", timeout=60)
    if names:
        log.info("removed %d finished login container(s)", len(names))
    return len(names)
