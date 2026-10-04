"""Session directories left on disk after their session is gone.

Every session gets `/home/dev/sessions/<name>` as its HOME, and until v0.10.27
closing a session removed only the worktree inside it — every cache, upload
and config file the session ever wrote stayed behind, with no row left to say
whose it was. Closing a session now removes the whole directory, but nothing
reclaims the ones already stranded, and a project that went through a lot of
sessions can be most of a server's disk.

A directory counts as left over only when nothing could still be using it: no
session row claims it, no tmux session of that name is running, and it has
not changed recently. The last two matter because a session's directory and
tmux session are created a moment before its row is written.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass

import asyncssh

from . import docker_remote
from .sessions import SESSIONS_ROOT

log = logging.getLogger(__name__)

# Long enough to cover the gap between a new session's directory appearing
# and its row being written, which includes cloning a branch.
RECENT_GRACE_SECONDS = 15 * 60

# What `sessions.sanitise_name` can produce. Anything else under the root was
# not made by Moonphase and is not ours to delete — and a name from the client
# that fails this can never reach a shell command.
_DIRECTORY_NAME = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


@dataclass(frozen=True)
class SessionDirectory:
    name: str
    bytes: int
    modified_at: float


def is_session_directory_name(name: str) -> bool:
    return bool(_DIRECTORY_NAME.match(name))


def orphaned(
    directories: list[SessionDirectory],
    *,
    claimed_homes: set[str],
    claimed_names: set[str],
    live_tmux: set[str],
    now: float,
    grace_seconds: float = RECENT_GRACE_SECONDS,
) -> list[SessionDirectory]:
    """The directories nothing could still be using, largest first."""
    found = [
        d
        for d in directories
        if is_session_directory_name(d.name)
        and f"{SESSIONS_ROOT}/{d.name}" not in claimed_homes
        and d.name not in claimed_names
        and d.name not in live_tmux
        and now - d.modified_at >= grace_seconds
    ]
    return sorted(found, key=lambda d: d.bytes, reverse=True)


_LIST_SCRIPT = f"""
cd {SESSIONS_ROOT} 2>/dev/null || exit 0
for d in */; do
  d=${{d%/}}
  [ -d "$d" ] || continue
  size=$(du -sb -- "$d" 2>/dev/null | cut -f1)
  printf '%s\\t%s\\t%s\\n' "$d" "${{size:-0}}" "$(stat -c %Y -- "$d")"
done
"""


def parse_listing(stdout: str) -> list[SessionDirectory]:
    out: list[SessionDirectory] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, size, mtime = parts
        try:
            out.append(SessionDirectory(name=name, bytes=int(size), modified_at=float(mtime)))
        except ValueError:
            continue
    return out


async def list_directories(
    conn: asyncssh.SSHClientConnection, container: str
) -> list[SessionDirectory]:
    # As root: a session that ran `sudo` can leave files `dev` cannot read,
    # and `du` would then undercount what deleting it frees.
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", _LIST_SCRIPT], user="root", timeout=600
    )
    result.check("Listing session directories")
    return parse_listing(result.stdout)


async def live_tmux_sessions(
    conn: asyncssh.SSHClientConnection, container: str
) -> set[str]:
    result = await docker_remote.exec_capture(
        conn, container, ["tmux", "list-sessions", "-F", "#{session_name}"], timeout=30
    )
    # No tmux server at all is exit 1 and means no sessions, not an error.
    if not result.ok:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


async def remove(
    conn: asyncssh.SSHClientConnection, container: str, names: list[str]
) -> None:
    """Delete these directories, then let git forget the worktrees inside them.

    Branches are kept, the same as closing a session keeps them.
    """
    safe = [n for n in names if is_session_directory_name(n)]
    if not safe:
        return
    paths = " ".join(shlex.quote(f"{SESSIONS_ROOT}/{n}") for n in safe)
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", f"rm -rf -- {paths}"], user="root", timeout=600
    )
    result.check("Removing leftover session directories")
    await docker_remote.exec_capture(
        conn,
        container,
        ["sh", "-c", "cd /workspace 2>/dev/null && git worktree prune || true"],
        timeout=60,
    )
