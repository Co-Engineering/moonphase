"""The preview proxy, carried over an authenticated WebSocket.

A preview window is a browser pointed at a SOCKS proxy whose every connection
lands inside the project's container, so `localhost:8000` means what the code
running in there means by it.

That proxy listens on the loopback interface of whichever machine runs the API,
and deliberately nowhere else: it is an unauthenticated network path *as the
container*, and publishing it would hand anyone who could reach the port a way
in. While the desktop app only existed as a development build beside the API,
loopback was also where the browser was, and that was the end of it.

An installed app talking to a server across the internet breaks that
coincidence. Its `127.0.0.1` is its own, the proxy is on the server's, and the
preview window fails to connect to a proxy that is running perfectly well
somewhere else.

So the stream comes to the client instead. The desktop app listens on its own
loopback, and pipes each connection here over a WebSocket that carries the
caller's token and is checked against the same project access as everything
else. The proxy stays unpublished, the browser gets a proxy it can reach, and
the SOCKS conversation itself is the one in `socks.py` — this only changes what
the bytes travel on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from .. import runtime, socks
from ..auth import Principal, websocket_principal
from ..runtime import CAN_OBSERVE, Forbidden, NotFound

log = logging.getLogger(__name__)

router = APIRouter(tags=["preview"])


class _WebSocketReader:
    """Present a WebSocket's incoming messages as a byte stream.

    SOCKS is a stream protocol and WebSocket is a message protocol, so message
    boundaries have to be dissolved: a client that sends a nine-byte request in
    two frames must still be able to have four bytes read from it. Hence the
    buffer.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._buffer = bytearray()
        self._eof = False

    async def _fill(self) -> None:
        if self._eof:
            return
        try:
            self._buffer += await self._websocket.receive_bytes()
        except (WebSocketDisconnect, RuntimeError, KeyError):
            # KeyError: a text frame or a close arrives without the "bytes" key.
            self._eof = True

    async def readexactly(self, n: int) -> bytes:
        while len(self._buffer) < n:
            before = len(self._buffer)
            await self._fill()
            if self._eof and len(self._buffer) == before:
                raise asyncio.IncompleteReadError(bytes(self._buffer), n)
        chunk = bytes(self._buffer[:n])
        del self._buffer[:n]
        return chunk

    async def read(self, n: int) -> bytes:
        if not self._buffer:
            await self._fill()
        if not self._buffer:
            return b""
        chunk = bytes(self._buffer[:n])
        del self._buffer[:n]
        return chunk


class _WebSocketWriter:
    """The other direction, with the sends serialised.

    Buffered until `drain`, because the protocol writes a header and its
    payload as separate calls and one frame per fragment would be wasteful.
    A lock, because `_pump` writes from one task while a reply may still be
    finishing in another, and two concurrent sends on one WebSocket corrupt it.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._pending = bytearray()
        self._lock = asyncio.Lock()
        self._closed = False

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._pending += data

    async def drain(self) -> None:
        if self._closed or not self._pending:
            return
        payload = bytes(self._pending)
        self._pending.clear()
        async with self._lock:
            try:
                await self._websocket.send_bytes(payload)
            except (WebSocketDisconnect, RuntimeError):
                self._closed = True

    def close(self) -> None:
        self._closed = True


@router.websocket("/ws/projects/{project_id}/preview/socks")
async def preview_socks(
    websocket: WebSocket,
    project_id: UUID,
    principal: Principal = Depends(websocket_principal),
) -> None:
    """One SOCKS5 conversation, for one connection the desktop app accepted.

    One per connection rather than multiplexed: the desktop app already has to
    accept a socket per request the preview makes, WebSockets are cheap next to
    the SSH channel each one opens anyway, and a multiplexer would be a second
    protocol to get wrong for no benefit anybody could see.
    """
    await websocket.accept()

    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except (NotFound, Forbidden):
        # 1008 is "policy violation", which is the closest thing WebSocket has
        # to 404 or 403. Which of those it was is deliberately not said here.
        await websocket.close(code=1008)
        return

    proxy = socks.ProjectProxy(str(project_id), ctx.container, ctx.target)
    reader = _WebSocketReader(websocket)
    writer = _WebSocketWriter(websocket)

    try:
        await proxy.handle_stream(reader, writer)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — one preview request is not fatal
        log.warning("preview socks stream failed: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
