"""What happened, in words a person who does not code can read.

The feed is faithful and, for a lot of people, unreadable: forty tool calls,
each with a path and a diff. Someone who asked for a todo app and came back an
hour later wants one sentence — "it made twelve files and installed three
packages, and now it is asking you something" — and the option to look closer.

Counted from the transcript rather than described by a model. A summary that is
generated could be wrong in ways nobody can check, and this one has to be
trustworthy precisely because its reader cannot verify it against the diff.
Counting is boring and it is always right.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field

import asyncssh

from . import docker_remote

log = logging.getLogger(__name__)

# The tail of the transcript is where recent work is. Bounded so a session that
# has run all week does not turn a summary into a download.
TAIL_BYTES = 400_000


@dataclass
class Digest:
    created: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    commands: int = 0
    installs: int = 0
    tests: int = 0
    searches: int = 0
    # The last thing the agent said in prose, which is usually the best
    # one-line account of where it got to.
    last_said: str = ""
    detail: str | None = None

    @property
    def empty(self) -> bool:
        return not (
            self.created or self.edited or self.commands or self.tests or self.last_said
        )


# Recognising what a shell command was for. Deliberately shallow: the point is
# to say "installed something" rather than to understand the command.
_INSTALL = re.compile(
    r"\b(npm|pnpm|yarn|bun)\s+(i|add|install)\b|\bpip3?\s+install\b|\buv\s+(pip\s+)?add\b"
    r"|\bapt(-get)?\s+install\b|\bpoetry\s+add\b|\bcargo\s+add\b",
    re.IGNORECASE,
)
_TEST = re.compile(
    r"\bpytest\b|\bjest\b|\bvitest\b|\bgo\s+test\b|\bcargo\s+test\b"
    r"|\b(npm|pnpm|yarn|bun)\s+(run\s+)?test\b|\brspec\b|\bphpunit\b",
    re.IGNORECASE,
)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return " ".join(parts)
    return ""


def _blocks(content: object) -> list[dict]:
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def relative(path: str, root: str) -> str:
    """A path as the person thinks of it.

    The transcript records absolute container paths, so an unedited list reads
    `/home/dev/sessions/oliver-test/work/README.md` — four directories of
    Moonphase's own plumbing in front of the one word that means anything.
    """
    if not root:
        return path
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def summarise(text: str, root: str = "") -> Digest:
    """Fold transcript lines into counts and a closing sentence."""
    digest = Digest()
    created: list[str] = []
    edited: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue

        said = " ".join(_text_of(message.get("content")).split())
        if said and message.get("role") == "assistant":
            digest.last_said = said

        for block in _blocks(message.get("content")):
            if block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            data = block.get("input")
            data = data if isinstance(data, dict) else {}
            path = str(data.get("file_path") or data.get("path") or "").strip()

            if name == "Write" and path:
                created.append(relative(path, root))
            elif name in {"Edit", "NotebookEdit", "MultiEdit"} and path:
                edited.append(relative(path, root))
            elif name == "Bash":
                command = str(data.get("command") or "")
                digest.commands += 1
                if _INSTALL.search(command):
                    digest.installs += 1
                if _TEST.search(command):
                    digest.tests += 1
            elif name in {"Grep", "Glob", "WebSearch", "WebFetch"}:
                digest.searches += 1

    # A file written twice is one file. Order preserved so the list reads as
    # the order things happened.
    digest.created = list(dict.fromkeys(created))
    # A file that was created here is not also "edited" — it is new.
    made = set(digest.created)
    digest.edited = [path for path in dict.fromkeys(edited) if path not in made]
    digest.last_said = digest.last_said[:400]
    return digest


async def read(
    conn: asyncssh.SSHClientConnection,
    container: str,
    transcript_dir: str,
    *,
    root: str = "",
    tail: int = TAIL_BYTES,
) -> Digest:
    """Summarise the newest transcript's tail.

    The newest file only: a digest is about what just happened, and an earlier
    conversation is a different subject rather than more of this one.
    """
    directory = shlex.quote(transcript_dir)
    script = (
        f"f=$(ls -1t {directory}/*.jsonl 2>/dev/null | head -1); "
        '[ -n "$f" ] || exit 0; '
        f'tail -c {tail} "$f"'
    )
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", script], timeout=45
    )
    if not result.ok:
        return Digest(detail="Could not read this session just now.")
    if not result.stdout.strip():
        return Digest()
    # The first line of a tail is usually half a record; parsing is tolerant,
    # so it simply does not count.
    return summarise(result.stdout, root)
