"""Relayed MCP server OAuth, inside an already-running project container.

The same problem `login.py` solves for the harness's own account, one level
down. `claude mcp login <name> --no-browser` is the identical shape of
interactive PKCE flow — print an authorization URL, wait for the browser step,
read a code back — except the redirect target it registers with the OAuth
provider is always `http://localhost:PORT/callback`. That is not configurable
to anything else, and no tunnel can make it mean something different: the
provider genuinely redirects the browser there, and "localhost" on the
person's own laptop is not this container. What still works is that Claude
Code also accepts the resulting redirect URL typed back at an interactive
prompt — verified empirically, not assumed — so completing the flow never
actually requires that listener to be reachable, only that the code and state
in the URL the browser lands on get relayed back in.

Unlike account sign-in this runs inside the *project's own* container rather
than a throwaway one: the server it is authenticating already has to be
configured in that session's own `~/.claude.json` for `claude mcp login` to
find it at all. It still gets a tmux session of its own, though, so it never
collides with whatever the real session is doing in its pane.

What comes out the other end is captured from `~/.claude/.credentials.json`'s
`mcpOAuth` key — see `moonphase.harness.claude_code.merge_into_credentials_file`
for how that gets replayed into every session afterward — and handed back to
the caller to persist; this module only drives the terminal, it does not know
about the database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shlex
import time
from dataclasses import dataclass, field

import asyncssh

from . import docker_remote
from .harness import SessionSpace
from .ssh import SSHError

log = logging.getLogger(__name__)

# Long enough to find your phone and approve, short enough that an abandoned
# attempt's tmux session does not linger in the container for a day.
SESSION_TTL_SECONDS = 600
TMUX_PREFIX = "moonphase-mcp-login-"

# starting -> awaiting_paste -> verifying -> complete | error
State = str

# Deliberately general rather than provider-specific: an MCP server's OAuth
# authorization URL can be on any domain, unlike the account flow's, which is
# always claude.com.
LOGIN_URL_PATTERN = re.compile(r"https?://\S+")


@dataclass
class McpLoginSession:
    id: str
    org_id: str
    project_id: str
    session_name: str
    server_name: str
    home: str
    container: str
    tmux_session: str
    user_id: str | None = None
    state: State = "starting"
    url: str | None = None
    detail: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    verifying_since: float | None = None
    # The captured "<server-name>|<hash>": {...} pair, once complete.
    credential_entry: str | None = None
    pane: str = ""
    cleaned_up: bool = False
    # This server's entry, if any, read before this attempt's relay ever
    # started. _harvest_credential diffs against this so an entry already
    # sitting in the file — from a previous, unrelated connection — can never
    # be mistaken for proof that *this* attempt succeeded.
    existing_credential: str | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > SESSION_TTL_SECONDS


_sessions: dict[str, McpLoginSession] = {}
_tasks: set[asyncio.Task] = set()


def get(session_id: str) -> McpLoginSession | None:
    session = _sessions.get(session_id)
    if session is None:
        return None
    if session.expired and session.state not in {"complete", "error"}:
        session.state = "error"
        session.detail = "Timed out. Start again."
    return session


def forget(session_id: str) -> None:
    _sessions.pop(session_id, None)


def _prune() -> None:
    for key in [k for k, s in _sessions.items() if s.expired]:
        _sessions.pop(key, None)


async def start(
    conn: asyncssh.SSHClientConnection,
    *,
    org_id: str,
    project_id: str,
    session_name: str,
    server_name: str,
    space: SessionSpace,
    container: str,
    user_id: str | None = None,
) -> McpLoginSession:
    """Begin an OAuth relay for one MCP server. Returns immediately."""
    _prune()

    session_id = secrets.token_urlsafe(16)
    session = McpLoginSession(
        id=session_id,
        org_id=org_id,
        project_id=project_id,
        session_name=session_name,
        server_name=server_name,
        home=space.home,
        container=container,
        tmux_session=f"{TMUX_PREFIX}{session_id[:12].lower()}",
        user_id=user_id,
    )
    _sessions[session_id] = session

    # Returned before any of it runs, the same reason login.py's does: the
    # exchange takes as long as it takes, and holding an HTTP request open for
    # that reads as a hung button rather than one working underneath.
    task = asyncio.create_task(_prepare(conn, session))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return session


async def _prepare(conn: asyncssh.SSHClientConnection, session: McpLoginSession) -> None:
    try:
        session.existing_credential = await _read_oauth_entry(
            conn, session.container, session.home, session.server_name
        )
        await _supersede(conn, session)

        command = ["claude", "mcp", "login", session.server_name, "--no-browser"]
        inner = " ".join(shlex.quote(a) for a in command)
        start_cmd = [
            "tmux", "new-session", "-d", "-s", session.tmux_session,
            "-x", "200", "-y", "50",
            "-e", f"HOME={session.home}",
            f"{inner}; echo '[moonphase] mcp login exited'; sleep {SESSION_TTL_SECONDS}",
        ]
        started = await docker_remote.exec_capture(
            conn, session.container, start_cmd, timeout=60
        )
        if not started.ok:
            session.state = "error"
            session.detail = (started.stderr or started.stdout).strip()[:300]
            return

        session.detail = "Waiting for the authorization link."
        pattern = LOGIN_URL_PATTERN
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            session.pane = await capture(conn, session)
            # The pane hard-wraps long URLs, so rejoin before matching.
            match = pattern.search(session.pane.replace("\n", ""))
            if match:
                session.url = match.group(0).rstrip(")]},.")
                session.state = "awaiting_paste"
                log.info("mcp login %s: authorization url ready", session.id)
                return

        session.state = "error"
        session.detail = (
            f"{session.server_name!r} did not produce an authorization link in "
            "time. It may not need OAuth at all — check whether it just takes "
            "an API key instead."
        )
        await cleanup(conn, session)
    except Exception as exc:  # noqa: BLE001 — the session is the only report
        log.warning("mcp login %s failed while preparing: %s", session.id, exc)
        session.state = "error"
        session.detail = str(exc)[:300]


async def submit_paste(
    conn: asyncssh.SSHClientConnection, session: McpLoginSession, redirect_url: str
) -> McpLoginSession:
    """Type the pasted redirect URL into the waiting prompt and return at once.

    Returning without waiting is the point, same as login.py's submit_code:
    the caller polls `advance` for the outcome rather than holding a request
    open for it.
    """
    if session.state not in {"awaiting_paste", "verifying"}:
        raise SSHError(f"This connection is {session.state}, not waiting for a link.")

    # `-l` sends it literally, so `&` and `?` in the URL are not reinterpreted
    # as tmux key names.
    await docker_remote.exec_capture(
        conn, session.container,
        ["tmux", "send-keys", "-t", session.tmux_session, "-l", redirect_url.strip()],
        timeout=30,
    )
    await asyncio.sleep(0.3)
    await docker_remote.exec_capture(
        conn, session.container,
        ["tmux", "send-keys", "-t", session.tmux_session, "Enter"],
        timeout=30,
    )

    session.state = "verifying"
    session.verifying_since = time.monotonic()
    session.detail = None
    return session


async def advance(
    conn: asyncssh.SSHClientConnection, session: McpLoginSession
) -> McpLoginSession:
    """Perform one non-blocking check on a verifying connection."""
    if session.state != "verifying":
        return session

    session.pane = await capture(conn, session)

    # Checked before the credential file, not after: a pane that plainly says
    # this attempt failed must never be overridden by an entry that happens
    # to be sitting in the file — whether it is stale or genuinely new, a
    # visible failure is the more trustworthy signal about *this* attempt.
    lowered = session.pane.lower()
    if any(
        phrase in lowered
        for phrase in ("couldn't complete authentication", "state mismatch", "invalid")
    ):
        session.state = "error"
        session.detail = _last_nonblank_line(session.pane) or (
            f"{session.server_name!r} rejected that link. Start again."
        )
        await cleanup(conn, session)
        return session

    entry = await _harvest_credential(conn, session)
    if entry is not None:
        session.credential_entry = entry
        session.state = "complete"
        session.detail = None
        log.info("mcp login %s: captured a credential for %s", session.id, session.server_name)
        await cleanup(conn, session)
        return session

    if session.verifying_since and time.monotonic() - session.verifying_since > 60:
        session.state = "error"
        session.detail = (
            "Timed out waiting to finish. The terminal output below shows "
            "where it stopped."
        )
        await cleanup(conn, session)

    return session


async def _read_oauth_entry(
    conn: asyncssh.SSHClientConnection, container: str, home: str, server_name: str
) -> str | None:
    """The `"<server-name>|<hash>": {...}` pair in this container's own
    credentials file right now, whatever it is.

    A plain lookup with no notion of "before" or "after" — used both to
    snapshot what was already there before a relay starts and, later, to see
    what is there now.
    """
    path = f"{home}/.claude/.credentials.json"
    result = await docker_remote.exec_capture(
        conn, container,
        ["sh", "-c", f"cat {shlex.quote(path)} 2>/dev/null"],
        timeout=30,
    )
    if not result.ok or not result.stdout.strip():
        return None
    try:
        doc = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    oauth = doc.get("mcpOAuth")
    if not isinstance(oauth, dict):
        return None

    prefix = f"{server_name}|"
    for key, value in oauth.items():
        if key.startswith(prefix) and isinstance(value, dict) and value.get("accessToken"):
            return json.dumps({key: value})
    return None


async def _harvest_credential(
    conn: asyncssh.SSHClientConnection, session: McpLoginSession
) -> str | None:
    """The `"<server-name>|<hash>": {...}` pair Claude Code wrote for *this*
    attempt, if any — never the one that was already there.

    Read from the session's own credentials file rather than parsed out of the
    pane: the pane only ever says whether it worked, and the file is the
    thing that has to be replayed into every other session afterward anyway.
    Compared against the snapshot taken before this attempt started, so a
    server the org already connected once can't be marked "complete" just
    because its old entry is still sitting there unchanged — only a
    genuinely new or different entry counts as this attempt's own result.
    """
    entry = await _read_oauth_entry(
        conn, session.container, session.home, session.server_name
    )
    if entry is None or entry == session.existing_credential:
        return None
    return entry


def _last_nonblank_line(pane: str) -> str | None:
    for line in reversed(pane.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:300]
    return None


async def capture(conn: asyncssh.SSHClientConnection, session: McpLoginSession) -> str:
    result = await docker_remote.exec_capture(
        conn, session.container,
        ["tmux", "capture-pane", "-p", "-t", session.tmux_session, "-S", "-200"],
        timeout=30,
    )
    return result.stdout if result.ok else ""


async def cleanup(conn: asyncssh.SSHClientConnection, session: McpLoginSession) -> None:
    """Kill the relay's tmux session — never the container, which is the
    real project's, not a throwaway one."""
    if session.cleaned_up:
        return
    session.cleaned_up = True
    try:
        await docker_remote.exec_capture(
            conn, session.container,
            ["tmux", "kill-session", "-t", session.tmux_session],
            timeout=30,
        )
    except SSHError as exc:
        log.warning(
            "could not clean up mcp login tmux session %s: %s", session.tmux_session, exc
        )


async def _supersede(conn: asyncssh.SSHClientConnection, session: McpLoginSession) -> None:
    """Retire this caller's earlier attempts at the same server.

    Only their own and only the same (project, server): someone else
    connecting a different server, or the same server in a different project,
    is unrelated work and must not be cancelled out from under them.
    """
    if session.user_id is None:
        return
    for other in list(_sessions.values()):
        if other is session or other.user_id != session.user_id:
            continue
        if other.project_id != session.project_id or other.server_name != session.server_name:
            continue
        if other.state not in {"starting", "awaiting_paste"}:
            continue
        other.state = "error"
        other.detail = "Replaced by a newer attempt."
        try:
            await cleanup(conn, other)
        except SSHError as exc:
            log.warning("could not retire mcp login %s: %s", other.id, exc)
