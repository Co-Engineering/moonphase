"""Reading a harness's own session transcript.

The phone client renders this rather than a terminal. A 80-column TUI on a
390-pixel screen is technically possible and practically useless, and a second
PTY client would also drag the desktop's window size around — tmux sizes a
window to its most recent client. Reading the transcript sidesteps both: the
phone observes without attaching, and writes back through `tmux send-keys`, so
the one session stays the single source of truth.

Tailing is by byte offset into the newest transcript file. Offsets rather than
line counts because the file is appended to continuously, and a cursor that
survives is what makes polling cheap: each request ships only what is new.
"""

from __future__ import annotations

import difflib
import json
import logging
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

import asyncssh

from . import docker_remote
from .harness import Harness, SessionSpace

log = logging.getLogger(__name__)

# One poll should never drag megabytes over SSH.
MAX_BYTES_PER_READ = 256 * 1024
# On a cold open, enough history to see what happened without loading a
# whole day's session.
INITIAL_LINES = 300


# A diff has to fit on a phone and travel over a slow connection. Past a
# couple of screens nobody reads it anyway; the counts still tell the story.
MAX_DIFF_LINES = 120
MAX_DIFF_LINE_CHARS = 200


@dataclass
class DiffLine:
    # " " context, "+" added, "-" removed.
    sign: str
    text: str


@dataclass
class TranscriptEvent:
    """One thing that happened, normalised across harnesses."""

    id: str
    # user | assistant | thinking | tool | result | system
    kind: str
    text: str = ""
    at: str | None = None
    tool: str | None = None
    # Result events only: whether the tool succeeded.
    ok: bool | None = None
    # True for a subagent's traffic, which the UI dims rather than hides.
    sidechain: bool = False
    # Edits and writes carry their change, so a phone can approve one on its
    # merits rather than on a file name.
    diff: list[DiffLine] | None = None
    added: int = 0
    removed: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_diff(before: str, after: str) -> tuple[list[DiffLine], int, int, bool]:
    """A unified diff reduced to what a small screen can show.

    Returns the lines, the added and removed counts, and whether it was cut
    short. Counts come from the full diff even when the lines are truncated —
    "+240 −13, showing the first 120" is honest; a silently short diff is not.
    """
    old_lines = before.splitlines()
    new_lines = after.splitlines()

    lines: list[DiffLine] = []
    added = removed = 0
    truncated = False

    for raw in difflib.unified_diff(old_lines, new_lines, n=2, lineterm=""):
        # The ---/+++ headers name files we do not have; @@ hunk markers are
        # useful, everything else is content.
        if raw.startswith(("---", "+++")):
            continue
        sign, text = (raw[0], raw[1:]) if raw and raw[0] in "+- @" else (" ", raw)
        if raw.startswith("@@"):
            sign, text = "@", raw
        elif sign == "+":
            added += 1
        elif sign == "-":
            removed += 1

        if len(lines) < MAX_DIFF_LINES:
            lines.append(DiffLine(sign=sign, text=text[:MAX_DIFF_LINE_CHARS]))
        else:
            truncated = True

    return lines, added, removed, truncated


@dataclass
class Cursor:
    """Where we last read to. Serialised as `<filename>:<byte offset>`."""

    filename: str = ""
    offset: int = 0

    def encode(self) -> str:
        return f"{self.filename}:{self.offset}" if self.filename else ""

    @classmethod
    def decode(cls, raw: str | None) -> Cursor:
        if not raw or ":" not in raw:
            return cls()
        name, _, offset = raw.rpartition(":")
        try:
            return cls(filename=name, offset=max(0, int(offset)))
        except ValueError:
            return cls()


@dataclass
class TranscriptPage:
    events: list[TranscriptEvent] = field(default_factory=list)
    cursor: str = ""
    # False when the harness has not written a transcript yet.
    available: bool = True


async def _newest_file(
    conn: asyncssh.SSHClientConnection, container: str, directory: str
) -> str | None:
    """The transcript for the harness's current run.

    A harness starts a new file per session, so the newest is the live one;
    older files are previous conversations in the same workspace.
    """
    result = await docker_remote.exec_capture(
        conn,
        container,
        ["sh", "-c", f"ls -1t {shlex.quote(directory)}/*.jsonl 2>/dev/null | head -1"],
        timeout=30,
    )
    path = result.stdout.strip()
    return path or None


async def read(
    conn: asyncssh.SSHClientConnection,
    container: str,
    harness: Harness,
    *,
    cursor: str | None = None,
    space: SessionSpace | None = None,
) -> TranscriptPage:
    """Everything written since `cursor`, plus a cursor for next time.

    The space matters: each session writes its transcript under its own HOME,
    so reading the wrong one shows you somebody else's conversation.
    """
    directory = harness.transcript_dir(space or SessionSpace())
    path = await _newest_file(conn, container, directory)
    if path is None:
        return TranscriptPage(available=False)

    filename = path.rsplit("/", 1)[-1]
    position = Cursor.decode(cursor)

    size_result = await docker_remote.exec_capture(
        conn, container,
        ["sh", "-c", f"stat -c %s {shlex.quote(path)} 2>/dev/null || echo 0"],
        timeout=30,
    )
    try:
        size = int(size_result.stdout.strip() or 0)
    except ValueError:
        size = 0

    fresh = position.filename != filename
    # A file that shrank was rotated or replaced; re-reading from where we were
    # would decode the middle of a line.
    if position.offset > size:
        fresh = True

    if fresh:
        # Cold open: take the tail, and bound the cursor to the size we
        # measured so nothing written since is skipped.
        command = (
            f"head -c {size} {shlex.quote(path)} 2>/dev/null | tail -n {INITIAL_LINES}"
        )
        new_offset = size
    else:
        if position.offset >= size:
            return TranscriptPage(events=[], cursor=position.encode())
        command = (
            f"tail -c +{position.offset + 1} {shlex.quote(path)} 2>/dev/null "
            f"| head -c {MAX_BYTES_PER_READ}"
        )
        new_offset = position.offset

    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", command], timeout=60
    )
    raw = result.stdout if result.ok else ""

    events: list[TranscriptEvent] = []
    consumed = 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith("\n"):
            # A partial final line: the harness is mid-write. Leave it for the
            # next poll rather than dropping or mis-parsing it.
            break
        consumed += len(line.encode())
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        events.extend(harness.parse_transcript_record(record))

    if not fresh:
        new_offset = position.offset + consumed

    return TranscriptPage(
        events=events, cursor=Cursor(filename=filename, offset=new_offset).encode()
    )
