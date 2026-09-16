"""TranscriptEventOut must keep every field TranscriptEvent carries.

The REST polling fallback (routers/feed.py) validates through this model —
the live WebSocket path sends the dataclass straight through and never hits
it — so a field declared on TranscriptEvent but missing here is silently
dropped for anyone who ever falls back to polling, with no error to notice
it by.
"""

from __future__ import annotations

from moonphase.schemas import TranscriptEventOut
from moonphase.transcript import DiffLine, TodoItem, TranscriptEvent


def test_transcript_event_out_keeps_every_field() -> None:
    event = TranscriptEvent(
        id="e1",
        kind="tool",
        text="3/5 done",
        at="2026-08-17T10:00:00Z",
        tool="TodoWrite",
        ok=True,
        sidechain=False,
        diff=[DiffLine(sign="+", text="new line")],
        added=1,
        removed=0,
        truncated=False,
        image_media_type="image/png",
        image_data="Zm9v",
        todos=[TodoItem(content="Fix the bug", status="in_progress")],
    )

    out = TranscriptEventOut.model_validate(event.to_dict())

    assert out.diff is not None and out.diff[0].sign == "+" and out.diff[0].text == "new line"
    assert out.added == 1
    assert out.removed == 0
    assert out.truncated is False
    assert out.image_media_type == "image/png"
    assert out.image_data == "Zm9v"
    assert out.todos is not None and out.todos[0].content == "Fix the bug"
    assert out.todos[0].status == "in_progress"
