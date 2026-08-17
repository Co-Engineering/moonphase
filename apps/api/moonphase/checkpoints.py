"""Save points, which are commits without the vocabulary.

The thing that frightens someone who does not know git is not that the agent
will fail — it is that it will succeed at the wrong thing and they will have no
way back. They are right to be frightened, because until now there wasn't one:
the agent commits when it feels like it, and everything else lives in a
worktree nobody has a handle on.

So: a button that saves where you are, a list of the places you have saved, and
a button that puts you back at one. Underneath it is `git commit` and `git
restore`, and the words never appear.

The rule that makes this safe enough to hand to someone who cannot inspect it:
**going back never destroys anything.** Restoring first saves the current state
as its own point, then moves the files, then records the move. Every state the
project has been in stays reachable, so "undo" has an undo. That costs one
extra commit and buys the entire feature its confidence.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncssh

from . import docker_remote

log = logging.getLogger(__name__)

# Save points Moonphase made, kept apart from whatever the agent commits on its
# own so the list is a record of the person's decisions rather than a changelog.
TRAILER = "Moonphase-Checkpoint:"

# Fields, tab-separated, in the order `git log` is asked for them.
_FORMAT = "%H%x09%aI%x09%s"

MAX_POINTS = 50


@dataclass
class Checkpoint:
    id: str
    at: str
    label: str
    # True for the point that reflects the files as they are now.
    current: bool = False
    automatic: bool = False


@dataclass
class Board:
    """Everything the save-point panel needs in one round trip."""

    points: list[Checkpoint] = field(default_factory=list)
    # Files changed since the most recent save point, so "you have unsaved
    # work" is a fact rather than a guess.
    unsaved: int = 0
    detail: str | None = None


def _identity() -> str:
    """Commit as the person, falling back rather than failing.

    A worktree whose git identity was never configured would otherwise refuse
    the commit, and "your save failed because user.email is not set" is exactly
    the sentence this feature exists to avoid.
    """
    return (
        '-c user.name="${GIT_AUTHOR_NAME:-Moonphase}" '
        '-c user.email="${GIT_AUTHOR_EMAIL:-moonphase@localhost}"'
    )


def parse_board(stdout: str) -> Board:
    """Read the log and the dirty-file count back out."""
    board = Board()
    section = ""
    for line in stdout.splitlines():
        if line.startswith("###"):
            section = line[3:].strip()
            continue
        if section == "ERROR" and line.strip():
            board.detail = line.strip()
        elif section == "LOG" and line.strip():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            sha, at, subject = parts
            label = subject.strip()
            automatic = label.startswith("Before going back")
            board.points.append(
                Checkpoint(id=sha.strip(), at=at.strip(), label=label, automatic=automatic)
            )
        elif section == "DIRTY" and line.strip():
            board.unsaved += 1

    # The newest point matches the files on disk only when nothing has changed
    # since; otherwise "where you are" is somewhere not yet saved.
    if board.points and board.unsaved == 0:
        board.points[0].current = True
    return board


def board_script(workdir: str) -> str:
    d = shlex.quote(workdir)
    return f"""
cd {d} 2>/dev/null || {{ echo "###ERROR"; echo "That project folder is missing."; exit 0; }}
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {{
  echo "###ERROR"; echo "This project is not set up for save points yet."; exit 0; }}
echo "###LOG"
git log --grep="{TRAILER}" --pretty=format:"{_FORMAT}" -n {MAX_POINTS} 2>/dev/null
echo
echo "###DIRTY"
# -uall lists untracked files rather than collapsing a new directory into one
# line: "4 things changed" when 27 files did is the kind of undercount that
# makes someone skip the save.
git status --porcelain -uall 2>/dev/null
"""


def save_script(workdir: str, label: str) -> str:
    """Commit everything, including files git has never seen.

    `git add -A` rather than only tracked changes: the person clicked save
    because they want *this*, and a new file they cannot see the tracked status
    of is still part of it.
    """
    d = shlex.quote(workdir)
    # The label is data, so it goes in through a variable rather than being
    # spliced into the message argument.
    safe = shlex.quote(label)
    return f"""
cd {d} 2>/dev/null || {{ echo "###ERROR"; echo "That project folder is missing."; exit 0; }}
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {{
  echo "###ERROR"; echo "This project is not set up for save points yet."; exit 0; }}
LABEL={safe}
git add -A 2>/dev/null
if git diff --cached --quiet 2>/dev/null; then
  echo "###ERROR"; echo "Nothing has changed since your last save point."; exit 0
fi
git {_identity()} commit -q -m "$LABEL" -m "{TRAILER} 1" 2>&1 | tail -3
echo "###OK"
git rev-parse HEAD
"""


def restore_script(workdir: str, checkpoint: str, label: str) -> str:
    """Put the files back, having first saved where they are.

    `git clean -fd` without `-x` removes files created since the point but
    leaves anything ignored — which is how `node_modules` and a virtualenv
    survive an undo instead of costing twenty minutes to reinstall.
    """
    d = shlex.quote(workdir)
    sha = shlex.quote(checkpoint)
    safe = shlex.quote(label)
    return f"""
cd {d} 2>/dev/null || {{ echo "###ERROR"; echo "That project folder is missing."; exit 0; }}
git cat-file -e {sha} 2>/dev/null || {{
  echo "###ERROR"; echo "That save point is no longer there."; exit 0; }}

# Going back is itself undoable: whatever is here now becomes a point first.
git add -A 2>/dev/null
if ! git diff --cached --quiet 2>/dev/null; then
  git {_identity()} commit -q -m "Before going back" -m "{TRAILER} 1" 2>/dev/null
fi

git restore --source={sha} --staged --worktree -- . 2>&1 | tail -2
git clean -fdq 2>/dev/null

git add -A 2>/dev/null
if git diff --cached --quiet 2>/dev/null; then
  # Already exactly there. Not an error, just nothing to record.
  echo "###OK"; git rev-parse HEAD; exit 0
fi
git {_identity()} commit -q -m {safe} -m "{TRAILER} 1" 2>&1 | tail -3
echo "###OK"
git rev-parse HEAD
"""


def parse_result(stdout: str) -> tuple[bool, str]:
    """Whether it worked, and what to say if it did not."""
    if "###OK" in stdout:
        return True, ""
    section = ""
    message = ""
    for line in stdout.splitlines():
        if line.startswith("###"):
            section = line[3:].strip()
            continue
        if section == "ERROR" and line.strip() and not message:
            message = line.strip()
    return False, message or "That did not work. Try again in a moment."


def default_label(now: datetime | None = None) -> str:
    """A name for someone who did not want to think of one."""
    at = now or datetime.now(UTC)
    return f"Save point — {at.strftime('%-d %b, %H:%M')}"


async def board(
    conn: asyncssh.SSHClientConnection, container: str, workdir: str
) -> Board:
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", board_script(workdir)], timeout=45
    )
    if not result.ok and not result.stdout:
        return Board(detail="Could not reach this project just now.")
    return parse_board(result.stdout)


async def save(
    conn: asyncssh.SSHClientConnection, container: str, workdir: str, label: str
) -> tuple[bool, str]:
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", save_script(workdir, label)], timeout=90
    )
    return parse_result(result.stdout)


async def restore(
    conn: asyncssh.SSHClientConnection,
    container: str,
    workdir: str,
    checkpoint: str,
    label: str,
) -> tuple[bool, str]:
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", restore_script(workdir, checkpoint, label)], timeout=120
    )
    return parse_result(result.stdout)
