"""Connecting an OAuth-only MCP server, relayed through a running session.

Mirrors routers/profile.py's harness-login endpoints — start, poll, submit —
but scoped to one MCP server inside one project's session rather than the
harness's own account inside a throwaway container. See mcp_login.py for why
the mechanics differ and why they still work with no browser reachable from
the container.
"""

from __future__ import annotations

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


def _out(session: mcp_login.McpLoginSession) -> McpOAuthOut:
    # The pane is only useful once something is worth showing — the same
    # judgement call routers/profile.py makes for the account flow.
    show_pane = session.state in {"verifying", "error"}
    return McpOAuthOut(
        session_id=session.id,
        state=session.state,
        url=session.url,
        detail=session.detail,
        pane=session.pane if show_pane else None,
    )


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
            "MCP server in it.",
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
            server_name=payload.server_name,
            space=space,
            container=ctx.container,
            user_id=str(principal.user_id),
        )
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _out(mcp_session)


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
    mcp_session = mcp_login.get(login_session_id)
    if mcp_session is None:
        raise HTTPException(status_code=404, detail="No such connection attempt.")

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
    mcp_session = mcp_login.get(login_session_id)
    if mcp_session is None:
        raise HTTPException(status_code=404, detail="No such connection attempt.")

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
