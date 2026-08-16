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
from ..runtime import CAN_CONTROL, CAN_OBSERVE, Forbidden, NotFound
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

READ_CHUNK = 65536

# The attach wrapper prints its tty before tmux takes over. Give it a moment,
# but never let a missing marker stall the terminal.
MARKER_TIMEOUT_SECONDS = 5.0


async def _consume_tty_marker(
    process: asyncssh.SSHClientProcess,
) -> tuple[str | None, bytes]:
    """Read the wrapper's tty announcement without showing it to the user.

    Returns the tty and whatever bytes were read past it, which belong to tmux
    and must still reach the client. A missing marker is not an error: it just
    means this attach cannot detach itself precisely later.
    """
    buffer = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MARKER_TIMEOUT_SECONDS

    while b"\n" not in buffer and len(buffer) < 512:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(process.stdout.read(512), timeout=remaining)
        except TimeoutError:
            break
        if not chunk:
            break
        buffer += chunk if isinstance(chunk, bytes) else chunk.encode()

    marker = sessions.TTY_MARKER.encode()
    index = buffer.find(marker)
    if index == -1:
        return None, buffer

    line_end = buffer.find(b"\n", index)
    if line_end == -1:
        return None, buffer

    tty = buffer[index + len(marker) : line_end].decode(errors="replace").strip()
    # Keep anything before the marker (there should be nothing) and after it.
    leftover = buffer[:index] + buffer[line_end + 1 :]
    return tty or None, leftover


async def _pump_output(process: asyncssh.SSHClientProcess, websocket: WebSocket) -> None:
    """Remote stdout → client."""
    while True:
        data = await process.stdout.read(READ_CHUNK)
        if not data:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        await websocket.send_bytes(data)


async def _pump_input(
    process: asyncssh.SSHClientProcess,
    websocket: WebSocket,
    *,
    writable: bool = True,
) -> None:
    """Client → remote stdin, plus out-of-band control messages.

    A viewer's socket is still read: it carries pings and the disconnect that
    ends the session. Only the keystrokes are dropped. tmux is attached
    read-only as well, but the guarantee that matters is this one, because it
    does not depend on a tmux version behaving as documented.
    """
    while True:
        message = await websocket.receive()

        if message["type"] == "websocket.disconnect":
            return

        data = message.get("bytes")
        if data is not None:
            if writable:
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
            if writable:
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
            if writable:
                process.stdin.write(str(control.get("data", "")).encode())
        elif kind == "ping":
            await websocket.send_text(json.dumps({"type": "pong"}))


@router.websocket("/ws/projects/{project_id}/terminal")
async def project_terminal(
    websocket: WebSocket,
    project_id: UUID,
    cols: int = Query(default=120, ge=20, le=500),
    rows: int = Query(default=32, ge=5, le=200),
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
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close(code=4403)
        return
    except NotFound as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close(code=4404)
        return

    # A viewer gets the same live picture and none of the keyboard.
    writable = ctx.access in CAN_CONTROL

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
    #
    # Skipped entirely for a viewer: starting a container and launching a
    # harness is exactly the kind of thing view-only access exists to prevent,
    # and it would be doing it with someone else's credentials.
    try:
        container = await docker_remote.inspect(conn_ssh, ctx.container)
        if container is None:
            raise SSHError("The project container no longer exists on this server.")
        if not writable:
            if container.state != "running":
                raise SSHError(
                    "This project is not running, and view-only access cannot "
                    "start it."
                )
            if session_name not in await sessions.client_counts(
                conn_ssh, ctx.container
            ):
                raise SSHError(
                    f"There is no session called {session_name!r} running yet."
                )
        else:
            if container.state != "running":
                await docker_remote.start(conn_ssh, ctx.container)

            workspace_profile = await runtime.load_profile(
                ctx.project["org_id"], project_id, ctx.harness
            )
            await sessions.ensure_session(
                conn_ssh,
                ctx.container,
                harness_kind=ctx.harness,
                workspace_profile=workspace_profile,
                session=session_name,
            )
    except SSHError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close(code=4500)
        return

    attach = sessions.attach_command(
        ctx.container, session_name, read_only=not writable
    )

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

    client_tty, leftover = await _consume_tty_marker(process)
    if leftover:
        await websocket.send_bytes(leftover)

    if writable:
        # A viewer arriving must not look like the session being driven.
        async with user_session(principal.claims) as conn:
            await queries.touch_attached(conn, project_id, session_name)

    await websocket.send_text(
        json.dumps(
            {
                "type": "attached",
                "project_id": str(project_id),
                "container": ctx.container,
                "session": session_name,
                "writable": writable,
            }
        )
    )

    output_task = asyncio.create_task(_pump_output(process, websocket))
    input_task = asyncio.create_task(
        _pump_input(process, websocket, writable=writable)
    )

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
        # Detach explicitly, then close. Closing alone is not enough: `docker
        # exec` leaves the process it started running inside the container, so
        # the tmux client would linger forever and keep constraining the
        # window size for everyone else.
        if client_tty:
            with contextlib.suppress(Exception):
                await sessions.detach_client(conn_ssh, ctx.container, client_tty)
        with contextlib.suppress(Exception):
            process.close()
        for task in (output_task, input_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await websocket.close()
        log.info(
            "terminal detached from project %s session %s", project_id, session_name
        )
