"""Claude Code config scoped to one project, or one person's session in it.

The workspace profile (see `profile.py`) is org-wide. This is the layer
beneath it: a project might need an MCP server the rest of the org doesn't,
and a session might want its own permission rules or a skill only its owner
needs — see `moonphase.harness.claude_code.ClaudeCode.compose_project_layers`
for how the three scopes combine when a session actually starts.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from .. import queries, runtime
from ..auth import Principal, current_principal
from ..db import user_session
from ..profile import parse_json_object
from ..runtime import CAN_ADMINISTER, CAN_CONTROL, CAN_OBSERVE, NotFound
from ..schemas import ClaudeConfigIn, ClaudeConfigOut

router = APIRouter(prefix="/api/projects", tags=["claude-config"])


def _out(row: dict | None) -> ClaudeConfigOut:
    row = row or {}
    return ClaudeConfigOut(
        claude_settings_json=row.get("claude_settings_json"),
        claude_md=row.get("claude_md"),
        mcp_json=row.get("mcp_json"),
        skills={
            str(k): str(v)
            for k, v in parse_json_object(row.get("skills_json")).items()
        },
        env_vars={
            str(k): str(v) for k, v in parse_json_object(row.get("env_vars")).items()
        },
    )


@router.get("/{project_id}/config", response_model=ClaudeConfigOut)
async def get_project_config(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> ClaudeConfigOut:
    """Applies to every session in this project, for everyone who can drive one.

    CAN_CONTROL, not CAN_OBSERVE: a plain viewer share is watch-only — the
    feed and the terminal, nothing about configuration — and this can carry
    real secrets (env_vars exists specifically to hold things like a
    project-only database URL, unencrypted, in the same row).
    """
    try:
        await runtime.load_project_context(
            principal.claims, project_id, require=CAN_CONTROL
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except runtime.Forbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        row = await queries.get_project_config(conn, project_id)
    return _out(row)


@router.put("/{project_id}/config", response_model=ClaudeConfigOut)
async def update_project_config(
    project_id: UUID,
    payload: ClaudeConfigIn,
    principal: Principal = Depends(current_principal),
) -> ClaudeConfigOut:
    """Takes effect for everyone in this project on their next harness restart,
    including a project admin's own session — so setting it is an admin-only
    act, not something a plain write-collaborator can do to everyone else
    unreviewed (mcp_json in particular can name an MCP server command that
    runs on the next session start)."""
    try:
        await runtime.load_project_context(
            principal.claims, project_id, require=CAN_ADMINISTER
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except runtime.Forbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        try:
            row = await queries.update_project_config(
                conn,
                project_id,
                claude_settings_json=payload.claude_settings_json,
                claude_md=payload.claude_md,
                mcp_json=payload.mcp_json,
                skills=payload.skills,
                env_vars=payload.env_vars,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return _out(row)


@router.get("/{project_id}/sessions/{session}/config", response_model=ClaudeConfigOut)
async def get_session_config(
    project_id: UUID, session: str, principal: Principal = Depends(current_principal)
) -> ClaudeConfigOut:
    """A session's own config is between its owner and a project admin.

    It is not secret the way a credential is, but it is personal in the same
    way the session itself is — someone else driving their own session in
    this project has no reason to see it.
    """
    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except runtime.Forbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        session_row = await queries.get_session(conn, project_id, session)
        if session_row is None:
            raise HTTPException(status_code=404, detail=f"No session called {session!r}.")
        if not session_row.get("is_mine") and ctx.access != "admin":
            raise HTTPException(
                status_code=403,
                detail="This is someone else's session; only they or a project "
                "admin can see its configuration.",
            )
        row = await queries.get_session_config(conn, project_id, session)
    return _out(row)


@router.put("/{project_id}/sessions/{session}/config", response_model=ClaudeConfigOut)
async def update_session_config(
    project_id: UUID,
    session: str,
    payload: ClaudeConfigIn,
    principal: Principal = Depends(current_principal),
) -> ClaudeConfigOut:
    """Only the session's own owner may set it — same rule as driving it at all."""
    try:
        await runtime.load_project_context(
            principal.claims, project_id, require=CAN_CONTROL
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except runtime.Forbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        try:
            row = await queries.update_session_config(
                conn,
                project_id,
                session,
                claude_settings_json=payload.claude_settings_json,
                claude_md=payload.claude_md,
                mcp_json=payload.mcp_json,
                skills=payload.skills,
                env_vars=payload.env_vars,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return _out(row)


__all__ = ["router"]
