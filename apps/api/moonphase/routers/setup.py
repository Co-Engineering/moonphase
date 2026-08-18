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

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text

from ..auth import Principal, current_principal
from ..db import service_session, user_session
from ..schemas import SetupIn, SetupStateOut

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
    """
    found = await _state()
    return SetupStateOut(
        needs_setup=found["users"] == 0,
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
            {"url": payload.public_url, "signup_open": payload.signup_open},
        )
        row = result.first()
    if row is None:
        raise HTTPException(status_code=403, detail="Only an owner can do that.")

    log.info(
        "setup completed: public_url=%s signup_open=%s", row.public_url, row.signup_open
    )
    return SetupStateOut(needs_setup=False, signup_open=bool(row.signup_open))


@router.get("/signup-allowed")
async def signup_allowed(response: Response) -> Response:
    """Whether the proxy should let a signup through.

    GoTrue's own switch is an environment variable, so closing signup its way
    would mean restarting a container — which is exactly the kind of thing this
    module exists to avoid. Caddy asks here instead, per request.

    Open while there are no accounts, because otherwise nobody could ever make
    the first one.
    """
    found = await _state()
    allowed = found["users"] == 0 or found["signup_open"]
    return Response(status_code=200 if allowed else 403)


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
