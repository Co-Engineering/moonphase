"""A transcript line too large to read in one go must not stop the feed.

Only complete lines move the cursor, so a line longer than one read window was
re-read forever: every poll fetched the same bytes, found no newline, reported
nothing, and left the cursor where it was. The feed stopped there permanently.

Nothing produced such a line until an agent could take a screenshot. One
arrives as a single base64 record of a few hundred kilobytes, so the first
screenshot of any real page froze that project's feed for good.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from moonphase import transcript
from moonphase.harness.claude_code import ClaudeCode


def _screenshot_record(payload_bytes: int) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "A" * payload_bytes,
                                },
                            }
                        ],
                    }
                ]
            },
        }
    ) + "\n"


def _plain_record(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": text, "is_error": False}
                ]
            },
        }
    ) + "\n"


def _reader(blob: bytes):
    """Stands in for the container, answering the three shell commands used."""

    class Result:
        ok = True

        def __init__(self, out: str) -> None:
            self.stdout = out

    def run(_conn: Any, _container: str, command: list[str], **_: Any) -> Result:
        joined = " ".join(command)
        if "stat -c %s" in joined:
            return Result(str(len(blob)))
        if "head -n 1 | wc -c" in joined:
            start = int(joined.split("tail -c +")[1].split()[0]) - 1
            end = blob.find(b"\n", start)
            return Result("0" if end == -1 else str(end - start + 1))
        if "tail -c +" in joined:
            start = int(joined.split("tail -c +")[1].split()[0]) - 1
            window = blob[start : start + transcript.MAX_BYTES_PER_READ]
            return Result(window.decode("utf-8", "ignore"))
        return Result("")

    return run


async def _poll(blob: bytes, times: int) -> list[tuple[int, int]]:
    """(cursor offset, event count) for each poll."""
    seen: list[tuple[int, int]] = []
    cursor = transcript.Cursor(filename="session.jsonl", offset=0).encode()
    with (
        patch.object(
            transcript, "_newest_file", AsyncMock(return_value="/t/session.jsonl")
        ),
        patch.object(
            transcript.docker_remote,
            "exec_capture",
            AsyncMock(side_effect=_reader(blob)),
        ),
    ):
        for _ in range(times):
            page = await transcript.read(None, "container", ClaudeCode(), cursor=cursor)
            cursor = page.cursor
            seen.append(
                (transcript.Cursor.decode(cursor).offset, len(page.events))
            )
    return seen


@pytest.mark.asyncio
async def test_a_line_larger_than_one_read_does_not_stop_the_feed() -> None:
    """The failure this exists for: the cursor stayed at 0 and no event ever
    arrived again, on every poll, forever."""
    oversized = _screenshot_record(transcript.MAX_BYTES_PER_READ + 50_000)
    blob = (oversized + _plain_record("this comes after")).encode()

    polls = await _poll(blob, 3)

    offsets = [offset for offset, _ in polls]
    assert offsets[0] > 0, "the cursor must move past a line it cannot read"
    # And what followed the unreadable line is still delivered.
    assert sum(count for _, count in polls) >= 1
    assert offsets[-1] == len(blob)


@pytest.mark.asyncio
async def test_an_ordinary_screenshot_is_delivered_rather_than_skipped() -> None:
    """Skipping must be the last resort, not the usual path — a screenshot is
    the reason any of this is here."""
    blob = _screenshot_record(300_000).encode()

    polls = await _poll(blob, 2)

    assert polls[0][1] == 1, "the screenshot should arrive whole, on the first poll"
    assert polls[0][0] == len(blob)


@pytest.mark.asyncio
async def test_a_half_written_line_is_waited_for_rather_than_skipped() -> None:
    """A short read with no complete line is the harness mid-write. Skipping
    there would throw away a record that was about to be finished."""
    blob = b'{"type": "user", "message": {"content": [{"type": "tool_res'

    polls = await _poll(blob, 2)

    assert [offset for offset, _ in polls] == [0, 0]
    assert sum(count for _, count in polls) == 0
