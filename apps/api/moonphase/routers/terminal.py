"""WebSocket ↔ PTY bridge.

A client attaching here gets a real pseudo-terminal on a real `tmux attach`
inside the project container. Nothing about the session belongs to this
connection: dropping it detaches, and the harness carries on. That asymmetry is
the entire product, so the code below is careful to never kill the remote
process on disconnect — it only closes its own channel.

Wire protocol
    client → server   binary frame : raw stdin bytes
                      text frame   : JSON control, {"type": "resize", ...}
    server → client   binary frame : raw stdout bytes
                      text frame   : JSON status, {"type": "exit"|"error", ...}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from uuid import UUID

import asyncssh
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from .. import docker_remote, queries, runtime, sessions, ssh
from ..auth import Principal, websocket_principal
from ..db import user_session
from ..runtime import NotFound
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

READ_CHUNK = 65536


async def _pump_output(process: asyncssh.SSHClientProcess, websocket: WebSocket) -> None:
    """Remote stdout → client."""
    while True:
        data = await process.stdout.read(READ_CHUNK)
        if not data:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        await websocket.send_bytes(data)


async def _pump_input(process: asyncssh.SSHClientProcess, websocket: WebSocket) -> None:
    """Client → remote stdin, plus out-of-band control messages."""
    while True:
        message = await websocket.receive()

        if message["type"] == "websocket.disconnect":
            return

        data = message.get("bytes")
        if data is not None:
            process.stdin.write(data)
            continue

        raw = message.get("text")
        if not raw:
            continue
        try:
            control = json.loads(raw)
        except json.JSONDecodeError:
            # Not control traffic — treat it as typed input so a naive client
            # sending text frames still works.
            process.stdin.write(raw.encode())
            continue

        kind = control.get("type")
        if kind == "resize":
            cols = int(control.get("cols", 80))
            rows = int(control.get("rows", 24))
            # Clamp: a bogus geometry from a client can wedge the remote TUI.
            cols = max(20, min(cols, 500))
            rows = max(5, min(rows, 200))
            with contextlib.suppress(OSError, asyncssh.Error):
                process.change_terminal_size(cols, rows)
        elif kind == "input":
            process.stdin.write(str(control.get("data", "")).encode())
        elif kind == "ping":
            await websocket.send_text(json.dumps({"type": "pong"}))


@router.websocket("/ws/projects/{project_id}/terminal")
async def project_terminal(
    websocket: WebSocket,
    project_id: UUID,
    cols: int = Query(default=120, ge=20, le=500),
    rows: int = Query(default=32, ge=5, le=200),
    principal: Principal = Depends(websocket_principal),
) -> None:
    await websocket.accept()

    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close(code=4404)
        return

    try:
        conn_ssh = await ssh.pool.get(ctx.target)
    except SSHError as exc:
        await ssh.pool.drop(ctx.target.server_id)
        await websocket.send_text(
            json.dumps({"type": "error", "message": f"Could not reach the server: {exc}"})
        )
        await websocket.close(code=4503)
        return

    # Make sure there is something to attach to. `ensure_session` is a no-op
    # when the session already exists, which is the normal case.
    try:
        container = await docker_remote.inspect(conn_ssh, ctx.container)
        if container is None:
            raise SSHError("The project container no longer exists on this server.")
        if container.state != "running":
            await docker_remote.start(conn_ssh, ctx.container)

        workspace_profile = await runtime.load_profile(
            principal.claims, ctx.project["org_id"], project_id, ctx.harness
        )
        await sessions.ensure_session(
            conn_ssh,
            ctx.container,
            harness_kind=ctx.harness,
            workspace_profile=workspace_profile,
        )
    except SSHError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close(code=4500)
        return

    attach = sessions.attach_command(ctx.container)

    try:
        process = await conn_ssh.create_process(
            attach,
            term_type="xterm-256color",
            term_size=(cols, rows),
            encoding=None,  # binary in both directions; the TUI is not text
        )
    except asyncssh.Error as exc:
        await websocket.send_text(
            json.dumps({"type": "error", "message": f"Could not attach: {exc}"})
        )
        await websocket.close(code=4500)
        return

    async with user_session(principal.claims) as conn:
        await queries.touch_attached(conn, project_id, sessions.DEFAULT_SESSION)

    await websocket.send_text(
        json.dumps(
            {
                "type": "attached",
                "project_id": str(project_id),
                "container": ctx.container,
                "session": sessions.DEFAULT_SESSION,
            }
        )
    )

    output_task = asyncio.create_task(_pump_output(process, websocket))
    input_task = asyncio.create_task(_pump_input(process, websocket))

    try:
        done, pending = await asyncio.wait(
            {output_task, input_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                log.warning("terminal pump for %s ended: %s", project_id, exc)
    except WebSocketDisconnect:
        pass
    finally:
        # Detach, do not terminate. Closing the channel leaves `tmux attach`'s
        # parent gone but the tmux *server* — and therefore the harness —
        # running inside the container, which is exactly what we want.
        with contextlib.suppress(Exception):
            process.close()
        for task in (output_task, input_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await websocket.close()
        log.info("terminal detached from project %s", project_id)
