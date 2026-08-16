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
from ..schemas import (
    EnvironmentOut,
    HarnessCredentialIn,
    HarnessCredentialOut,
    HarnessInfoOut,
    HealthOut,
    OrganizationOut,
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
async def list_environments() -> list[EnvironmentOut]:
    """Base distributions a project container can run on."""
    return [
        EnvironmentOut(
            key=env.key,
            display_name=env.display_name,
            description=env.description,
            base_image=env.base_image,
        )
        for env in environments.available()
    ]


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
