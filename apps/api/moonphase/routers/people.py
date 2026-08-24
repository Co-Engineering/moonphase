"""The accounts on this instance, and who may change them.

Separate from organization membership on purpose. Every account owns its own
personal organization — a trigger makes one on signup — so "owner of an org"
says nothing about whether you may close registration or remove somebody else.
Instance administration is its own list, and this is what edits it.

Adding an account works with or without a mail server, because most instances
of this are one person and a VPS and will never have one. With SMTP configured
the invitation is emailed; without it the account is created with a password
generated here and shown once, for the administrator to pass on however they
like. Refusing to add anyone until a mail server exists would be correct and
useless.

Removing one is the dangerous direction, and it is guarded rather than
confirmed-away. Deleting an account cascades through its personal organization
and takes its servers and projects with it — rows, at least; the containers on
those machines carry on running with nothing left pointing at them. So an
account that still owns work is refused, with a count, and the administrator is
told to deal with the work first.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from uuid import UUID

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text

from .. import authconfig, updates
from ..auth import Principal, current_principal
from ..config import get_settings
from ..db import service_session, user_session
from ..schemas import (
    InstanceSettingsIn,
    InstanceSettingsOut,
    PersonInviteIn,
    PersonInviteOut,
    PersonOut,
    UpdateStateOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/instance", tags=["instance"])


def _auth_base() -> str:
    """Where GoTrue answers.

    Configured directly in the compose stack, because the proxy that fronts it
    for browsers is not on the path between two containers on the same network.
    Falling back to the public URL keeps development working, where the API
    talks to whatever `SUPABASE_URL` points at.
    """
    settings = get_settings()
    if settings.moonphase_auth_url:
        return settings.moonphase_auth_url.rstrip("/")
    return f"{settings.supabase_url.rstrip('/')}/auth/v1"


def _service_token() -> str:
    """A short-lived token asserting the service role.

    GoTrue's admin endpoints ask for one. Minted here from the secret both
    services already share rather than kept anywhere, so there is no long-lived
    admin key to leak — it exists for the duration of one call.
    """
    settings = get_settings()
    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=503,
            detail="This instance has no shared auth secret, so accounts "
            "cannot be managed from here.",
        )
    now = int(time.time())
    return jwt.encode(
        {"role": "service_role", "iss": "moonphase", "iat": now, "exp": now + 60},
        settings.supabase_jwt_secret,
        algorithm="HS256",
    )


async def _gotrue(method: str, path: str, **kwargs: object) -> dict:
    """One call to GoTrue's admin API, with its errors turned into ours."""
    url = f"{_auth_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {_service_token()}"},
                **kwargs,  # type: ignore[arg-type]
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not reach the auth service: {exc}"
        ) from exc

    if response.status_code >= 400:
        # GoTrue says why in `msg`; anything else is better than the status code
        # alone, which people cannot act on.
        detail = "The auth service refused that."
        try:
            body = response.json()
            detail = body.get("msg") or body.get("error_description") or detail
        except ValueError:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)

    if not response.content:
        return {}
    return response.json()


async def _is_admin(user_id: str) -> bool:
    async with service_session() as conn:
        found = await conn.execute(
            text("select 1 from instance_admins where user_id = cast(:id as uuid)"),
            {"id": user_id},
        )
        return found.first() is not None


async def _require_admin(principal: Principal) -> None:
    if not await _is_admin(str(principal.user_id)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an administrator of this Moonphase can manage accounts.",
        )


@router.get("/people", response_model=list[PersonOut])
async def list_people(
    principal: Principal = Depends(current_principal),
) -> list[PersonOut]:
    """Everyone with an account here.

    Read as service_role because `auth.users` belongs to GoTrue and has no
    policies of ours on it. The guard is the admin check above rather than the
    database, which is why it is the first thing this does.
    """
    await _require_admin(principal)

    async with service_session() as conn:
        rows = await conn.execute(
            text(
                """
                select u.id,
                       u.email,
                       u.created_at,
                       u.last_sign_in_at,
                       (a.user_id is not null) as is_admin,
                       (
                         select count(*) from projects p
                         join org_members m on m.org_id = p.org_id
                         where m.user_id = u.id and m.role = 'owner'
                       ) as owned_projects
                  from auth.users u
                  left join instance_admins a on a.user_id = u.id
                 order by u.created_at asc
                """
            )
        )
        people = rows.mappings().all()

    return [
        PersonOut(
            id=str(row["id"]),
            email=row["email"] or "",
            created_at=row["created_at"],
            last_sign_in_at=row["last_sign_in_at"],
            is_admin=bool(row["is_admin"]),
            owned_projects=int(row["owned_projects"] or 0),
            is_you=str(row["id"]) == str(principal.user_id),
        )
        for row in people
    ]


@router.post("/people", response_model=PersonInviteOut, status_code=201)
async def invite_person(
    payload: PersonInviteIn,
    principal: Principal = Depends(current_principal),
) -> PersonInviteOut:
    """Create an account for somebody.

    The password is generated rather than chosen by the administrator: one
    picked by the person creating the account is a password two people know,
    and it is invariably the same one they picked last time.
    """
    await _require_admin(principal)

    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="That is not an email address.")

    async with service_session() as conn:
        existing = await conn.execute(
            text("select 1 from auth.users where lower(email) = :email"),
            {"email": email},
        )
        if existing.first() is not None:
            raise HTTPException(
                status_code=409, detail=f"{email} already has an account here."
            )

    # 20 characters of url-safe base64 — around 120 bits, which is far past
    # anything that will be guessed and still short enough to read down a phone.
    password = secrets.token_urlsafe(15)

    # GoTrue's own admin API rather than an insert into its table. The columns
    # of `auth.users` are its business and change between versions; a row
    # written by hand works until the day it does not, and the failure would be
    # an account that exists and cannot sign in.
    created = await _gotrue(
        "POST",
        "/admin/users",
        json={
            "email": email,
            "password": password,
            # No mail server on most instances of this, so waiting for a
            # confirmation nobody can send would create an account that cannot
            # be used. The administrator vouched for the address by typing it.
            "email_confirm": True,
        },
    )
    user_id = str(created.get("id") or "")
    if not user_id:  # pragma: no cover — GoTrue returns the row or an error
        raise HTTPException(status_code=502, detail="The auth service returned no id.")

    if payload.admin:
        async with service_session() as conn:
            await conn.execute(
                text(
                    """
                    insert into instance_admins (user_id, added_by)
                    values (cast(:id as uuid), cast(:by as uuid))
                    on conflict (user_id) do nothing
                    """
                ),
                {"id": user_id, "by": str(principal.user_id)},
            )

    log.info("account created for %s by %s", email, principal.user_id)
    return PersonInviteOut(
        id=user_id,
        email=email,
        # Shown once. Nothing stores it, and there is nowhere to look it up
        # afterwards — the row holds a hash.
        password=password,
        is_admin=payload.admin,
    )


@router.delete("/people/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_person(
    user_id: UUID, principal: Principal = Depends(current_principal)
) -> None:
    """Delete an account.

    Refused while it still owns projects. Deleting it cascades through its
    personal organization and takes those rows with it, and the containers they
    describe would carry on running on the servers with nothing left pointing at
    them — a mess that is invisible until someone wonders why a machine is busy.
    """
    await _require_admin(principal)

    if str(user_id) == str(principal.user_id):
        raise HTTPException(
            status_code=400,
            detail="You cannot remove your own account. Ask another "
            "administrator, or hand the instance over first.",
        )

    async with service_session() as conn:
        found = await conn.execute(
            text(
                """
                select u.email,
                       (
                         select count(*) from projects p
                         join org_members m on m.org_id = p.org_id
                         where m.user_id = u.id and m.role = 'owner'
                       ) as owned_projects
                  from auth.users u
                 where u.id = :id
                """
            ),
            {"id": str(user_id)},
        )
        row = found.first()
        if row is None:
            raise HTTPException(status_code=404, detail="No such account.")

        owned = int(row.owned_projects or 0)
        if owned:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{row.email} still owns {owned} "
                    f"project{'s' if owned != 1 else ''}. Removing the account "
                    "would delete them, and leave their containers running on "
                    "your servers with nothing pointing at them. Delete or hand "
                    "over the projects first."
                ),
            )

        # The last administrator cannot be removed — nor demoted, below. An
        # instance nobody can administer is only recoverable with a database
        # client.
        remaining = await conn.execute(
            text(
                "select count(*) from instance_admins where user_id <> cast(:id as uuid)"
            ),
            {"id": str(user_id)},
        )
        if int(remaining.scalar() or 0) == 0:
            raise HTTPException(
                status_code=409,
                detail="That is the last administrator. Make somebody else one first.",
            )

    # Again through GoTrue, which owns these rows and knows what else it keeps
    # beside them — sessions, refresh tokens, identities.
    await _gotrue("DELETE", f"/admin/users/{user_id}")

    log.info("account %s removed by %s", user_id, principal.user_id)


@router.put("/people/{user_id}/admin", response_model=PersonOut)
async def set_admin(
    user_id: UUID,
    make_admin: bool,
    principal: Principal = Depends(current_principal),
) -> PersonOut:
    """Grant or revoke administration of this instance."""
    await _require_admin(principal)

    async with service_session() as conn:
        if make_admin:
            await conn.execute(
                text(
                    """
                    insert into instance_admins (user_id, added_by)
                    values (cast(:id as uuid), cast(:by as uuid))
                    on conflict (user_id) do nothing
                    """
                ),
                {"id": str(user_id), "by": str(principal.user_id)},
            )
        else:
            remaining = await conn.execute(
                text(
                    "select count(*) from instance_admins "
                    "where user_id <> cast(:id as uuid)"
                ),
                {"id": str(user_id)},
            )
            if int(remaining.scalar() or 0) == 0:
                raise HTTPException(
                    status_code=409,
                    detail="That is the last administrator. Make somebody else "
                    "one first.",
                )
            await conn.execute(
                text("delete from instance_admins where user_id = cast(:id as uuid)"),
                {"id": str(user_id)},
            )

        row = (
            await conn.execute(
                text(
                    """
                    select u.id, u.email, u.created_at, u.last_sign_in_at,
                           (a.user_id is not null) as is_admin
                      from auth.users u
                      left join instance_admins a on a.user_id = u.id
                     where u.id = :id
                    """
                ),
                {"id": str(user_id)},
            )
        ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="No such account.")

    return PersonOut(
        id=str(row["id"]),
        email=row["email"] or "",
        created_at=row["created_at"],
        last_sign_in_at=row["last_sign_in_at"],
        is_admin=bool(row["is_admin"]),
        owned_projects=0,
        is_you=str(row["id"]) == str(principal.user_id),
    )


@router.get("/me", response_model=dict)
async def whoami(principal: Principal = Depends(current_principal)) -> dict:
    """Whether the caller administers this instance.

    Its own endpoint because the client needs it before it can decide what to
    draw, and asking for the list of accounts to find out would 403 for exactly
    the people who should not see it.
    """
    return {"is_instance_admin": await _is_admin(str(principal.user_id))}

@router.get("/settings", response_model=InstanceSettingsOut)
async def read_settings(
    principal: Principal = Depends(current_principal),
) -> InstanceSettingsOut:
    """What this instance is configured to be.

    Its own endpoint rather than the unauthenticated `/api/setup`, which
    deliberately answers only "does this need setting up" — telling anyone who
    can reach the port what domain it believes in is a different question, and
    one for people who are signed in and administer it.
    """
    await _require_admin(principal)

    async with service_session() as conn:
        row = (
            await conn.execute(
                text("select public_url, signup_open from instance_settings limit 1")
            )
        ).first()

    return InstanceSettingsOut(
        public_url=row.public_url if row else None,
        signup_open=bool(row.signup_open) if row else True,
    )


@router.put("/settings", response_model=InstanceSettingsOut)
async def write_settings(
    payload: InstanceSettingsIn,
    principal: Principal = Depends(current_principal),
) -> InstanceSettingsOut:
    """Change the domain, or whether anyone else may sign up.

    Written through the caller's own session rather than the service role, so
    the database's policy has to agree as well as the check above. Two locks on
    one door, because the thing behind it is who else can get in.
    """
    await _require_admin(principal)

    async with user_session(principal.claims) as conn:
        result = await conn.execute(
            text(
                """
                update instance_settings
                   set public_url = :url,
                       signup_open = :signup_open,
                       updated_at = now()
                 returning public_url, signup_open
                """
            ),
            {
                # Same normalising as setup: a domain typed without a scheme is
                # the common case, and stored as typed it is not a URL.
                "url": authconfig.normalise_public_url(payload.public_url),
                "signup_open": payload.signup_open,
            },
        )
        row = result.first()

    if row is None:
        raise HTTPException(
            status_code=403,
            detail="Only an administrator of this Moonphase can change its settings.",
        )

    log.info(
        "instance settings changed by %s: public_url=%s signup_open=%s",
        principal.user_id, row.public_url, row.signup_open,
    )
    return InstanceSettingsOut(
        public_url=row.public_url, signup_open=bool(row.signup_open)
    )


# Where the updater, if one is running, reads requests and writes what happened.
# Its presence is how the API knows whether one-click updates are available:
# the volume is mounted by the opt-in compose file and by nothing else.
UPDATE_DIR = Path(os.environ.get("MOONPHASE_UPDATE_DIR", "/updates"))


def _updater_present() -> bool:
    return UPDATE_DIR.is_dir()


def _updater_status() -> tuple[str | None, str | None]:
    """(state, detail) from the updater's last run, if it has had one."""
    try:
        raw = (UPDATE_DIR / "status").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    state, _, detail = raw.partition("|")
    return state.strip() or None, detail.strip() or None


def _describe(state: updates.UpdateState) -> UpdateStateOut:
    status, status_detail = _updater_status()
    return UpdateStateOut(
        running_version=state.running_version,
        running_commit=state.running_commit,
        latest_version=state.latest_version,
        release_url=state.release_url,
        release_notes=state.release_notes,
        published_at=state.published_at,
        update_available=state.update_available,
        detail=state.detail,
        can_apply=_updater_present(),
        status=status,
        status_detail=status_detail,
        # Given rather than described, because the alternative is somebody
        # typing a half-remembered version of it.
        command="cd moonphase && docker compose pull && docker compose up -d",
    )


@router.get("/update", response_model=UpdateStateOut)
async def update_state(
    force: bool = False, principal: Principal = Depends(current_principal)
) -> UpdateStateOut:
    """Whether a newer release exists than the one running.

    Administrators only: which build is running is a small thing to know about
    somebody's server, and no business of everyone with an account on it.
    """
    await _require_admin(principal)
    return _describe(await updates.check(force=force))


@router.post("/update", response_model=UpdateStateOut)
async def apply_update(principal: Principal = Depends(current_principal)) -> UpdateStateOut:
    """Ask the updater to pull and restart.

    Writes a nonce into the shared volume and returns immediately. It cannot
    wait for the result: applying an update recreates this container, so the
    request that started it does not survive to answer. The client polls the
    status the updater leaves behind, which outlives both of them.
    """
    await _require_admin(principal)

    if not _updater_present():
        raise HTTPException(
            status_code=409,
            detail="This instance has no updater. Add docker-compose.update.yml "
            "to turn on one-click updates, or run the command yourself.",
        )

    state = await updates.check()
    if state.update_available is not True:
        raise HTTPException(
            status_code=409,
            detail="There is no newer release to update to.",
        )

    try:
        # The contents are a nonce and nothing reads them: the updater's only
        # question is whether the file changed. Nothing here can ask it to run
        # anything of the caller's choosing, which is the point.
        (UPDATE_DIR / "request").write_text(
            f"{time.time()}-{secrets.token_hex(8)}\n", encoding="utf-8"
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not reach the updater: {exc}"
        ) from exc

    log.info("update requested by %s", principal.user_id)
    updates.forget()
    return _describe(state)
