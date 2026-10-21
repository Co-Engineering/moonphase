"""Reading a harness's own session transcript.

The phone client renders this rather than a terminal. A 80-column TUI on a
390-pixel screen is technically possible and practically useless, and a second
PTY client would also drag the desktop's window size around — tmux sizes a
window to its most recent client. Reading the transcript sidesteps both: the
phone observes without attaching, and writes back through `tmux send-keys`, so
the one session stays the single source of truth.

Tailing is by byte offset into the live transcript file — the one the harness
says it is writing now (`Harness.live_transcript_script`). Offsets rather than
line counts because the file is appended to continuously, and a cursor that
survives is what makes polling cheap: each request ships only what is new.
"""

from __future__ import annotations

import difflib
import json
import logging
import posixpath
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

import asyncssh

from . import docker_remote
from .harness import Harness, SessionSpace

log = logging.getLogger(__name__)

# One poll should never drag megabytes over SSH.
# One poll's worth. Large enough that a browser screenshot — which arrives as
# one base64 line and is routinely a few hundred kilobytes — fits in a single
# read, and bounded so a poll cannot become unbounded work.
MAX_BYTES_PER_READ = 1024 * 1024
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
class TodoItem:
    content: str
    # pending | in_progress | completed
    status: str


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
    # A tool result carrying an image — a screenshot from a browser MCP
    # server, most often — as base64, ready for an <img data:> src.
    image_media_type: str | None = None
    image_data: str | None = None
    # A TodoWrite call's own checklist, so the feed can pin the current plan
    # instead of showing this as a blank tool call.
    todos: list[TodoItem] | None = None

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
    """Where we last read to. Serialised as `<file>:<byte offset>`.

    `filename` is the file's identity (`file_identity`): its path relative to
    the harness's transcript directory. For a harness that writes one flat
    `<id>.jsonl` per conversation that is exactly the file's name, which is
    what this field held before nested transcripts existed — so a cursor a
    client is holding across a deploy still continues where it was rather
    than replaying the tail. It is a path for one that nests them, and may
    contain `/` (and even `:`; decoding splits on the last one).
    """

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


def file_identity(directory: str, path: str) -> str:
    """Which transcript `path` is, as the cursor and the parser know it.

    The path relative to the harness's transcript directory. A basename alone
    used to be enough, because Claude Code names each conversation's file by
    its own uuid; Antigravity names every one of them
    `transcript_full.jsonl` and tells them apart by the directory above. By
    name alone a new conversation would look like the old one carrying on:
    the REST read would apply the old file's offset to the new one — starting
    mid-line and dropping everything before it — and the socket would go on
    tailing the old file for as long as it existed, since `tail -f` on a file
    that is still there never exits. The relative path is still exactly the
    basename for a flat directory, so nothing changes for Claude Code.

    A path outside the directory is not expected — every harness's live
    transcript lives under its own `transcript_dir` — but if one turns up it
    is identified by its whole path rather than by a name it may share.
    """
    root = posixpath.normpath(directory)
    full = posixpath.normpath(path)
    prefix = root.rstrip("/") + "/"
    if full.startswith(prefix) and len(full) > len(prefix):
        return full[len(prefix):]
    return full


async def live_file(
    conn: asyncssh.SSHClientConnection,
    container: str,
    harness: Harness,
    space: SessionSpace,
) -> str | None:
    """The path of the transcript the harness is writing now, or None.

    Asked of the harness rather than assumed. For a harness writing a flat
    file per conversation it is the newest one (the base class's default);
    Antigravity nests one directory per conversation and records which is
    current itself, and a "newest file" there could be a sub-agent's or
    another worktree's conversation. Shared by the REST read and the feed
    socket, so the two can never disagree about which file is live.
    """
    result = await docker_remote.exec_capture(
        conn,
        container,
        ["sh", "-c", harness.live_transcript_script(space)],
        timeout=30,
    )
    # One path. Anything after the first line is not part of it.
    path = next(iter(result.stdout.strip().splitlines()), "").strip()
    return path or None


async def _skip_one_line(
    conn: asyncssh.SSHClientConnection,
    container: str,
    path: str,
    offset: int,
) -> int:
    """The offset just past the line starting at `offset`.

    Asked of the file rather than guessed, because the point is to get past a
    line too long to have been read. `head -n 1` reads one line however long
    it is; only its length comes back.
    """
    command = (
        f"tail -c +{offset + 1} {shlex.quote(path)} 2>/dev/null | head -n 1 | wc -c"
    )
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", command], timeout=60
    )
    try:
        length = int(result.stdout.strip() or 0)
    except ValueError:
        length = 0
    if length <= 0:
        # Nothing readable there. Leave the cursor alone rather than skipping
        # blind, and let the next poll try again.
        return offset
    log.info("skipping a transcript line of %d bytes: too large to read", length)
    return offset + length


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
    space = space or SessionSpace()
    path = await live_file(conn, container, harness, space)
    if path is None:
        return TranscriptPage(available=False)

    identity = file_identity(harness.transcript_dir(space), path)
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

    # A different file is a different conversation, even when it has the same
    # name as the last one (see `file_identity`).
    fresh = position.filename != identity
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
    filled = len(raw.encode()) >= MAX_BYTES_PER_READ

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
        events.extend(harness.parse_transcript_record(record, source=identity))

    if not fresh:
        new_offset = position.offset + consumed
        if consumed == 0 and filled:
            # One line longer than a whole read. Only complete lines advance
            # the cursor, so this would be re-read forever: every poll fetches
            # the same bytes, finds no newline, and reports nothing. The feed
            # stops there permanently — which is what a screenshot bigger than
            # the window did to it.
            #
            # A short read with no complete line is different and must still
            # wait: that is the harness mid-write, and the rest is coming.
            new_offset = await _skip_one_line(conn, container, path, position.offset)

    return TranscriptPage(
        events=events, cursor=Cursor(filename=identity, offset=new_offset).encode()
    )
