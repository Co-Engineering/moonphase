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
import uuid
from typing import NamedTuple

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

# A start commit is a full object name and nothing shorter: anything else goes
# through git's name lookup, where a tag or a branch can answer instead.
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def is_safe_branch(name: str) -> bool:
    """Whether `name` may be passed to git as a branch.

    Must start with a letter or digit — which is what keeps an option out of a
    refspec position — and git's own rules bar the rest: no `..`, no trailing
    `/` or `.lock`, no component beginning with a dot.
    """
    # HEAD names no branch; as refs/remotes/origin/HEAD it is the symref to
    # origin's default branch.
    if not _SAFE_BRANCH.match(name) or name == "HEAD":
        return False
    if ".." in name or name.endswith("/") or name.endswith(".lock"):
        return False
    return not any(part.startswith(".") or part == "" for part in name.split("/"))


def workdir_for(session: str) -> str:
    return f"{SESSIONS_ROOT}/{session}/work"


def branch_for(session: str) -> str:
    return f"{BRANCH_PREFIX}{session}"


def wrap_for_login_shell(script: str) -> str:
    """`script`, made safe to run under `bash -lc`.

    Run in a subshell whose status is handed back with errexit switched off,
    so nothing the logout script does can become the answer. Failures and
    their output pass through untouched — the point is only that a success
    stays a success.
    """
    return f"(\n{script}\n)\nrc=$?\nset +e\nexit $rc\n"


async def _run(
    conn: asyncssh.SSHClientConnection,
    container: str,
    script: str,
    *,
    timeout: int = 120,
) -> ssh.CommandResult:
    # A login shell, because the harness's toolchain is on the PATH the
    # profile sets — and a login shell runs ~/.bash_logout on the way out.
    # Debian's copies it into every home and it calls `clear_console`, which
    # fails when there is no console, which a `docker exec` without a TTY has
    # none of. With `set -e` still in force that failure became the shell's
    # exit status: a script that ran perfectly reported exit 1 with nothing on
    # either stream.
    #
    #   bash -lc 'set -e; exit 0'   -> 1
    #   bash -lc 'set -e; true'     -> 0
    #
    # It only bit a script that exits explicitly, so `ensure_worktree` failed
    # on precisely the path where it had nothing to do — the worktree already
    # existed — and "Restart harness" failed for every session that had been
    # started once before.
    #
    return await docker_remote.exec_capture(
        conn, container, ["bash", "-lc", wrap_for_login_shell(script)], timeout=timeout
    )


# Where a fetch's credentials live for the length of one command. Outside the
# workspace volume on purpose, same reasoning as the clone's own: the volume
# outlives this and is what the agent works in, and a token sitting in it
# would outlive the reason for it.
#
# One file per call. A single shared path let two calls at once — a session
# starting while the branch list loads — delete each other's credential
# mid-fetch.
_FETCH_CREDENTIALS_PREFIX = "/tmp/.moonphase-fetch-credentials-"


def _fetch_credential_path() -> str:
    return f"{_FETCH_CREDENTIALS_PREFIX}{uuid.uuid4().hex}"


async def _write_fetch_credential(
    conn: asyncssh.SSHClientConnection, container: str, path: str, token: str
) -> None:
    command = (
        f"docker exec -i -u dev {shlex.quote(container)} sh -c "
        + shlex.quote(f"umask 077 && cat > {path}")
    )
    result = await ssh.run(
        conn,
        command,
        timeout=30,
        stdin=f"https://x-access-token:{token}@github.com\n",
    )
    result.check("Writing a temporary git credential")


async def _clear_fetch_credential(
    conn: asyncssh.SSHClientConnection, container: str, path: str
) -> None:
    await docker_remote.exec_capture(conn, container, ["rm", "-f", path], timeout=30)


def _git_env_flags(credential_file: str | None) -> str:
    """`-c` flags that make a `git` invocation use the fetch credential, if any.

    Mirrors the clone's own: `core.askPass=` so a private repo fails fast
    instead of hanging on a prompt nothing can answer, the credential helper
    only when there is a token to offer, and GitHub's ssh addresses rewritten
    to https — there is no key in the container, so an `origin` written as
    `git@github.com:` could otherwise never be fetched at all.
    """
    flags = (
        "-c core.askPass="
        " -c url.https://github.com/.insteadOf=git@github.com:"
        " -c url.https://github.com/.insteadOf=ssh://git@github.com/"
    )
    if credential_file:
        flags += f" -c {shlex.quote(f'credential.helper=store --file={credential_file}')}"
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
    result.check("Preparing the workspace repository")


class Worktree(NamedTuple):
    workdir: str
    branch: str
    # What `branch` was created from — a full ref or a commit SHA. None when a
    # branch already on disk was picked up instead of created.
    base: str | None
    # Anything about where the session started that its owner should be told,
    # in plain words: a fetch that failed, local commits origin's copy does
    # not have, a requested start that could not be honoured.
    note: str | None


# Plain text rather than an f-string: every value arrives as a shell variable
# assigned in a quoted header, so nothing in here is interpolated by Python.
_WORKTREE_BODY = r"""
set -e
note() { printf 'MOONPHASE_NOTE=%s\n' "$*"; }
# Without the lock's descriptor: whatever git leaves running, a detached
# `gc --auto` say, would otherwise go on holding the lock after this exits.
git() { command git "$@" 9>&-; }
has() { git show-ref --verify --quiet "$1"; }

if [ -n "$START_COMMIT" ]; then
  REQUESTED="commit $(printf '%.12s' "$START_COMMIT")"
else
  REQUESTED="$START_BRANCH"
fi
reused() {
  if [ -n "$REQUESTED" ]; then
    note "$BRANCH already existed, so this session carries on from it as it was" \
      "rather than starting from $REQUESTED."
  fi
}

mkdir -p "$(dirname "$WORKDIR")"
cd "$REPO_ROOT"

# Two sessions starting at once would otherwise fetch into the same refs and
# edit the same worktree bookkeeping concurrently.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$(git rev-parse --git-common-dir)/moonphase-worktree.lock"
  if ! flock -w 100 9; then
    echo "Timed out waiting for another session's checkout to be created." >&2
    exit 1
  fi
fi

# Already a worktree of this repository? Nothing to do.
if git worktree list --porcelain | grep -qxF "worktree $WORKDIR"; then
  reused
  exit 0
fi
# A stale directory from an interrupted attempt would make `worktree add`
# refuse; prune first, and only remove it if git does not know about it.
git worktree prune
if [ -d "$WORKDIR" ]; then
  rmdir "$WORKDIR" 2>/dev/null || true
fi
# Never reset: it may hold the only copy of somebody's work.
if has "refs/heads/$BRANCH"; then
  reused
  git worktree add "$WORKDIR" "$BRANCH"
  # Closing the session took its record with the old worktree; where the
  # branch was created is the best start the Changes view can still have.
  RECORD="$(git -C "$WORKDIR" rev-parse --absolute-git-dir)/moonphase-base"
  if [ ! -f "$RECORD" ]; then
    OLDEST=$(git reflog show --format='%H %gs' "refs/heads/$BRANCH" -- 2>/dev/null | tail -n 1)
    case "${OLDEST#* }" in
      "branch: Created from"*) printf '\n%s\n' "${OLDEST%% *}" > "$RECORD" ;;
    esac
  fi
  exit 0
fi

X=""
if [ -n "$START_COMMIT" ]; then
  if ! git cat-file -e "$START_COMMIT^{commit}" 2>/dev/null; then
    echo "Commit $START_COMMIT is not in this repository." >&2
    exit 1
  fi
  START="$START_COMMIT"
elif [ -n "$START_BRANCH" ]; then
  X="$START_BRANCH"
else
  X=$(git symbolic-ref --short -q HEAD || true)
  if [ -z "$X" ]; then
    if ! START=$(git rev-parse --verify -q 'HEAD^{commit}'); then
      echo "$REPO_ROOT has no commit to start from." >&2
      exit 1
    fi
  fi
fi

if [ -n "$X" ]; then
  # Full refnames only. A bare name goes through git's lookup order, where a
  # tag of the same name answers before the branch — and a refname cannot be
  # mistaken for an option, whatever the branch is called.
  LOCAL="refs/heads/$X"
  REMOTE="refs/remotes/origin/$X"
  FETCH_FAILED=""
  FETCH_DETAIL=""
  MISSING=""
  GONE=""
  if git remote get-url origin >/dev/null 2>&1; then
    # Shallow only for a branch with no history here yet. A refresh has to
    # join up with what is already here, or whether local is behind origin
    # or ahead of it cannot be told.
    DEPTH=""
    if ! has "$LOCAL" && ! has "$REMOTE"; then
      DEPTH="--depth=50"
    fi
    TIMEOUT=""
    if command -v timeout >/dev/null 2>&1; then
      TIMEOUT="timeout 60"
    fi
    rc=0
    ERR=$(GIT_TERMINAL_PROMPT=0 $TIMEOUT git "${GIT_FLAGS[@]}" fetch --quiet --no-tags \
      $DEPTH origin "+$LOCAL:$REMOTE" 2>&1 >/dev/null 9>&-) || rc=$?
    if [ "$rc" != 0 ]; then
      if [ "$rc" = 124 ]; then
        ERR="timed out after 60 seconds"
      fi
      ERR="${ERR:-git fetch exited $rc}"
      case "$ERR" in
        *"couldn't find remote ref"*) MISSING=1 ;;
      esac
      # The first line for a note, all of it for an error.
      FETCH_FAILED=$(printf '%s\n' "$ERR" \
        | sed -n '/[^[:space:]]/{s/^fatal: //;p;q;}' | cut -c1-200)
      FETCH_FAILED="${FETCH_FAILED:-git fetch failed}"
      FETCH_DETAIL=$(printf '%s' "$ERR" | tr -s '\n\t ' ' ' | cut -c1-400)
    fi
  fi
  # Origin deleted it. Its leftover copy would otherwise be started from as if
  # it were current.
  if [ -n "$MISSING" ] && has "$REMOTE"; then
    git update-ref --no-deref -d "$REMOTE"
    GONE=1
  fi

  # Origin's copy whenever there is one. Whether a local branch holds work of
  # its own cannot be told reliably in a shallow clone (force-pushes, shallow
  # cuts, expired reflogs); whatever it holds stays on it.
  if has "$REMOTE"; then
    START="$REMOTE"
  elif has "$LOCAL"; then
    START="$LOCAL"
    if [ -n "$GONE" ]; then
      note "$X no longer exists on origin, so this session starts from the local copy."
    fi
  else
    echo "No branch called '$X' was found locally or on origin.${FETCH_DETAIL:+ $FETCH_DETAIL}" >&2
    exit 1
  fi
  # A branch that is simply not on origin (local-only, or a clone of an empty
  # repository) has nothing to refresh, so nothing to say.
  if [ -n "$FETCH_FAILED" ] && [ -z "$MISSING" ]; then
    note "Could not refresh $X from origin ($FETCH_FAILED), so this session starts" \
      "from the copy already here, which may be behind."
  fi
fi

BASE_SHA=$(git rev-parse --verify -q "$START^{commit}")
git worktree add --no-track -b "$BRANCH" "$WORKDIR" "$BASE_SHA"
# What the Changes view diffs from. In the worktree's own git dir rather than
# keyed by branch, so the agent switching branches inside it keeps it.
printf '%s\n%s\n' "$START" "$BASE_SHA" \
  > "$(git -C "$WORKDIR" rev-parse --absolute-git-dir)/moonphase-base"
printf 'MOONPHASE_BASE=%s\n' "$START"
"""


def worktree_script(
    workdir: str,
    branch: str,
    *,
    repo_root: str = REPO_ROOT,
    start_branch: str | None = None,
    start_commit: str | None = None,
    credential_file: str | None = None,
) -> str:
    """The shell script behind `ensure_worktree`, on its own so it can be run
    against a real repository without a container."""
    if start_branch and start_commit:
        raise ValueError("A worktree starts from a branch or a commit, not both.")
    # Checked here as well as at the edge: this is the function that builds
    # the command line, so this is where the guarantee has to hold.
    if start_branch and not is_safe_branch(start_branch):
        raise SSHError(f"{start_branch!r} is not a usable branch name.")
    if start_commit and not _FULL_SHA.fullmatch(start_commit):
        raise SSHError(f"{start_commit!r} is not a full commit SHA.")
    header = "\n".join(
        [
            f"REPO_ROOT={shlex.quote(repo_root)}",
            f"WORKDIR={shlex.quote(workdir)}",
            f"BRANCH={shlex.quote(branch)}",
            f"START_BRANCH={shlex.quote(start_branch or '')}",
            f"START_COMMIT={shlex.quote(start_commit or '')}",
            f"GIT_FLAGS=({_git_env_flags(credential_file)})",
        ]
    )
    return header + _WORKTREE_BODY


def read_worktree_output(stdout: str) -> tuple[str | None, str | None]:
    """(base, note) from what `worktree_script` printed, among git's own output."""
    base: str | None = None
    notes: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("MOONPHASE_BASE="):
            base = line.partition("=")[2].strip() or None
        elif line.startswith("MOONPHASE_NOTE="):
            text = line.partition("=")[2].strip()
            if text:
                notes.append(text)
    return base, " ".join(notes) or None


async def ensure_worktree(
    conn: asyncssh.SSHClientConnection,
    container: str,
    session: str,
    *,
    author_name: str = "Moonphase",
    author_email: str = "moonphase@localhost",
    start_branch: str | None = None,
    start_commit: str | None = None,
    token: str | None = None,
) -> Worktree:
    """Give `session` its own checkout.

    Idempotent, and deliberately tolerant of the two states a half-finished
    previous attempt can leave behind: a directory with no branch, and a branch
    with no directory.

    Where it starts only matters the first time: once `moonphase/<session>`
    exists it is checked out as-is, on the reasoning that a name reused on
    purpose means picking up where that branch left off, not overwriting it
    with a fresh start. The returned note says so if a start was asked for.

    `start_branch` — or, left out, the branch `/workspace` is on — is
    refreshed from origin first. The clone's own copy of its default branch is
    never updated after cloning, so starting from it as it stood put every new
    session behind by however much had landed since. The clone is also shallow
    and single-branch, so most other branches are not on disk at all; `token`
    is the caller's GitHub credential, so a private repo's can be fetched.
    Origin's copy is started from whenever origin has the branch; the local
    one only when it does not.

    `start_commit` is a full commit SHA — the output of `snapshot_worktree`,
    say. A SHA is always already local (every session's worktree shares one
    object database), so it is used as it is, without a fetch.
    """
    workdir = workdir_for(session)
    branch = branch_for(session)
    credential_file = _fetch_credential_path() if token and not start_commit else None
    script = worktree_script(
        workdir,
        branch,
        start_branch=start_branch,
        start_commit=start_commit,
        credential_file=credential_file,
    )

    await ensure_repository(
        conn, container, author_name=author_name, author_email=author_email
    )
    if credential_file and token:
        await _write_fetch_credential(conn, container, credential_file, token)
    try:
        result = await _run(conn, container, script, timeout=180)
    finally:
        if credential_file:
            await _clear_fetch_credential(conn, container, credential_file)
    result.check(f"Creating a working directory for session {session!r}")
    base, note = read_worktree_output(result.stdout)
    return Worktree(workdir, branch, base, note)


async def snapshot_worktree(
    conn: asyncssh.SSHClientConnection,
    container: str,
    workdir: str,
    *,
    author_name: str,
    author_email: str,
) -> str:
    """A commit capturing `workdir` exactly as it is right now. Returns its SHA.

    Staged, unstaged and untracked files all go in, the same as a save point —
    but nothing here touches `workdir` itself: this is for handing another
    worktree a starting point, not for the session it runs. `GIT_INDEX_FILE`
    points `add` and `write-tree` at a throwaway copy of the real index, and
    `commit-tree` makes the commit directly, so the real index, HEAD and the
    working tree are never in the blast radius even if this fails halfway.

    Returns the current HEAD unchanged if nothing was dirty, rather than
    manufacturing an empty commit.
    """
    d = shlex.quote(workdir)
    quoted_name = shlex.quote(author_name)
    quoted_email = shlex.quote(author_email)
    script = f"""
set -e
cd {d}
HEAD=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain -uall)" ]; then
  REAL_INDEX=$(git rev-parse --git-path index)
  SCRATCH_INDEX=$(mktemp)
  cp "$REAL_INDEX" "$SCRATCH_INDEX"
  GIT_INDEX_FILE="$SCRATCH_INDEX" git add -A
  TREE=$(GIT_INDEX_FILE="$SCRATCH_INDEX" git write-tree)
  rm -f "$SCRATCH_INDEX"
  git -c user.name={quoted_name} -c user.email={quoted_email} \
      commit-tree "$TREE" -p "$HEAD" -m 'Moonphase: duplicate-session snapshot'
else
  echo "$HEAD"
fi
"""
    result = await _run(conn, container, script)
    result.check(f"Capturing the current state of {workdir!r}")
    return result.stdout.strip().splitlines()[-1]


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
    credential_file = _fetch_credential_path() if token else None
    script = f"""
cd {REPO_ROOT} 2>/dev/null || exit 1
git rev-parse --abbrev-ref HEAD 2>/dev/null
git for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null \\
  | grep -v '^{BRANCH_PREFIX}' || true
GIT_TERMINAL_PROMPT=0 git {_git_env_flags(credential_file)} ls-remote --heads origin 2>/dev/null \\
  | sed -E 's#.*refs/heads/##'
"""
    if credential_file and token:
        await _write_fetch_credential(conn, container, credential_file, token)
    try:
        result = await _run(conn, container, script, timeout=30)
    finally:
        if credential_file:
            await _clear_fetch_credential(conn, container, credential_file)
    if not result.ok:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        if not name or name in seen or name == "HEAD":
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
