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
import re
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

# What a branch name is allowed to be before it reaches a `git` command line.
#
# Shell-quoting is not enough on its own. `git fetch origin <refspec>` parses
# an argument beginning with `-` as an option however carefully it is quoted,
# and `--upload-pack=<command>` runs that command — so a branch name chosen by
# whoever starts a session was arbitrary code in the project's container. This
# is the check that closes that, and it is deliberately narrower than git's own
# rules: the names it rejects are ones nobody has.
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


def is_safe_branch(name: str) -> bool:
    """Whether `name` may be passed to git as a branch.

    Must start with a letter or digit — which is what keeps an option out of a
    refspec position — and git's own rules bar the rest: no `..`, no trailing
    `/` or `.lock`, no component beginning with a dot.
    """
    if not _SAFE_BRANCH.match(name):
        return False
    if ".." in name or name.endswith("/") or name.endswith(".lock"):
        return False
    return not any(part.startswith(".") or part == "" for part in name.split("/"))


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


# Where a fetch's credentials live for the length of one command. Outside the
# workspace volume on purpose, same reasoning as the clone's own: the volume
# outlives this and is what the agent works in, and a token sitting in it
# would outlive the reason for it.
_FETCH_CREDENTIALS = "/tmp/.moonphase-fetch-credentials"


async def _write_fetch_credential(
    conn: asyncssh.SSHClientConnection, container: str, token: str
) -> None:
    command = (
        f"docker exec -i -u dev {shlex.quote(container)} sh -c "
        + shlex.quote(f"cat > {_FETCH_CREDENTIALS} && chmod 600 {_FETCH_CREDENTIALS}")
    )
    result = await ssh.run(
        conn,
        command,
        timeout=30,
        stdin=f"https://x-access-token:{token}@github.com\n",
    )
    result.check("Writing a temporary git credential")


async def _clear_fetch_credential(
    conn: asyncssh.SSHClientConnection, container: str
) -> None:
    await docker_remote.exec_capture(
        conn, container, ["rm", "-f", _FETCH_CREDENTIALS], timeout=30
    )


def _git_env_flags(token: str | None) -> str:
    """`-c` flags that make a `git` invocation use the fetch credential, if any.

    Mirrors the clone's own: `core.askPass=` so a private repo fails fast
    instead of hanging on a prompt nothing can answer, and the credential
    helper only when there is a token to offer.
    """
    flags = "-c core.askPass="
    if token:
        flags += f" -c credential.helper={shlex.quote(f'store --file={_FETCH_CREDENTIALS}')}"
    return flags


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
    start_point: str | None = None,
    token: str | None = None,
) -> tuple[str, str]:
    """Give `session` its own checkout. Returns (workdir, branch).

    Idempotent, and deliberately tolerant of the two states a half-finished
    previous attempt can leave behind: a directory with no branch, and a branch
    with no directory.

    `start_point` only matters the first time: once `moonphase/<session>` exists
    it is checked out as-is, on the reasoning that a name reused on purpose
    means picking up where that branch left off, not overwriting it with a
    fresh start silently. Because the clone is shallow and single-branch, a
    `start_point` that is not the repository's default branch usually is not on
    disk yet — `token` is the caller's GitHub credential, so a private repo's
    other branches can still be fetched.
    """
    await ensure_repository(
        conn, container, author_name=author_name, author_email=author_email
    )

    workdir = workdir_for(session)
    branch = branch_for(session)
    quoted_dir = shlex.quote(workdir)
    quoted_branch = shlex.quote(branch)

    start_clause = ""
    if start_point:
        # Checked here as well as at the edge: this is the function that builds
        # the command line, so this is where the guarantee has to hold.
        if not is_safe_branch(start_point):
            raise SSHError(f"{start_point!r} is not a usable branch name.")
        quoted_start = shlex.quote(start_point)
        start_clause = f"""
  if ! git show-ref --verify --quiet refs/heads/{quoted_start} && \\
     ! git show-ref --verify --quiet refs/remotes/origin/{quoted_start}; then
    GIT_TERMINAL_PROMPT=0 git {_git_env_flags(token)} fetch --quiet --depth 50 \\
      origin {quoted_start}:refs/remotes/origin/{quoted_start} 2>/dev/null || true
  fi
  if git show-ref --verify --quiet refs/heads/{quoted_start}; then
    START_REF={quoted_start}
  elif git show-ref --verify --quiet refs/remotes/origin/{quoted_start}; then
    START_REF=origin/{quoted_start}
  else
    echo "No branch called {start_point!r} was found locally or on origin." >&2
    exit 1
  fi
"""

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
{start_clause}
  git worktree add -b {quoted_branch} {quoted_dir} ${{START_REF:-}}
fi
"""
    if token:
        await _write_fetch_credential(conn, container, token)
    try:
        result = await _run(conn, container, script, timeout=180)
    finally:
        if token:
            await _clear_fetch_credential(conn, container)
    if not result.ok:
        raise SSHError(
            f"Could not create a working directory for session {session!r}: "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    return workdir, branch


async def list_branches(
    conn: asyncssh.SSHClientConnection,
    container: str,
    *,
    token: str | None = None,
) -> list[str]:
    """Branch names worth offering as a session's starting point.

    The current branch first, then whatever else is already local, then
    everything on the remote. The clone is shallow and single-branch, so
    nothing besides the default branch's own history exists on disk yet —
    `ls-remote` asks the remote only for ref names, which costs nothing like a
    fetch would, so the list is complete even though most of it is not fetched.
    Internal `moonphase/*` session branches are left out; they are checkouts of
    somebody's session, not places to start a new one from.
    """
    script = f"""
cd {REPO_ROOT} 2>/dev/null || exit 1
git rev-parse --abbrev-ref HEAD 2>/dev/null
git for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null \\
  | grep -v '^{BRANCH_PREFIX}' || true
GIT_TERMINAL_PROMPT=0 git {_git_env_flags(token)} ls-remote --heads origin 2>/dev/null \\
  | sed -E 's#.*refs/heads/##'
"""
    if token:
        await _write_fetch_credential(conn, container, token)
    try:
        result = await _run(conn, container, script, timeout=30)
    finally:
        if token:
            await _clear_fetch_credential(conn, container)
    if not result.ok:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


async def existing_branch_names(
    conn: asyncssh.SSHClientConnection, container: str
) -> set[str]:
    """Session names whose `moonphase/*` branch is still on disk, closed or not.

    Used to keep an auto-generated session name from quietly reusing a branch
    left over from a session of the same name that was closed earlier: that
    name is deterministic (it comes from who is asking), so without this check
    every session someone opens without typing a name would keep resuming
    their very first one instead of starting clean.
    """
    script = f"""
cd {REPO_ROOT} 2>/dev/null || exit 0
git for-each-ref --format='%(refname:short)' refs/heads/{BRANCH_PREFIX.rstrip("/")}
"""
    result = await _run(conn, container, script, timeout=30)
    if not result.ok:
        return set()
    return {
        line.strip()[len(BRANCH_PREFIX) :]
        for line in result.stdout.splitlines()
        if line.strip().startswith(BRANCH_PREFIX)
    }


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
