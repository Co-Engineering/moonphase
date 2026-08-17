"""What an agent actually changed.

The feed says what it did and the terminal says what it is doing. Neither
answers the question you have after leaving it alone for an hour, which is
"what is different now" — and the honest answer to that is a diff, not a
summary of one.

Read from the session's own worktree, against the point it branched from, so
what comes back is the whole change and not just the part that happens to be
committed. An agent that has written twenty files and committed none has still
changed twenty files, and a review screen that shows nothing would be worse
than no review screen at all.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field

import asyncssh

from . import docker_remote

log = logging.getLogger(__name__)

# Enough to review a session's work, bounded so one runaway refactor cannot
# push megabytes through a websocket into a phone.
MAX_PATCH_BYTES = 400_000

_SECTION = "###MOONPHASE-"


@dataclass
class ChangedFile:
    path: str
    added: int = 0
    removed: int = 0
    # 'untracked' for a file git has never seen; it has no diff to show but is
    # very much part of what changed.
    status: str = "modified"


@dataclass
class Changes:
    branch: str = ""
    base: str = ""
    files: list[ChangedFile] = field(default_factory=list)
    patch: str = ""
    truncated: bool = False
    # Set when the directory is not a git repository at all, which is a normal
    # state for a scratch project and not an error.
    detail: str | None = None

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)


def _script(workdir: str, limit: int) -> str:
    """One shell round trip for branch, base, per-file stats and the patch.

    Asking separately would be four execs into a container over SSH for a view
    that is polled, which is the kind of cost that makes a feature not worth
    having.
    """
    d = shlex.quote(workdir)
    return f"""
cd {d} 2>/dev/null || {{ echo "{_SECTION}ERROR"; echo "no such directory"; exit 0; }}
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {{
  echo "{_SECTION}ERROR"; echo "not a git repository"; exit 0; }}

# `git rev-parse --abbrev-ref origin/HEAD` prints "origin/HEAD" on stdout while
# failing when the ref does not exist, which is how the base once came back as
# the literal string "HEAD" and every diff was empty. symbolic-ref either
# resolves or says nothing.
BASE=$(git symbolic-ref --short -q refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
if [ -z "$BASE" ] || [ "$BASE" = "HEAD" ]; then
  BASE=""
  for b in main master trunk; do
    git show-ref -q --verify "refs/heads/$b" && BASE="$b" && break
  done
fi
# Compare against where this branch left the base, not against the base as it
# is now: work someone else landed meanwhile is not this session's doing.
REF=$(git merge-base HEAD "$BASE" 2>/dev/null)
[ -n "$REF" ] || REF=$(git rev-list --max-parents=0 HEAD 2>/dev/null | tail -1)

echo "{_SECTION}BRANCH"
git rev-parse --abbrev-ref HEAD 2>/dev/null
echo "{_SECTION}BASE"
echo "$BASE"
echo "{_SECTION}STAT"
git diff --numstat "$REF" 2>/dev/null
echo "{_SECTION}UNTRACKED"
git ls-files --others --exclude-standard 2>/dev/null
echo "{_SECTION}PATCH"
git diff "$REF" 2>/dev/null | head -c {limit}
"""


def parse(stdout: str) -> Changes:
    """Split the sections back apart.

    Tolerant of missing sections: a repository with no commits produces some of
    them and not others, and that is a state to render rather than an error.
    """
    changes = Changes()
    section = ""
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body)
        if section == "BRANCH":
            changes.branch = text.strip()
        elif section == "BASE":
            changes.base = text.strip()
        elif section == "ERROR":
            changes.detail = text.strip() or "Could not read the worktree."
        elif section == "STAT":
            for line in text.splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                added, removed, path = parts
                changes.files.append(
                    ChangedFile(
                        path=path.strip(),
                        # Binary files report '-' rather than a count.
                        added=int(added) if added.isdigit() else 0,
                        removed=int(removed) if removed.isdigit() else 0,
                        status="binary" if not added.isdigit() else "modified",
                    )
                )
        elif section == "UNTRACKED":
            for line in text.splitlines():
                path = line.strip()
                if path:
                    changes.files.append(ChangedFile(path=path, status="untracked"))
        elif section == "PATCH":
            changes.patch = text

    for line in stdout.splitlines():
        if line.startswith(_SECTION):
            flush()
            section = line[len(_SECTION) :].strip()
            body = []
            continue
        body.append(line)
    flush()

    changes.files.sort(key=lambda f: (f.status == "untracked", -(f.added + f.removed)))
    changes.truncated = len(changes.patch.encode("utf-8", "ignore")) >= MAX_PATCH_BYTES
    return changes


async def read(
    conn: asyncssh.SSHClientConnection,
    container: str,
    workdir: str,
    *,
    limit: int = MAX_PATCH_BYTES,
) -> Changes:
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", _script(workdir, limit)], timeout=60
    )
    if not result.ok and not result.stdout:
        return Changes(detail=(result.stderr or "Could not read the worktree.")[:200])
    return parse(result.stdout)
