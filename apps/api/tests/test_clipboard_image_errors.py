"""A pasted image that never made it onto the harness's filesystem used to
say nothing at all — too large, or the SSH write itself failed, both looked
identical to the paste never having been noticed. `_pump_input` now reports
either back to the client as a `clipboard-image-error` frame instead of
swallowing it.
"""

from __future__ import annotations

import json

from moonphase import sessions
from moonphase.routers import terminal
from moonphase.ssh import SSHError


class _FakeWebSocket:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)
        self.sent_text: list[str] = []

    async def receive(self) -> dict:
        if self._messages:
            return self._messages.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()


def _clipboard_message(data: str) -> dict:
    return {
        "type": "websocket.receive",
        "text": json.dumps({"type": "clipboard-image", "data": data}),
    }


async def test_an_oversized_image_is_rejected_with_an_error_instead_of_silence(
    monkeypatch,
) -> None:
    staged = False

    async def fake_stage(*args, **kwargs):
        nonlocal staged
        staged = True

    monkeypatch.setattr(terminal.sessions, "stage_clipboard_image", fake_stage)

    huge = "a" * (terminal.MAX_CLIPBOARD_IMAGE_BASE64 + 1)
    ws = _FakeWebSocket([_clipboard_message(huge)])

    await terminal._pump_input(
        _FakeProcess(),
        ws,
        writable=True,
        conn_ssh=object(),
        container="c1",
        space=sessions.SessionSpace(),
    )

    assert staged is False, "an oversized image must never reach the SSH write"
    assert len(ws.sent_text) == 1
    message = json.loads(ws.sent_text[0])
    assert message["type"] == "clipboard-image-error"
    assert "large" in message["message"].lower()


async def test_a_failed_stage_reports_the_error_instead_of_going_silent(monkeypatch) -> None:
    async def failing_stage(*args, **kwargs):
        raise SSHError("no space left on device")

    monkeypatch.setattr(terminal.sessions, "stage_clipboard_image", failing_stage)

    ws = _FakeWebSocket([_clipboard_message("aGVsbG8=")])

    await terminal._pump_input(
        _FakeProcess(),
        ws,
        writable=True,
        conn_ssh=object(),
        container="c1",
        space=sessions.SessionSpace(),
    )

    assert len(ws.sent_text) == 1
    message = json.loads(ws.sent_text[0])
    assert message["type"] == "clipboard-image-error"
    assert "no space left on device" in message["message"]


async def test_a_successful_stage_reports_nothing(monkeypatch) -> None:
    async def fake_stage(*args, **kwargs):
        return None

    monkeypatch.setattr(terminal.sessions, "stage_clipboard_image", fake_stage)

    ws = _FakeWebSocket([_clipboard_message("aGVsbG8=")])

    await terminal._pump_input(
        _FakeProcess(),
        ws,
        writable=True,
        conn_ssh=object(),
        container="c1",
        space=sessions.SessionSpace(),
    )

    assert ws.sent_text == []


async def test_a_viewer_cannot_stage_an_image_or_learn_anything_by_trying(monkeypatch) -> None:
    staged = False

    async def fake_stage(*args, **kwargs):
        nonlocal staged
        staged = True

    monkeypatch.setattr(terminal.sessions, "stage_clipboard_image", fake_stage)

    ws = _FakeWebSocket([_clipboard_message("aGVsbG8=")])

    await terminal._pump_input(
        _FakeProcess(),
        ws,
        writable=False,
        conn_ssh=object(),
        container="c1",
        space=sessions.SessionSpace(),
    )

    assert staged is False
    assert ws.sent_text == []
