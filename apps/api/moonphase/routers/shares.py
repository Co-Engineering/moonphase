"""Sharing a server or a project with one person.

Organizations cover "my team can use everything we own". This covers the other
case, which turns out to be the common one: lending a colleague a machine, or
pulling someone into a single running session to look at what the agent did.

Both resources behave identically, so the two route groups are generated from
one implementation. The only asymmetry worth knowing is what the grant means:

    server   viewer        see that the machine exists and how it is doing
             collaborator  also create your own projects on it
    project  viewer        watch the feed and the terminal, read-only
             collaborator  also type into it, answer prompts, start and stop

Authorization is not decided here. `public.server_access()` and
`public.project_access()` in the database are the definition, the RLS policies
enforce them, and these routes read the same answer so the error message can be
specific instead of an empty result set.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from .. import queries
from ..auth import Principal, current_principal
from ..db import service_session, user_session
from ..schemas import ShareIn, ShareOut, ShareRoleIn

router = APIRouter(tags=["sharing"])


def _to_out(row: dict[str, Any], *, viewer_id: str) -> ShareOut:
    return ShareOut.model_validate(
        {
            **row,
            # A grant made to an address with no account yet is real but
            # dormant, and the distinction is the thing the sharer most wants
            # to see.
            "accepted": row.get("user_id") is not None,
            "is_you": str(row.get("user_id") or "") == viewer_id,
        }
    )


async def _require_admin(claims: dict[str, Any], kind: str, resource_id: UUID) -> None:
    async with user_session(claims) as conn:
        access = await queries.access_level(conn, kind, resource_id)
    if access is None:
        raise HTTPException(status_code=404, detail=f"{kind.capitalize()} not found.")
    if access != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"Only an owner of this {kind} can manage who it is shared with.",
        )


async def _list(kind: str, resource_id: UUID, principal: Principal) -> list[ShareOut]:
    async with user_session(principal.claims) as conn:
        access = await queries.access_level(conn, kind, resource_id)
        if access is None:
            raise HTTPException(
                status_code=404, detail=f"{kind.capitalize()} not found."
            )
        # Not an admin check: the policy already narrows a non-admin to their
        # own row, which is what lets a recipient see and give up their access.
        rows = await queries.list_shares(conn, kind, resource_id)
    return [_to_out(r, viewer_id=principal.user_id) for r in rows]


async def _create(
    kind: str, resource_id: UUID, payload: ShareIn, principal: Principal
) -> ShareOut:
    await _require_admin(principal.claims, kind, resource_id)

    if principal.email and payload.email == principal.email.lower():
        raise HTTPException(
            status_code=422, detail="You already have access to this."
        )

    # auth.users is not readable by the caller's role, so resolving an address
    # to an account is the one privileged step in this file.
    async with service_session() as conn:
        user = await queries.find_user_by_email_privileged(conn, payload.email)

    async with user_session(principal.claims) as conn:
        if user is not None:
            org_id = await _owning_org(conn, kind, resource_id)
            if org_id is not None and await queries.is_org_member_by_user(
                conn, org_id, str(user["id"])
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{payload.email} is already in the organization that owns "
                        f"this {kind}, so they can already use it."
                    ),
                )

        try:
            row = await queries.upsert_share(
                conn,
                kind,
                resource_id,
                email=payload.email,
                user_id=str(user["id"]) if user else None,
                role=payload.role,
                created_by=principal.user_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    return _to_out(row, viewer_id=principal.user_id)


async def _owning_org(conn: Any, kind: str, resource_id: UUID) -> UUID | None:
    if kind == "server":
        row = await queries.get_server(conn, resource_id)
    else:
        row = await queries.get_project(conn, resource_id)
    return row["org_id"] if row else None


async def _set_role(
    kind: str,
    resource_id: UUID,
    share_id: UUID,
    payload: ShareRoleIn,
    principal: Principal,
) -> ShareOut:
    await _require_admin(principal.claims, kind, resource_id)
    async with user_session(principal.claims) as conn:
        row = await queries.update_share_role(
            conn, kind, resource_id, share_id, payload.role
        )
    if row is None:
        raise HTTPException(status_code=404, detail="No such share.")
    return _to_out(row, viewer_id=principal.user_id)


async def _revoke(
    kind: str, resource_id: UUID, share_id: UUID, principal: Principal
) -> None:
    # No admin check: the policy permits an admin of the resource *or* the
    # recipient themselves, so this same route is how someone walks away from
    # something that was shared with them.
    async with user_session(principal.claims) as conn:
        removed = await queries.delete_share(conn, kind, resource_id, share_id)
    if not removed:
        raise HTTPException(
            status_code=404, detail="No such share, or not yours to remove."
        )


# --- servers ------------------------------------------------------------------


@router.get("/api/servers/{server_id}/shares", response_model=list[ShareOut])
async def list_server_shares(
    server_id: UUID, principal: Principal = Depends(current_principal)
) -> list[ShareOut]:
    return await _list("server", server_id, principal)


@router.post(
    "/api/servers/{server_id}/shares",
    response_model=ShareOut,
    status_code=status.HTTP_201_CREATED,
)
async def share_server(
    server_id: UUID,
    payload: ShareIn,
    principal: Principal = Depends(current_principal),
) -> ShareOut:
    return await _create("server", server_id, payload, principal)


@router.patch(
    "/api/servers/{server_id}/shares/{share_id}", response_model=ShareOut
)
async def set_server_share_role(
    server_id: UUID,
    share_id: UUID,
    payload: ShareRoleIn,
    principal: Principal = Depends(current_principal),
) -> ShareOut:
    return await _set_role("server", server_id, share_id, payload, principal)


@router.delete(
    "/api/servers/{server_id}/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unshare_server(
    server_id: UUID,
    share_id: UUID,
    principal: Principal = Depends(current_principal),
) -> None:
    await _revoke("server", server_id, share_id, principal)


# --- projects -----------------------------------------------------------------


@router.get("/api/projects/{project_id}/shares", response_model=list[ShareOut])
async def list_project_shares(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> list[ShareOut]:
    return await _list("project", project_id, principal)


@router.post(
    "/api/projects/{project_id}/shares",
    response_model=ShareOut,
    status_code=status.HTTP_201_CREATED,
)
async def share_project(
    project_id: UUID,
    payload: ShareIn,
    principal: Principal = Depends(current_principal),
) -> ShareOut:
    return await _create("project", project_id, payload, principal)


@router.patch(
    "/api/projects/{project_id}/shares/{share_id}", response_model=ShareOut
)
async def set_project_share_role(
    project_id: UUID,
    share_id: UUID,
    payload: ShareRoleIn,
    principal: Principal = Depends(current_principal),
) -> ShareOut:
    return await _set_role("project", project_id, share_id, payload, principal)


@router.delete(
    "/api/projects/{project_id}/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unshare_project(
    project_id: UUID,
    share_id: UUID,
    principal: Principal = Depends(current_principal),
) -> None:
    await _revoke("project", project_id, share_id, principal)
