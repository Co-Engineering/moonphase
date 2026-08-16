"""Streaming feed.

Polling every three seconds made the phone feel a step behind the terminal:
worst case you watched a spinner for three seconds after tapping an answer that
had already been accepted. This pushes instead.

Two different things are streamed, because they have genuinely different
shapes:

* **Transcript events** come from `tail -f` inside the container. The harness
  appends as it goes, so there is a real stream to follow and no reason to ask
  repeatedly whether anything happened.
* **The prompt** lives in the terminal, which has no append-only log — the pane
  is a screen that gets rewritten. That still has to be sampled, but sampling it
  server-side and pushing only on change means the client sees a new question
  within a second without asking for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shlex
from dataclasses import asdict
from uuid import UUID

import asyncssh
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from .. import activity, docker_remote, runtime, sessions, ssh
from .. import harness as harness_registry
from .. import transcript as transcript_reader
from ..auth import Principal, websocket_principal
from ..runtime import CAN_OBSERVE, Forbidden, NotFound
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(tags=["feed"])

# How often to re-read the pane. Fast enough that a question appears while the
# user is still looking at the screen, slow enough to stay one cheap exec.
PANE_INTERVAL_SECONDS = 1.5
# How often to check whether the harness started a new transcript file. Rare,
# so this only needs to be faster than a human notices.
ROTATION_INTERVAL_SECONDS = 5.0


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def _send(websocket: WebSocket, payload: dict) -> None:
    await websocket.send_text(json.dumps(payload))


async def _stream_transcript(
    websocket: WebSocket,
    conn: asyncssh.SSHClientConnection,
    container: str,
    harness,
    start_cursor: str,
    space,
) -> None:
    """Follow the newest transcript file and push events as they are written.

    Resumes from exactly where the initial page stopped. Following from the
    top of the file instead would replay everything the client just received,
    and following from the end would drop anything written in between.
    """
    directory = harness.transcript_dir(space)
    position = transcript_reader.Cursor.decode(start_cursor)
    current: str | None = position.filename or None
    process: asyncssh.SSHClientProcess | None = None
    buffer = ""

    async def newest() -> str | None:
        result = await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", f"ls -1t {shlex.quote(directory)}/*.jsonl 2>/dev/null | head -1"],
            timeout=30,
        )
        return result.stdout.strip() or None

    async def start_tail(path: str, byte_offset: int) -> asyncssh.SSHClientProcess:
        command = (
            f"docker exec -i {shlex.quote(container)} "
            f"tail -c +{byte_offset + 1} -f {shlex.quote(path)}"
        )
        return await conn.create_process(command, encoding=None)

    try:
        while True:
            path = await newest()
            if path is None:
                await asyncio.sleep(ROTATION_INTERVAL_SECONDS)
                continue

            filename = path.rsplit("/", 1)[-1]
            if process is None or filename != current:
                if process is not None:
                    with contextlib.suppress(Exception):
                        process.close()
                # Resume where the page left off for the file it read; a
                # rotation is a new run, so follow that one from its start.
                offset = position.offset if filename == position.filename else 0
                process = await start_tail(path, offset)
                current = filename
                buffer = ""
            try:
                chunk = await asyncio.wait_for(
                    process.stdout.read(65536), timeout=ROTATION_INTERVAL_SECONDS
                )
            except TimeoutError:
                # No output for a while: a good moment to notice a rotation.
                continue

            if not chunk:
                # tail exited — the file was replaced under it.
                current = None
                continue

            buffer += chunk.decode("utf-8", errors="replace")
            *complete, buffer = buffer.split("\n")

            events = []
            for line in complete:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                events.extend(harness.parse_transcript_record(record))

            if events:
                await _send(
                    websocket,
                    {"type": "events", "events": [asdict(e) for e in events]},
                )
    finally:
        if process is not None:
            with contextlib.suppress(Exception):
                process.close()


async def _stream_prompt(
    websocket: WebSocket,
    conn: asyncssh.SSHClientConnection,
    container: str,
    harness,
    session_name: str,
) -> None:
    """Sample the pane and push only when it changes."""
    signals = harness.activity_signals()
    last = ""

    while True:
        pane = await sessions.capture_pane(
            conn, container, session=session_name, lines=80
        )
        digest = _digest(pane)
        if digest != last:
            last = digest
            parsed = activity.parse_prompt(pane, signals)
            await _send(
                websocket,
                {
                    "type": "prompt",
                    "prompt": (
                        {"question": parsed.question, "options": parsed.options}
                        if parsed
                        else None
                    ),
                    # Derived here rather than read from the database so it
                    # tracks the pane the user is looking at, not the monitor's
                    # last sweep.
                    "activity": "awaiting_input" if parsed else "working",
                },
            )
        await asyncio.sleep(PANE_INTERVAL_SECONDS)


@router.websocket("/ws/projects/{project_id}/feed")
async def project_feed(
    websocket: WebSocket,
    project_id: UUID,
    session: str = Query(default=sessions.DEFAULT_SESSION, max_length=64),
    principal: Principal = Depends(websocket_principal),
) -> None:
    await websocket.accept()
    session_name = sessions.sanitise_name(session)

    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except Forbidden as exc:
        await _send(websocket, {"type": "error", "message": str(exc)})
        await websocket.close(code=4403)
        return
    except NotFound as exc:
        await _send(websocket, {"type": "error", "message": str(exc)})
        await websocket.close(code=4404)
        return

    if ctx.project["status"] != "running":
        await _send(websocket, {"type": "page", "events": [], "available": False})
        await websocket.close(code=4409)
        return

    harness = harness_registry.get(ctx.harness)

    try:
        space, _row = await runtime.load_session_space(
            principal.claims, project_id, session_name
        )
    except NotFound as exc:
        await _send(websocket, {"type": "error", "message": str(exc)})
        await websocket.close(code=4404)
        return

    try:
        conn_ssh = await ssh.pool.get(ctx.target)
        # Send history first, so the client has something to render before the
        # stream produces anything.
        page = await transcript_reader.read(
            conn_ssh, ctx.container, harness, space=space
        )
    except SSHError as exc:
        await _send(websocket, {"type": "error", "message": str(exc)})
        await websocket.close(code=4503)
        return

    await _send(
        websocket,
        {
            "type": "page",
            "events": [e.to_dict() for e in page.events],
            "available": page.available,
            "cursor": page.cursor,
        },
    )

    tasks = [
        asyncio.create_task(
            _stream_transcript(
                websocket, conn_ssh, ctx.container, harness, page.cursor, space
            )
        ),
        asyncio.create_task(
            _stream_prompt(websocket, conn_ssh, ctx.container, harness, session_name)
        ),
        # Reading is what notices the client going away; without it a closed
        # phone would leave both streams running against the server.
        asyncio.create_task(websocket.receive_text()),
    ]

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                log.warning("feed stream for %s ended: %s", project_id, exc)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await websocket.close()
        log.info("feed closed for project %s", project_id)
