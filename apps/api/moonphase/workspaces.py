"""Per-session working directories, as git worktrees.

Two people sharing a project share the code, not the file handles. If both
their agents edited `/workspace` at once they would overwrite each other
mid-thought, and the damage would be invisible until something failed to build.

So `/workspace` is the repository, and every session gets a worktree beside it
on a branch of its own. Sharing work then means merging, which is a problem git
already solved and which people already know how to reason about.

The one wrinkle is that a project need not have started as a repository — an
empty project is just a directory. Rather than special-casing that forever, the
first session initialises one. A workspace you can branch and merge is the
better default anyway, and it costs an empty commit.
"""

from __future__ import annotations

import logging
import shlex

import asyncssh

from . import docker_remote, ssh
from .ssh import SSHError

log = logging.getLogger(__name__)

REPO_ROOT = "/workspace"

# Worktrees live inside the session directory they belong to, not beside
# `/workspace`. Two reasons: the filesystem root is not writable by the
# container's user, and keeping a session's checkout next to its home means
# everything one person owns is in one place, on the volume that persists.
SESSIONS_ROOT = "/home/dev/sessions"

# Branch names are prefixed so a session called `main` cannot collide with the
# branch the repository is already on, and so `git branch` makes it obvious
# which branches Moonphase created.
BRANCH_PREFIX = "moonphase/"


def workdir_for(session: str) -> str:
    return f"{SESSIONS_ROOT}/{session}/work"


def branch_for(session: str) -> str:
    return f"{BRANCH_PREFIX}{session}"


async def _run(
    conn: asyncssh.SSHClientConnection,
    container: str,
    script: str,
    *,
    timeout: int = 120,
) -> ssh.CommandResult:
    return await docker_remote.exec_capture(
        conn, container, ["bash", "-lc", script], timeout=timeout
    )


async def ensure_repository(
    conn: asyncssh.SSHClientConnection,
    container: str,
    *,
    author_name: str = "Moonphase",
    author_email: str = "moonphase@localhost",
) -> None:
    """Make `/workspace` a git repository with at least one commit.

    Worktrees need a commit to branch from, so an empty project gets one. The
    identity is passed explicitly rather than read from config: this may run
    before any user identity has been written, and git refuses to commit
    without one.
    """
    quoted_name = shlex.quote(author_name)
    quoted_email = shlex.quote(author_email)
    script = f"""
set -e
cd {REPO_ROOT}
if [ ! -d .git ]; then
  git init -q -b main
fi
if ! git rev-parse HEAD >/dev/null 2>&1; then
  git -c user.name={quoted_name} -c user.email={quoted_email} \
      commit -q --allow-empty -m 'Moonphase: initial commit'
fi
"""
    result = await _run(conn, container, script)
    if not result.ok:
        raise SSHError(
            "Could not prepare the workspace repository: "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )


async def ensure_worktree(
    conn: asyncssh.SSHClientConnection,
    container: str,
    session: str,
    *,
    author_name: str = "Moonphase",
    author_email: str = "moonphase@localhost",
) -> tuple[str, str]:
    """Give `session` its own checkout. Returns (workdir, branch).

    Idempotent, and deliberately tolerant of the two states a half-finished
    previous attempt can leave behind: a directory with no branch, and a branch
    with no directory.
    """
    await ensure_repository(
        conn, container, author_name=author_name, author_email=author_email
    )

    workdir = workdir_for(session)
    branch = branch_for(session)
    quoted_dir = shlex.quote(workdir)
    quoted_branch = shlex.quote(branch)

    script = f"""
set -e
mkdir -p {shlex.quote(workdir.rsplit("/", 1)[0])}
cd {REPO_ROOT}
# Already a worktree of this repository? Nothing to do.
if git worktree list --porcelain | grep -qx "worktree {workdir}"; then
  exit 0
fi
# A stale directory from an interrupted attempt would make `worktree add`
# refuse; prune first, and only remove it if git does not know about it.
git worktree prune
if [ -d {quoted_dir} ] && ! git worktree list --porcelain | grep -qx "worktree {workdir}"; then
  rmdir {quoted_dir} 2>/dev/null || true
fi
if git show-ref --verify --quiet refs/heads/{quoted_branch}; then
  git worktree add {quoted_dir} {quoted_branch}
else
  git worktree add -b {quoted_branch} {quoted_dir}
fi
"""
    result = await _run(conn, container, script, timeout=180)
    if not result.ok:
        raise SSHError(
            f"Could not create a working directory for session {session!r}: "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    return workdir, branch


async def remove_worktree(
    conn: asyncssh.SSHClientConnection, container: str, session: str
) -> None:
    """Drop a session's checkout, keeping its branch.

    Deleting the branch too would throw away work whose only copy is that
    branch, which is not a decision a "close this session" button should be
    making. The branch stays; the directory goes.
    """
    workdir = shlex.quote(workdir_for(session))
    script = f"""
cd {REPO_ROOT} 2>/dev/null || exit 0
git worktree remove --force {workdir} 2>/dev/null || rm -rf {workdir}
git worktree prune
"""
    result = await _run(conn, container, script)
    if not result.ok:
        log.warning(
            "could not remove worktree for %s: %s",
            session,
            (result.stderr or result.stdout).strip()[:200],
        )


async def status(
    conn: asyncssh.SSHClientConnection, container: str, workdir: str
) -> dict[str, str | int]:
    """Branch, and how far it has diverged — enough for the UI to say so."""
    script = f"""
cd {shlex.quote(workdir)} 2>/dev/null || exit 1
printf 'branch=%s\\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
printf 'dirty=%s\\n' "$(git status --porcelain 2>/dev/null | wc -l)"
printf 'ahead=%s\\n' "$(git rev-list --count main..HEAD 2>/dev/null || echo 0)"
"""
    result = await _run(conn, container, script, timeout=60)
    if not result.ok:
        return {}
    out: dict[str, str | int] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in ("dirty", "ahead"):
            out[key] = int(value or 0)
        elif key:
            out[key] = value
    return out
