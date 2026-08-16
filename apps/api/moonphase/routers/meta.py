"""Organizations, harness catalogue, credentials and health."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text

from .. import environments, queries
from .. import harness as harness_registry
from ..auth import Principal, current_principal
from ..db import service_session, user_session
from ..runtime import CAN_ADMINISTER, Forbidden
from ..schemas import (
    EnvironmentIn,
    EnvironmentOut,
    HarnessCredentialIn,
    HarnessCredentialOut,
    HarnessInfoOut,
    HealthOut,
    OrganizationOut,
    SessionOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["meta"])

VERSION = "0.1.0"


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    database = "ok"
    try:
        async with service_session() as conn:
            await conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 — health must report, not raise
        database = f"error: {exc}"[:200]
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        version=VERSION,
        database=database,
    )


@router.get("/sessions", response_model=list[SessionOut])
async def list_all_sessions(
    principal: Principal = Depends(current_principal),
) -> list[SessionOut]:
    """Every session the caller can see, across every project.

    One database query for the whole sidebar. Asking per project would be a
    request each, and none of them would tell you anything the others did not.
    """
    async with user_session(principal.claims) as conn:
        rows = await queries.list_all_sessions(conn)
    return [SessionOut.model_validate(row) for row in rows]


@router.get("/organizations", response_model=list[OrganizationOut])
async def list_organizations(
    principal: Principal = Depends(current_principal),
) -> list[OrganizationOut]:
    async with user_session(principal.claims) as conn:
        rows = await queries.list_organizations(conn)
    return [OrganizationOut.model_validate(r) for r in rows]


@router.get("/harnesses", response_model=list[HarnessInfoOut])
async def list_harnesses(
    principal: Principal = Depends(current_principal),
) -> list[HarnessInfoOut]:
    """What Moonphase can run, and which of it is actually usable.

    `configured` is the one the UI cares about: offering a harness nobody has
    signed into produces a project whose terminal comes up unable to do
    anything, with no clue why.
    """
    async with user_session(principal.claims) as conn:
        org_id = await queries.personal_org_id(conn)

    configured: set[str] = set()
    if org_id is not None:
        async with service_session() as conn:
            for harness in harness_registry.available():
                row = await queries.resolve_harness_credential_privileged(
                    conn, org_id=org_id, project_id=org_id, harness=str(harness.kind)
                )
                if row is not None:
                    configured.add(str(harness.kind))

    return [
        HarnessInfoOut(
            kind=h.kind.value,
            display_name=h.display_name,
            supported_auth_modes=[m.value for m in h.supported_auth_modes],
            available=True,
            configured=h.kind.value in configured,
            login_supported=h.login_command() is not None,
        )
        for h in harness_registry.available()
    ]


@router.get("/environments", response_model=list[EnvironmentOut])
async def list_environments(
    principal: Principal = Depends(current_principal),
) -> list[EnvironmentOut]:
    """Base images a project container can run on.

    Moonphase's own entries plus anything this organization has defined. A
    custom entry sharing a key with a built-in replaces it, which is how you
    pin a familiar name to a different base.
    """
    async with user_session(principal.claims) as conn:
        org_id = await queries.personal_org_id(conn)
        rows = await queries.list_environments(conn)
        counts = (
            await queries.environment_usage(conn, org_id) if org_id is not None else {}
        )

    return [
        EnvironmentOut(
            key=env.key,
            display_name=env.display_name,
            description=env.description,
            base_image=env.base_image,
            setup_script=env.setup_script,
            builtin=env.builtin,
            project_count=counts.get(env.key, 0),
        )
        for env in environments.merge(rows)
    ]


@router.put("/environments", response_model=EnvironmentOut)
async def upsert_environment(
    payload: EnvironmentIn, principal: Principal = Depends(current_principal)
) -> EnvironmentOut:
    """Define or update an environment.

    The image is built on the server the first time a project uses it, so this
    returns immediately rather than waiting on a build that may take minutes.
    """
    async with user_session(principal.claims) as conn:
        try:
            org_id = await queries.resolve_org(conn, payload.org_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            row = await queries.upsert_environment(
                conn,
                org_id=org_id,
                key=payload.key,
                display_name=payload.display_name.strip(),
                description=(payload.description or "").strip() or None,
                base_image=payload.base_image,
                setup_script=(payload.setup_script or "").strip() or None,
                created_by=principal.user_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        count = await queries.count_projects_using_environment(conn, org_id, payload.key)

    env = environments.from_row(row)
    return EnvironmentOut(
        key=env.key,
        display_name=env.display_name,
        description=env.description,
        base_image=env.base_image,
        setup_script=env.setup_script,
        builtin=False,
        project_count=count,
    )


@router.delete("/environments/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_environment(
    key: str, principal: Principal = Depends(current_principal)
) -> None:
    """Remove a custom environment.

    Existing projects keep running: their containers are already built, and the
    project falls back to the default only if it is ever recreated.
    """
    async with user_session(principal.claims) as conn:
        org_id = await queries.resolve_org(conn, None)
        deleted = await queries.delete_environment(conn, org_id, key)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=(
                "No custom environment with that key. Built-in environments "
                "cannot be deleted, only shadowed by defining one with the "
                "same key."
            ),
        )


@router.get("/harness-credentials", response_model=list[HarnessCredentialOut])
async def list_harness_credentials(
    principal: Principal = Depends(current_principal),
) -> list[HarnessCredentialOut]:
    """Metadata only. The material itself never leaves the backend."""
    async with user_session(principal.claims) as conn:
        orgs = await queries.list_organizations(conn)
    org_ids: list[UUID] = [o["id"] for o in orgs]
    async with service_session() as conn:
        rows = await queries.list_harness_credentials(conn, org_ids)
    return [HarnessCredentialOut.model_validate(r) for r in rows]


@router.post(
    "/harness-credentials",
    response_model=HarnessCredentialOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_harness_credential(
    payload: HarnessCredentialIn, principal: Principal = Depends(current_principal)
) -> HarnessCredentialOut:
    try:
        payload.validate_material()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        # Resolving the org through the RLS-scoped session is what stops a
        # caller attaching a credential to an org they are not a member of.
        try:
            org_id = await queries.resolve_org(conn, payload.org_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        if payload.project_id is not None:
            project = await queries.get_project(conn, payload.project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found.")
            # Which account the agent runs as is the project owner's call. Being
            # able to see a project — as a collaborator, a viewer, or the owner
            # of the machine under it — must not let you swap its credentials.
            if project.get("access") not in CAN_ADMINISTER:
                raise Forbidden(
                    "Only an owner of this project can change which account it "
                    "runs as."
                )
            org_id = project["org_id"]

    async with service_session() as conn:
        row = await queries.upsert_harness_credential_privileged(
            conn,
            org_id=org_id,
            project_id=payload.project_id,
            harness=payload.harness,
            auth_mode=payload.auth_mode,
            label=payload.label,
            api_key=payload.api_key,
            oauth_blob=payload.oauth_blob,
            created_by=principal.user_id,
        )
    return HarnessCredentialOut.model_validate(row)
