"""The phone client's view of a session.

A readable feed instead of a terminal, and taps instead of keystrokes. This is
not a second protocol onto the agent: the feed is the harness's own transcript,
and every answer goes back through `tmux send-keys` into the same session the
desktop is attached to. Both surfaces therefore agree by construction, and
nothing here can drift from what the terminal shows.

It also avoids a concrete problem with using a real terminal on a phone: tmux
sizes a window to its most recent client, so a phone attaching would squeeze
the desktop down to phone width. A reader that never attaches cannot.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from .. import activity, queries, runtime, sessions, ssh
from .. import harness as harness_registry
from .. import transcript as transcript_reader
from ..auth import Principal, current_principal
from ..db import user_session
from ..runtime import CAN_OBSERVE, NotFound
from ..schemas import AnswerIn, FeedOut, PromptOut, TranscriptEventOut
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["feed"])


@router.get("/{project_id}/feed", response_model=FeedOut)
async def get_feed(
    project_id: UUID,
    session: str | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(current_principal),
) -> FeedOut:
    """New transcript events since `cursor`, plus any question being asked.

    Everything the phone needs in one request: polling twice for "what
    happened" and "is it waiting" would double the SSH round trips for a client
    that is often on a slow connection.
    """
    session_name = sessions.sanitise_name(session or sessions.DEFAULT_SESSION)

    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if ctx.project["status"] != "running":
        return FeedOut(available=False, activity="stopped", cursor=cursor or "")

    harness = harness_registry.get(ctx.harness)
    try:
        space, _row = await runtime.load_session_space(
            principal.claims, project_id, session_name
        )
    except NotFound:
        return FeedOut(available=False, activity="unknown", cursor=cursor or "")

    try:
        conn_ssh = await ssh.pool.get(ctx.target)
        page = await transcript_reader.read(
            conn_ssh, ctx.container, harness, cursor=cursor, space=space
        )
        pane = await sessions.capture_pane(
            conn_ssh, ctx.container, session=session_name, lines=80
        )
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    parsed = activity.parse_prompt(pane, harness.activity_signals())

    async with user_session(principal.claims) as conn:
        rows = await queries.get_sessions(conn, project_id)
    state = next(
        (str(r["activity"]) for r in rows if r["tmux_session"] == session_name),
        "unknown",
    )

    return FeedOut(
        events=[TranscriptEventOut.model_validate(e.to_dict()) for e in page.events],
        cursor=page.cursor,
        available=page.available,
        activity=state,
        prompt=PromptOut.model_validate(
            {"question": parsed.question, "options": parsed.options}
        )
        if parsed
        else None,
    )


@router.post("/{project_id}/feed/answer", status_code=status.HTTP_204_NO_CONTENT)
async def answer(
    project_id: UUID,
    payload: AnswerIn,
    session: str | None = None,
    principal: Principal = Depends(current_principal),
) -> None:
    """Answer a waiting prompt by tapping an option.

    Deliberately the same `send-keys` path a typed message uses: an answer is
    just a keystroke arriving in the session, so the desktop terminal shows it
    happening exactly as if the user had typed it there.
    """
    session_name = sessions.sanitise_name(session or sessions.DEFAULT_SESSION)

    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn_ssh = await ssh.pool.get(ctx.target)
    try:
        await sessions.send_keys(
            conn_ssh, ctx.container, payload.key, session=session_name, enter=True
        )
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
