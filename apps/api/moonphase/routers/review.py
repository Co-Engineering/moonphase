"""Answering, reviewing and finding — the three things you do from away.

These share a shape: they are all about a session you are not sitting in front
of. Answering a prompt without opening a terminal is the point of the phone
client. Seeing what changed is the point of leaving an agent alone. Finding
where something happened is the point of keeping transcripts at all.

They also share a cost model. Each one groups its work by container so that a
project with four sessions costs one round trip and not four.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import activity, changes, checkpoints, digest, queries, runtime, search, sessions, ssh
from .. import harness as harness_registry
from ..auth import Principal, current_principal
from ..db import user_session
from ..runtime import CAN_OBSERVE, NotFound
from ..schemas import (
    AttentionOut,
    ChangedFileOut,
    ChangesOut,
    CheckpointOut,
    CheckpointsOut,
    DigestOut,
    PromptOut,
    SaveCheckpointIn,
    SearchHitOut,
    SearchOut,
)
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["review"])

# A search that has to reach four machines should still feel like a search.
SEARCH_TIMEOUT_SECONDS = 25


async def _my_waiting_sessions(principal: Principal) -> list[dict[str, Any]]:
    """Sessions of mine that are blocked on an answer.

    Only mine: someone else's agent waiting on them is not something I could
    answer if I tried, because it runs on their credentials.
    """
    async with user_session(principal.claims) as conn:
        rows = await queries.list_all_sessions(conn)
    return [
        row
        for row in rows
        if row.get("is_mine") and str(row.get("activity")) == "awaiting_input"
    ]


@router.get("/attention", response_model=list[AttentionOut])
async def attention(
    principal: Principal = Depends(current_principal),
) -> list[AttentionOut]:
    """Every question waiting on you, with its options already parsed.

    The home screen could always say that something was waiting; answering it
    meant opening the project, which on a phone is most of the work. The
    question and its buttons come back here so the answer is one tap from the
    screen you are already on.
    """
    waiting = await _my_waiting_sessions(principal)
    if not waiting:
        return []

    # One capture per container, however many sessions are in it.
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in waiting:
        groups.setdefault(str(row["project_id"]), []).append(row)

    async def one(project_id: str, rows: list[dict[str, Any]]) -> list[AttentionOut]:
        try:
            ctx = await runtime.load_project_context(
                principal.claims, UUID(project_id), require=CAN_OBSERVE
            )
            conn_ssh = await ssh.pool.get(ctx.target)
            panes = await sessions.capture_all_panes(conn_ssh, ctx.container)
        except (NotFound, SSHError) as exc:
            log.debug("attention: %s unreachable (%s)", project_id, exc)
            return []

        signals = harness_registry.get(ctx.harness).activity_signals()
        out: list[AttentionOut] = []
        for row in rows:
            name = str(row["tmux_session"])
            pane = panes.get(name)
            if pane is None:
                continue
            parsed = activity.parse_prompt(pane, signals)
            out.append(
                AttentionOut(
                    project_id=row["project_id"],
                    project_name=str(row.get("project_name") or "project"),
                    session=name,
                    activity_at=row.get("activity_at"),
                    question=(
                        parsed.question if parsed else str(row.get("activity_detail") or "")
                    ),
                    prompt=(
                        PromptOut.model_validate(
                            {"question": parsed.question, "options": parsed.options}
                        )
                        if parsed
                        else None
                    ),
                    # The last few lines, because a question without what led to
                    # it is not enough to answer safely from a phone.
                    tail="\n".join(pane.splitlines()[-14:]),
                )
            )
        return out

    results = await asyncio.gather(
        *(one(pid, rows) for pid, rows in groups.items()), return_exceptions=True
    )
    flat: list[AttentionOut] = []
    for result in results:
        if isinstance(result, list):
            flat.extend(result)
    flat.sort(key=lambda item: item.activity_at or "")
    return flat


@router.get("/projects/{project_id}/sessions/{name}/changes", response_model=ChangesOut)
async def session_changes(
    project_id: UUID,
    name: str,
    principal: Principal = Depends(current_principal),
) -> ChangesOut:
    """What this session has done to the code, committed or not."""
    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    session_name = sessions.sanitise_name(name)
    async with user_session(principal.claims) as conn:
        rows = await queries.get_sessions(conn, project_id)
    row = next(
        (r for r in rows if str(r.get("tmux_session")) == session_name), None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such session.")

    space = sessions.space_for(session_name, row.get("workdir"))
    conn_ssh = await ssh.pool.get(ctx.target)
    try:
        found = await changes.read(conn_ssh, ctx.container, space.workdir)
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return ChangesOut(
        branch=found.branch,
        base=found.base,
        added=found.added,
        removed=found.removed,
        truncated=found.truncated,
        detail=found.detail,
        patch=found.patch,
        files=[
            ChangedFileOut(
                path=f.path, added=f.added, removed=f.removed, status=f.status
            )
            for f in found.files
        ],
    )


@router.get("/search", response_model=SearchOut)
async def search_transcripts(
    q: str = Query(min_length=2, max_length=200),
    principal: Principal = Depends(current_principal),
) -> SearchOut:
    """Find a phrase across every session you own.

    Yours only, and enforced by which sessions come back from the database
    rather than by filtering afterwards: a shared project does not make someone
    else's conversation yours to read.
    """
    async with user_session(principal.claims) as conn:
        rows = [r for r in await queries.list_all_sessions(conn) if r.get("is_mine")]

    async def one(row: dict[str, Any]) -> list[SearchHitOut]:
        directory = row.get("transcript_path")
        if not directory:
            return []
        try:
            ctx = await runtime.load_project_context(
                principal.claims, row["project_id"], require=CAN_OBSERVE
            )
            conn_ssh = await ssh.pool.get(ctx.target)
            found = await search.search_session(
                conn_ssh, ctx.container, str(directory), q
            )
        except (NotFound, SSHError) as exc:
            log.debug("search: %s unreachable (%s)", row.get("tmux_session"), exc)
            return []
        return [
            SearchHitOut(
                project_id=row["project_id"],
                project_name=str(row.get("project_name") or "project"),
                session=str(row["tmux_session"]),
                at=hit["at"],
                role=hit["role"],
                text=hit["text"],
            )
            for hit in found
        ]

    try:
        async with asyncio.timeout(SEARCH_TIMEOUT_SECONDS):
            results = await asyncio.gather(
                *(one(row) for row in rows), return_exceptions=True
            )
    except TimeoutError:
        # Partial results beat an error page: one unreachable machine should
        # not hide what the others found.
        return SearchOut(query=q, hits=[], partial=True)

    hits: list[SearchHitOut] = []
    partial = False
    for result in results:
        if isinstance(result, list):
            hits.extend(result)
        else:
            partial = True
    hits.sort(key=lambda hit: hit.at, reverse=True)
    return SearchOut(query=q, hits=hits, partial=partial)


@router.post(
    "/projects/{project_id}/sessions/{name}/answer",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def answer_from_anywhere(
    project_id: UUID,
    name: str,
    payload: dict,
    principal: Principal = Depends(current_principal),
) -> None:
    """Answer a waiting prompt by session name.

    The feed already has an answer route, but it addresses the session
    implicitly. Answering from a list of everything waiting means naming which
    one, so this takes the session in the path.
    """
    keys = str(payload.get("key") or "").strip()
    if not keys:
        raise HTTPException(status_code=422, detail="Nothing to send.")

    session_name = sessions.sanitise_name(name)
    async with user_session(principal.claims) as conn:
        rows = await queries.get_sessions(conn, project_id)
    row = next((r for r in rows if str(r.get("tmux_session")) == session_name), None)
    if row is None or not row.get("is_mine"):
        # Not yours to answer. Typing into someone else's session would run on
        # their credentials and land in their branch.
        raise HTTPException(status_code=403, detail="That is not your session.")

    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
        conn_ssh = await ssh.pool.get(ctx.target)
        await sessions.send_keys(conn_ssh, ctx.container, keys, session=session_name)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _own_session(principal: Principal, project_id: UUID, name: str):
    """The session row, the context and its worktree, or a clear refusal.

    Save points write to a worktree and commit as its owner, so this is a
    stricter check than reading: watching someone else's project does not make
    their files yours to move.
    """
    session_name = sessions.sanitise_name(name)
    async with user_session(principal.claims) as conn:
        rows = await queries.get_sessions(conn, project_id)
    row = next((r for r in rows if str(r.get("tmux_session")) == session_name), None)
    if row is None:
        raise HTTPException(status_code=404, detail="No such session.")
    if not row.get("is_mine"):
        raise HTTPException(status_code=403, detail="That is not your session.")
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    space = sessions.space_for(session_name, row.get("workdir"))
    return ctx, space, session_name


@router.get(
    "/projects/{project_id}/sessions/{name}/checkpoints", response_model=CheckpointsOut
)
async def list_checkpoints(
    project_id: UUID,
    name: str,
    principal: Principal = Depends(current_principal),
) -> CheckpointsOut:
    """Where you have saved, and whether there is unsaved work."""
    ctx, space, _ = await _own_session(principal, project_id, name)
    conn_ssh = await ssh.pool.get(ctx.target)
    try:
        found = await checkpoints.board(conn_ssh, ctx.container, space.workdir)
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return CheckpointsOut(
        unsaved=found.unsaved,
        detail=found.detail,
        points=[
            CheckpointOut(
                id=point.id,
                at=point.at,
                label=point.label,
                current=point.current,
                automatic=point.automatic,
            )
            for point in found.points
        ],
    )


@router.post(
    "/projects/{project_id}/sessions/{name}/checkpoints", response_model=CheckpointsOut
)
async def save_checkpoint(
    project_id: UUID,
    name: str,
    payload: SaveCheckpointIn,
    principal: Principal = Depends(current_principal),
) -> CheckpointsOut:
    """Save everything as it is now, under a name."""
    ctx, space, session_name = await _own_session(principal, project_id, name)
    conn_ssh = await ssh.pool.get(ctx.target)
    label = (payload.label or "").strip() or checkpoints.default_label()
    workspace_profile = await runtime.load_session_profile(
        principal.claims, ctx.project, ctx.harness, session_name
    )
    try:
        ok, detail = await checkpoints.save(
            conn_ssh,
            ctx.container,
            space.workdir,
            label,
            author_name=workspace_profile.git_user_name or (principal.email or "Moonphase"),
            author_email=workspace_profile.git_user_email
            or (principal.email or "moonphase@localhost"),
        )
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=409, detail=detail)
    return await list_checkpoints(project_id, session_name, principal)


@router.post(
    "/projects/{project_id}/sessions/{name}/checkpoints/{checkpoint}/restore",
    response_model=CheckpointsOut,
)
async def restore_checkpoint(
    project_id: UUID,
    name: str,
    checkpoint: str,
    principal: Principal = Depends(current_principal),
) -> CheckpointsOut:
    """Put the files back to a save point.

    Nothing is destroyed: the current state is saved as its own point first, so
    the undo has an undo.
    """
    ctx, space, session_name = await _own_session(principal, project_id, name)
    if not checkpoint.isalnum() or len(checkpoint) < 7:
        raise HTTPException(status_code=422, detail="That is not a save point.")

    conn_ssh = await ssh.pool.get(ctx.target)
    board = await checkpoints.board(conn_ssh, ctx.container, space.workdir)
    target = next((p for p in board.points if p.id == checkpoint), None)
    if target is None:
        raise HTTPException(status_code=404, detail="That save point is no longer there.")

    workspace_profile = await runtime.load_session_profile(
        principal.claims, ctx.project, ctx.harness, session_name
    )
    try:
        ok, detail = await checkpoints.restore(
            conn_ssh,
            ctx.container,
            space.workdir,
            checkpoint,
            f"Went back to: {target.label}",
            author_name=workspace_profile.git_user_name or (principal.email or "Moonphase"),
            author_email=workspace_profile.git_user_email
            or (principal.email or "moonphase@localhost"),
        )
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=409, detail=detail)
    return await list_checkpoints(project_id, session_name, principal)


@router.get("/projects/{project_id}/sessions/{name}/summary", response_model=DigestOut)
async def session_summary(
    project_id: UUID,
    name: str,
    principal: Principal = Depends(current_principal),
) -> DigestOut:
    """What the agent has been doing, counted rather than described."""
    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    session_name = sessions.sanitise_name(name)
    async with user_session(principal.claims) as conn:
        rows = await queries.get_sessions(conn, project_id)
    row = next((r for r in rows if str(r.get("tmux_session")) == session_name), None)
    if row is None or not row.get("transcript_path"):
        return DigestOut()

    space = sessions.space_for(session_name, row.get("workdir"))
    conn_ssh = await ssh.pool.get(ctx.target)
    try:
        found = await digest.read(
            conn_ssh, ctx.container, str(row["transcript_path"]), root=space.workdir
        )
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return DigestOut(
        created=found.created[:40],
        edited=found.edited[:40],
        commands=found.commands,
        installs=found.installs,
        tests=found.tests,
        searches=found.searches,
        last_said=found.last_said,
        detail=found.detail,
    )
