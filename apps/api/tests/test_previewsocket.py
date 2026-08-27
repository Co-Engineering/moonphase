"""Authorization on the preview SOCKS WebSocket.

This socket hands the caller a live, unauthenticated network path *as the
container* — reachable services, no credential of their own required. A
read-only ("observe") share must not get one; only CAN_CONTROL should.
"""

from __future__ import annotations

import uuid

from moonphase import runtime
from moonphase.auth import Principal
from moonphase.routers import previewsocket
from moonphase.runtime import Forbidden


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int | None = None) -> None:
        self.close_code = code


PROJECT_ID = uuid.uuid4()


async def test_the_socks_socket_requires_control_not_just_observe(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_load_project_context(claims, project_id, *, require):
        captured["require"] = require
        raise Forbidden("read-only access")

    monkeypatch.setattr(runtime, "load_project_context", fake_load_project_context)

    websocket = _FakeWebSocket()
    principal = Principal(user_id="viewer", email=None, claims={"sub": "viewer"})

    await previewsocket.preview_socks(websocket, PROJECT_ID, principal=principal)

    assert captured["require"] is runtime.CAN_CONTROL
    assert websocket.close_code == 1008
