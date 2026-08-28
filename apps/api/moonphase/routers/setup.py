"""First run, and the settings that outlive it.

Installing something should not end with "now edit a file on the server". The
address this answers on, and whether anyone else may create an account, are
decisions a person makes after seeing the thing work — so they live in the
database, behind a screen, and can be changed later without a shell.

What stays in .env is only what must exist before the database does: the key
that encrypts credentials, and the password that reaches Postgres. Neither is a
choice anyone makes.

Three of these routes are deliberately unauthenticated, and each is careful
about what it admits to. Before setup there is no account to authenticate with,
which is the whole problem; afterwards they say only whether a thing is allowed,
never what is configured.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .. import authconfig, queries
from ..auth import Principal, current_principal
from ..db import service_session, user_session
from ..schemas import AuthMethodsIn, AuthMethodsOut, SetupIn, SetupStateOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])


def host_of(url: str) -> str:
    """The hostname alone, which is what a certificate is issued for."""
    # Lowercased first: the scheme is matched case-insensitively, and a URL
    # typed as HTTPS:// would otherwise keep its prefix and reduce to "https",
    # which is refused a certificate for the name it actually names.
    host = url.strip().lower()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host.split("/")[0].split(":")[0].strip()


async def _require_instance_admin(principal: Principal) -> None:
    """Defense in depth alongside the `auth_methods_write` RLS policy.

    That policy is the real gate — every org's owner is `owner`/`admin` of
    their own personal org from the moment they sign up, which is not the
    same thing as administering this instance, and a prior version of this
    policy conflated the two. Checking again here means a future regression
    in the DB policy fails closed at this layer too, rather than silently
    reopening "any signed-in user can rewrite the instance's SMTP relay and
    OAuth secrets."
    """
    async with service_session() as conn:
        found = await conn.execute(
            text("select 1 from instance_admins where user_id = cast(:id as uuid)"),
            {"id": str(principal.user_id)},
        )
    if found.first() is None:
        raise HTTPException(
            status_code=403,
            detail="Only an administrator of this Moonphase can change how people sign in.",
        )


async def _state() -> dict:
    """Whether anyone has signed up yet, and what the instance is set to.

    Read with the service role because the caller may have no account at all —
    that is precisely the case this exists to handle.
    """
    async with service_session() as conn:
        users = await conn.execute(text("select count(*) from auth.users"))
        count = int(users.scalar() or 0)
        row = (
            await conn.execute(
                text(
                    "select public_url, signup_open, setup_completed_at "
                    "from instance_settings limit 1"
                )
            )
        ).first()
    return {
        "users": count,
        "public_url": row.public_url if row else None,
        "signup_open": bool(row.signup_open) if row else True,
        "completed_at": row.setup_completed_at if row else None,
    }


@router.get("", response_model=SetupStateOut)
async def state() -> SetupStateOut:
    """Whether this instance still needs setting up.

    Unauthenticated on purpose: the client has to know whether to show a setup
    screen or a sign-in form, and before the first account exists there is
    nobody who could authenticate. It discloses only that — not the address,
    not anything configured.

    Driven by `setup_completed_at`, not `auth.users` count. Count is not
    sticky: complete() already guards against re-claiming an instance once
    it has an administrator, but that guard means nothing if this screen —
    which needs no authentication at all — reopens itself the moment
    `auth.users` is ever empty for any other reason. `setup_completed_at` is
    set once and never cleared, so a completed instance stays completed.
    """
    found = await _state()
    return SetupStateOut(
        needs_setup=found["completed_at"] is None,
        signup_open=found["signup_open"],
    )


@router.post("", response_model=SetupStateOut)
async def complete(
    payload: SetupIn, principal: Principal = Depends(current_principal)
) -> SetupStateOut:
    """Finish setting up, as the account that just signed up.

    Authenticated, so this cannot be raced by someone who is not the person
    installing — by the time it is called they hold a token, which means they
    are the first account.
    """
    # Claim the instance, if nobody has. Setting it up is what makes you its
    # administrator, and on a fresh install there is nobody to grant it: the
    # settings below are behind a policy that asks whether the caller is one,
    # so without this the person installing would be refused by the screen they
    # are installing with.
    #
    # Only ever from empty. A later caller who is not an administrator falls
    # through to the update, which quietly matches no rows and answers 403.
    async with service_session() as conn:
        await conn.execute(
            text(
                """
                insert into instance_admins (user_id)
                select cast(:user_id as uuid)
                 where not exists (select 1 from instance_admins)
                on conflict (user_id) do nothing
                """
            ),
            {"user_id": str(principal.user_id)},
        )

    async with user_session(principal.claims) as conn:
        result = await conn.execute(
            text(
                """
                update instance_settings
                   set public_url = coalesce(:url, public_url),
                       signup_open = :signup_open,
                       setup_completed_at = coalesce(setup_completed_at, now()),
                       updated_at = now()
                 returning public_url, signup_open
                """
            ),
            {
                # What was typed, made into an address. A bare name is what
                # people write and is not a URL; everything downstream needs one.
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
        "setup completed: public_url=%s signup_open=%s", row.public_url, row.signup_open
    )
    # Otherwise GoTrue keeps its bootstrap defaults — the site URL, the
    # signup-allowed OTP/OAuth endpoints — until someone separately saves the
    # "ways to sign in" screen, even though this is the point where an
    # administrator has just decided both.
    await publish_auth_config()
    return SetupStateOut(
        needs_setup=False,
        signup_open=bool(row.signup_open),
        public_url=row.public_url,
    )


@router.get("/signup-allowed")
async def signup_allowed(response: Response) -> Response:
    """Whether the proxy should let a signup through.

    GoTrue's own switch is an environment variable, so closing signup its way
    would mean restarting a container — which is exactly the kind of thing this
    module exists to avoid. Caddy asks here instead, per request.

    Open while there are no accounts, because otherwise nobody could ever make
    the first one.

    A refusal answers in GoTrue's own error shape. Caddy copies a non-2xx
    response here straight to the client, and the client is the Supabase
    library, which calls `.json()` on whatever it gets. An empty 403 therefore
    surfaced as "Failed to execute 'json' on 'Response': Unexpected end of JSON
    input" — a parser complaining, in place of the one sentence that would have
    explained it.
    """
    found = await _state()
    if found["users"] == 0 or found["signup_open"]:
        return Response(status_code=200)
    return JSONResponse(
        status_code=403,
        content={
            "code": 403,
            "error_code": "signup_disabled",
            "msg": "This Moonphase is not accepting new accounts.",
        },
    )


@router.get("/tls-allowed")
async def tls_allowed(domain: str = Query(default="")) -> Response:
    """Whether the proxy may obtain a certificate for this hostname.

    Caddy asks before every certificate it does not already hold. Answering
    from the database is what lets someone type their domain into a setup
    screen and have HTTPS work, with no file to edit and nothing to restart.

    Anything not configured here is refused: an open answer would let anyone
    pointing a DNS record at this address mint certificates against it, and
    rate limits are shared.
    """
    asked = host_of(domain)
    if not asked:
        return Response(status_code=400)

    found = await _state()
    configured = host_of(found["public_url"] or "")
    if configured and asked == configured:
        return Response(status_code=200)

    log.info("refused a certificate for %r (configured: %r)", asked, configured or None)
    return Response(status_code=403)


# --- how people sign in --------------------------------------------------------


def _methods_from(row: dict) -> authconfig.AuthMethods:
    return authconfig.AuthMethods(
        password_enabled=bool(row.get("password_enabled", True)),
        magic_link_enabled=bool(row.get("magic_link_enabled")),
        smtp_host=row.get("smtp_host") or "",
        smtp_port=int(row.get("smtp_port") or 587),
        smtp_user=row.get("smtp_user") or "",
        smtp_sender=row.get("smtp_sender") or "",
        smtp_password=row.get("smtp_password") or "",
        google_enabled=bool(row.get("google_enabled")),
        google_client_id=row.get("google_client_id") or "",
        google_client_secret=row.get("google_client_secret") or "",
        microsoft_enabled=bool(row.get("microsoft_enabled")),
        microsoft_client_id=row.get("microsoft_client_id") or "",
        microsoft_client_secret=row.get("microsoft_client_secret") or "",
        microsoft_tenant=row.get("microsoft_tenant") or "common",
        public_url=row.get("public_url") or "",
        signup_open=bool(row.get("signup_open", True)),
    )


async def publish_auth_config() -> list[str]:
    """Render the current settings and hand them to the auth container.

    Written only when it has actually changed, because the container restarts
    whenever the file does and rewriting identical bytes on every save would
    sign everyone out for no reason.
    """
    async with service_session() as conn:
        row = await queries.get_auth_methods_privileged(conn)
    methods = _methods_from(row)
    rendered = authconfig.render(methods)

    await asyncio.to_thread(_write_config, rendered)
    return authconfig.usable(methods)


# Why the last handoff to the auth container failed, or None if it did not.
#
# Kept because the failure is otherwise invisible: the settings are saved to
# the database either way, so the screen said "saved" while GoTrue never
# received the file and every OAuth sign-in answered "provider is not enabled".
# A setting that cannot reach the thing it configures is not a saved setting,
# and the screen has to say so.
_handoff_error: str | None = None


def _write_config(rendered: str) -> None:
    """Off the event loop: small, but a write to a shared volume all the same."""
    global _handoff_error
    path = Path(authconfig.CONFIG_PATH)
    try:
        if path.exists() and path.read_text() == rendered:
            _handoff_error = None
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        _handoff_error = None
        log.info("wrote auth configuration; the auth service will restart")
    except OSError as exc:
        # Outside the compose stack this is development and nothing is wrong.
        # Inside it, it means the volume is not writable by this user, and the
        # sign-in methods on the screen are not the ones in force.
        _handoff_error = (
            "The sign-in settings were saved but could not be handed to the "
            f"auth service ({exc.strerror or exc}), so they are not in force "
            "yet. Bring the stack up again to repair it: "
            "docker compose up -d"
        )
        log.warning("could not write %s (%s)", authconfig.CONFIG_PATH, exc)


@router.get("/methods", response_model=AuthMethodsOut)
async def read_methods() -> AuthMethodsOut:
    """Which ways in exist.

    Unauthenticated, and deliberately thin: a sign-in screen has to know which
    buttons to draw before anyone has signed in. It lists what is enabled and
    working, and nothing about how any of it is configured.
    """
    async with service_session() as conn:
        row = await queries.get_auth_methods_privileged(conn)
    methods = _methods_from(row)
    return AuthMethodsOut(
        enabled=authconfig.usable(methods),
        password_enabled=methods.password_enabled,
        magic_link_enabled=methods.magic_link_enabled,
        google_enabled=methods.google_enabled,
        microsoft_enabled=methods.microsoft_enabled,
        smtp_host=methods.smtp_host,
        smtp_port=methods.smtp_port,
        smtp_user=methods.smtp_user,
        smtp_sender=methods.smtp_sender,
        google_client_id=methods.google_client_id,
        microsoft_client_id=methods.microsoft_client_id,
        microsoft_tenant=methods.microsoft_tenant,
        redirect_uri=authconfig.redirect_uri(methods.public_url),
        # The handoff failure comes last: the configuration problems above are
        # about what was asked for, this is about whether it arrived.
        problems=authconfig.incomplete(methods)
        + ([_handoff_error] if _handoff_error else []),
    )


@router.put("/methods", response_model=AuthMethodsOut)
async def write_methods(
    payload: AuthMethodsIn, principal: Principal = Depends(current_principal)
) -> AuthMethodsOut:
    """Change how people sign in, and hand the result to the auth service."""
    await _require_instance_admin(principal)
    async with user_session(principal.claims) as conn:
        try:
            await queries.set_auth_methods(
                conn,
                fields={
                    "password_enabled": payload.password_enabled,
                    "magic_link_enabled": payload.magic_link_enabled,
                    "smtp_host": payload.smtp_host,
                    "smtp_port": payload.smtp_port,
                    "smtp_user": payload.smtp_user,
                    "smtp_sender": payload.smtp_sender,
                    "google_enabled": payload.google_enabled,
                    "google_client_id": payload.google_client_id,
                    "microsoft_enabled": payload.microsoft_enabled,
                    "microsoft_client_id": payload.microsoft_client_id,
                    "microsoft_tenant": payload.microsoft_tenant or "common",
                },
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Secrets go through the service role, because `private` is unreachable
    # from the authenticated one — which is the point of it.
    async with service_session() as conn:
        await queries.set_auth_secrets_privileged(
            conn,
            secrets={
                "google_client_secret": payload.google_client_secret,
                "microsoft_client_secret": payload.microsoft_client_secret,
                "smtp_password": payload.smtp_password,
            },
        )

    await publish_auth_config()
    return await read_methods()
