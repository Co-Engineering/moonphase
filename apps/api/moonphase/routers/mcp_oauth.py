"""Connecting an OAuth-only MCP server, relayed through a running session.

Mirrors routers/profile.py's harness-login endpoints — start, poll, submit —
but scoped to one MCP server inside one project's session rather than the
harness's own account inside a throwaway container. See mcp_login.py for why
the mechanics differ and why they still work with no browser reachable from
the container.

The resulting credential is org-wide regardless of which session the relay
actually ran through (see mcp_oauth_credentials), so "Connect" offered from a
project's or the org's own Configure screen — where there is no one specific
session in hand — auto-picks any one of the caller's own running sessions to
carry it, the same way signing in to Claude auto-picks any online server.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from .. import mcp_login, queries, runtime, sessions, ssh
from ..auth import Principal, current_principal
from ..db import service_session, user_session
from ..runtime import CAN_CONTROL, NotFound
from ..schemas import (
    McpOAuthConnectionOut,
    McpOAuthOut,
    McpOAuthPasteIn,
    McpOAuthStartIn,
)
from ..ssh import SSHError

router = APIRouter(prefix="/api/projects/{project_id}", tags=["mcp-oauth"])


def _get_mcp_session(
    login_session_id: str, project_id: UUID, principal: Principal
) -> mcp_login.McpLoginSession:
    """`mcp_login.get`, refused for anyone but the account that started it,
    on the same project it was started under.

    The session id is a random token, but it is also the only thing gating a
    flow that ends in a credential stored under `session.org_id` and
    `created_by=session.user_id` — a mismatch here must read as "no such
    session", not as a way to tell a wrong id apart from someone else's.

    The project check matters just as much as the user check: callers
    authorize against the URL's `project_id`, but `session.container` and
    `session.tmux_session` are fixed to whatever project the flow actually
    started under. Without this, someone who started a flow on a project
    they've since lost access to could keep driving it by naming a different
    project they still control in the URL — the access check would pass
    while the commands still ran against the *original* project's session.
    """
    session = mcp_login.get(login_session_id)
    if (
        session is None
        or session.user_id != str(principal.user_id)
        or session.project_id != str(project_id)
    ):
        raise HTTPException(status_code=404, detail="No such connection attempt.")
    return session


def _out(session: mcp_login.McpLoginSession) -> McpOAuthOut:
    # The pane is only useful once something is worth showing — the same
    # judgement call routers/profile.py makes for the account flow.
    show_pane = session.state in {"verifying", "error"}
    return McpOAuthOut(
        session_id=session.id,
        project_id=session.project_id,
        state=session.state,
        url=session.url,
        detail=session.detail,
        pane=session.pane if show_pane else None,
    )


def _own_running_session(
    rows: list[dict[str, Any]], *, where: str
) -> dict[str, Any]:
    """The caller's own running session among candidate rows, or a clear 409.

    Any one will do — the credential that comes out the other end is org-wide
    regardless of which session carried the relay — so the first is as good
    as any.
    """
    mine = [
        r for r in rows if r.get("is_mine") and str(r.get("state")) == "running"
    ]
    if not mine:
        raise HTTPException(
            status_code=409,
            detail=(
                "Connecting a server relays OAuth through one of your own "
                f"running sessions{where}, so it needs one. Start a session "
                "first, or connect from that session's own Configure dialog."
            ),
        )
    return mine[0]


async def _start(
    project_id: UUID,
    session_name: str,
    server_name: str,
    principal: Principal,
) -> McpOAuthOut:
    """Everything from "which session" settled to a relay under way."""
    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_CONTROL
        )
        space, row = await runtime.load_session_space(
            principal.claims, project_id, session_name
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not row.get("is_mine"):
        raise HTTPException(
            status_code=403,
            detail="This is someone else's session; only they can connect an "
            "MCP server through it.",
        )

    profile = await runtime.load_session_profile(
        principal.claims, ctx.project, ctx.harness, session_name
    )
    if not profile.has_harness_auth:
        raise HTTPException(
            status_code=409,
            detail="Connect Claude Code to your account first — this needs a "
            "running harness to relay through.",
        )

    async with user_session(principal.claims) as db:
        org_id = await queries.personal_org_id(db)
    if org_id is None:
        raise HTTPException(status_code=404, detail="You have no personal organization.")

    try:
        conn_ssh = await ssh.pool.get(ctx.target)
        # Idempotent — guarantees the server this is about to authenticate is
        # actually present in this session's ~/.claude.json before asking
        # `claude mcp login` to find it.
        await sessions.ensure_session(
            conn_ssh,
            ctx.container,
            harness_kind=ctx.harness,
            workspace_profile=profile,
            session=session_name,
            space=space,
        )
        mcp_session = await mcp_login.start(
            conn_ssh,
            org_id=str(org_id),
            project_id=str(project_id),
            session_name=session_name,
            server_name=server_name,
            space=space,
            container=ctx.container,
            user_id=str(principal.user_id),
        )
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _out(mcp_session)


@router.post(
    "/sessions/{session_name}/mcp-oauth/start", response_model=McpOAuthOut
)
async def start_mcp_oauth(
    project_id: UUID,
    session_name: str,
    payload: McpOAuthStartIn,
    principal: Principal = Depends(current_principal),
) -> McpOAuthOut:
    """Begin relaying OAuth for one MCP server, inside this session's container.

    The server has to already be configured — in the org, project or session
    Claude config — and materialised into this session, which is why this
    starts the session first: an MCP server added moments ago and never
    picked up by a restart would otherwise fail with a confusing "unknown
    server" from `claude mcp login` instead of a clear one from here.
    """
    return await _start(project_id, session_name, payload.server_name, principal)


@router.post("/mcp-oauth/start", response_model=McpOAuthOut)
async def start_mcp_oauth_for_project(
    project_id: UUID,
    payload: McpOAuthStartIn,
    principal: Principal = Depends(current_principal),
) -> McpOAuthOut:
    """Same as above, for Connect offered from the project's own Configure
    dialog — where a server is normally defined, but no one session is in
    hand. Relays through any one of the caller's own running sessions in
    this project."""
    async with user_session(principal.claims) as db:
        rows = await queries.get_sessions(db, project_id)
    row = _own_running_session(rows, where=" in this project")
    return await _start(
        project_id, str(row["tmux_session"]), payload.server_name, principal
    )


@router.get("/mcp-oauth/{login_session_id}", response_model=McpOAuthOut)
async def poll_mcp_oauth(
    project_id: UUID,
    login_session_id: str,
    principal: Principal = Depends(current_principal),
) -> McpOAuthOut:
    """Advance a relay by one step and report where it got to.

    Persists the captured credential the moment it appears — an upsert, so a
    client that keeps polling after completion does not cause a second row.
    """
    mcp_session = _get_mcp_session(login_session_id, project_id, principal)

    if mcp_session.state == "verifying":
        try:
            ctx = await runtime.load_project_context(
                principal.claims, project_id, require=CAN_CONTROL
            )
            conn_ssh = await ssh.pool.get(ctx.target)
            mcp_session = await mcp_login.advance(conn_ssh, mcp_session)
        except (SSHError, NotFound) as exc:
            mcp_session.state = "error"
            mcp_session.detail = str(exc)

        if mcp_session.state == "complete" and mcp_session.credential_entry is not None:
            async with service_session() as db:
                await queries.upsert_mcp_oauth_credential_privileged(
                    db,
                    org_id=UUID(mcp_session.org_id),
                    server_name=mcp_session.server_name,
                    credential_json=mcp_session.credential_entry,
                    created_by=mcp_session.user_id,
                )

    return _out(mcp_session)


@router.post("/mcp-oauth/{login_session_id}/paste", response_model=McpOAuthOut)
async def paste_mcp_oauth(
    project_id: UUID,
    login_session_id: str,
    payload: McpOAuthPasteIn,
    principal: Principal = Depends(current_principal),
) -> McpOAuthOut:
    """Hand the pasted redirect URL to the waiting flow.

    Types it and returns at once; the client polls for the result the same
    way it already does for the harness's own sign-in.
    """
    mcp_session = _get_mcp_session(login_session_id, project_id, principal)

    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_CONTROL
        )
        conn_ssh = await ssh.pool.get(ctx.target)
        mcp_session = await mcp_login.submit_paste(
            conn_ssh, mcp_session, payload.redirect_url
        )
    except (SSHError, NotFound) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _out(mcp_session)


# Not project-scoped — connected servers live at the org, same as the
# credential they hold.
profile_router = APIRouter(prefix="/api/profile", tags=["mcp-oauth"])


@profile_router.post("/mcp-oauth/start", response_model=McpOAuthOut)
async def start_mcp_oauth_for_org(
    payload: McpOAuthStartIn,
    principal: Principal = Depends(current_principal),
) -> McpOAuthOut:
    """Connect offered from Settings — no project in hand at all, so this
    relays through any one of the caller's own running sessions anywhere."""
    async with user_session(principal.claims) as db:
        rows = await queries.list_all_sessions(db)
    row = _own_running_session(rows, where="")
    return await _start(
        UUID(str(row["project_id"])), str(row["tmux_session"]), payload.server_name,
        principal,
    )


@profile_router.get("/mcp-oauth", response_model=list[McpOAuthConnectionOut])
async def list_mcp_oauth(
    principal: Principal = Depends(current_principal),
) -> list[McpOAuthConnectionOut]:
    """Every MCP server this org has connected via OAuth, for a settings list."""
    async with user_session(principal.claims) as db:
        org_id = await queries.personal_org_id(db)
    if org_id is None:
        return []
    async with service_session() as db:
        rows = await queries.list_mcp_oauth_credentials_privileged(db, org_id)
    return [McpOAuthConnectionOut.model_validate(row) for row in rows]


@profile_router.delete("/mcp-oauth/{server_name}", status_code=204)
async def disconnect_mcp_oauth(
    server_name: str,
    principal: Principal = Depends(current_principal),
) -> None:
    async with user_session(principal.claims) as db:
        org_id = await queries.personal_org_id(db)
    if org_id is None:
        raise HTTPException(status_code=404, detail="You have no personal organization.")
    async with service_session() as db:
        await queries.delete_mcp_oauth_credential_privileged(db, org_id, server_name)


__all__ = ["router", "profile_router"]
