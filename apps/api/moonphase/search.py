"""Finding the moment something happened.

After a week of sessions the thing you remember is not which project it was in
— it is a phrase. "Where did I tell it about the rate limiter." Scrolling four
transcripts to find that is the work the transcript was supposed to save.

Searched where the text already lives rather than by copying every message into
Postgres: the transcripts are the record, they are already on the machine, and
mirroring them would double the storage to answer a query that runs a few times
a day. Two execs per container: one to find which lines matched, one to fetch
those lines.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass

import asyncssh

from . import docker_remote

log = logging.getLogger(__name__)

# Per container. Enough to find what you meant without turning a search into a
# transfer of the whole history.
MAX_HITS = 30
SNIPPET = 240


@dataclass
class Hit:
    session: str
    project_id: str
    project_name: str
    at: str = ""
    role: str = "assistant"
    text: str = ""


def _plain(content: object) -> str:
    """Flatten a message body to something you can read in a list.

    Content is a string on a simple turn and a list of typed blocks otherwise;
    tool calls and their results are the bulk of an agent transcript and are
    noise in a search result, so only the text blocks survive.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return " ".join(parts)
    return ""


def parse_locations(stdout: str) -> list[tuple[str, int]]:
    """`path:line` pairs from grep, ignoring anything that is not one."""
    found: list[tuple[str, int]] = []
    for raw in stdout.splitlines():
        path, sep, number = raw.rpartition(":")
        if not sep or not number.strip().isdigit():
            continue
        found.append((path, int(number)))
    return found


def fetch_script(locations: list[tuple[str, int]]) -> str:
    """One `sed` per matched line, which is cheaper than reading whole files."""
    parts = []
    for path, line in locations:
        parts.append(f"sed -n {line}p {shlex.quote(path)}")
    return "; ".join(parts)


def records_from(stdout: str, query: str) -> list[dict[str, str]]:
    """Turn matched transcript lines into something worth showing.

    A line that does not parse is skipped rather than shown raw: a JSON blob in
    a search result is not an answer, and half a blob is worse.
    """
    out: list[dict[str, str]] = []
    needle = query.lower()
    for line in stdout.splitlines():
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
        text = " ".join(_plain(message.get("content")).split())
        if not text or needle not in text.lower():
            # The match was in a tool call or some other field the reader will
            # not see, so showing this row would look like a false positive.
            continue
        out.append(
            {
                "at": str(record.get("timestamp") or ""),
                "role": str(message.get("role") or record.get("type") or ""),
                "text": _around(text, needle),
            }
        )
    return out


def _around(text: str, needle: str) -> str:
    """A window centred on the match, so the hit is visible without opening it."""
    index = text.lower().find(needle)
    if index < 0 or len(text) <= SNIPPET:
        return text[:SNIPPET]
    start = max(0, index - SNIPPET // 3)
    end = min(len(text), start + SNIPPET)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


async def search_session(
    conn: asyncssh.SSHClientConnection,
    container: str,
    transcript_dir: str,
    query: str,
) -> list[dict[str, str]]:
    """Which lines matched, then what those lines say."""
    directory = shlex.quote(transcript_dir)
    # -F: the query is text a person typed, not a regular expression they wrote.
    # -H: grep omits the filename when it is given exactly one file, and a
    # session with one transcript is the common case — without this the output
    # is `line:content` and every location parses to nothing.
    # Cutting to the path and line number keeps a matched megabyte-long record
    # from coming back twice.
    locate = (
        f"grep -iFnH -m {MAX_HITS} -- {shlex.quote(query)} {directory}/*.jsonl "
        "2>/dev/null | cut -d: -f1,2"
    )
    found = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", locate], timeout=45
    )
    locations = parse_locations(found.stdout)[:MAX_HITS]
    if not locations:
        return []

    body = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", fetch_script(locations)], timeout=45
    )
    return records_from(body.stdout, query)
