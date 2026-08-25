"""Say what is wrong at startup, rather than at the worst possible moment.

Every misconfiguration this project has documented shares a shape: nothing
complains at boot, and the symptom arrives much later wearing a disguise. A
`SUPABASE_URL` pointing at the wrong instance becomes "Invalid token" on every
request. A database that is up but unmigrated becomes a 500 on the first page
someone opens. A missing `MOONPHASE_SECRET_KEY` waits until you add your first
server, which is the one moment you least want to find out.

All three are knowable in the first second. So they are checked then, and each
one that fails says the specific thing to do about it.

The distinction between fatal and a warning is whether the process can do its
job at all. No encryption key means it cannot hold a credential, so there is no
point being up; no push keys means notifications are off, which is a smaller
product rather than a broken one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import text

from .config import get_settings
from .db import service_session

log = logging.getLogger(__name__)

# A database that is merely slow to accept connections is normal on a cold
# start; one that is absent is not. This is the line between them.
DB_ATTEMPTS = 10
DB_DELAY_SECONDS = 2.0

AUTH_TIMEOUT_SECONDS = 5.0

# Tables the API cannot work without. Their absence means the migrations have
# not run, which is a different problem from the database being down and
# deserves a different sentence.
REQUIRED_TABLES = ("organizations", "servers", "projects", "project_sessions")


@dataclass
class Finding:
    fatal: bool
    summary: str
    # What to actually do. A diagnosis without one is only half an error.
    fix: str = ""

    def log(self) -> None:
        message = f"{self.summary} {self.fix}".strip()
        if self.fatal:
            log.error("preflight: %s", message)
        else:
            log.warning("preflight: %s", message)


class PreflightFailed(RuntimeError):
    """Raised when the process cannot do its job with this configuration."""


def check_secret_key() -> Finding | None:
    """The key that encrypts SSH and harness credentials at rest.

    Only its shape. A *missing* key is already refused when Settings loads,
    with the command to generate one — which is earlier and better than here.
    What that does not catch is a key that is present and unusable, which then
    waits until someone adds their first server to fail.
    """
    key = get_settings().moonphase_secret_key
    try:
        Fernet(key.encode())
    except Exception:
        return Finding(
            fatal=True,
            summary="MOONPHASE_SECRET_KEY is not a valid Fernet key.",
            fix="It must be 32 url-safe base64 bytes. Generating a new one makes "
            "every stored SSH key unreadable, so check for a typo first.",
        )
    return None


def check_cors() -> Finding | None:
    """A wildcard origin plus credentialed requests is not what it looks like.

    The CORS spec forbids `Access-Control-Allow-Origin: *` alongside
    `Access-Control-Allow-Credentials: true`, so when asked for both,
    Starlette reflects whatever `Origin` header the request sent instead of
    rejecting it or erroring. Credentials are always on here (main.py), so a
    literal `*` silently grants every origin credentialed access to the API.
    """
    if "*" in get_settings().cors_origins:
        return Finding(
            fatal=True,
            summary='MOONPHASE_CORS_ORIGINS includes "*", which is not a valid value.',
            fix="Credentialed requests are always enabled, so a wildcard origin is "
            "silently reflected back for every caller instead of being rejected. "
            "List the exact origin(s) that should be allowed, comma-separated.",
        )
    return None


async def check_database() -> Finding | None:
    """Reachable, and carrying the schema this version expects."""
    last: Exception | None = None
    for attempt in range(DB_ATTEMPTS):
        try:
            async with service_session() as conn:
                found = {
                    row[0]
                    for row in await conn.execute(
                        text(
                            "select table_name from information_schema.tables "
                            "where table_schema = 'public'"
                        )
                    )
                }
            missing = [table for table in REQUIRED_TABLES if table not in found]
            if missing:
                return Finding(
                    fatal=True,
                    summary=(
                        "The database is reachable but has not been migrated "
                        f"(missing: {', '.join(missing)})."
                    ),
                    fix="Run the migrations: `supabase db push`, or bring the stack "
                    "up with `docker compose up -d`, which runs them for you.",
                )
            return None
        except Exception as exc:  # noqa: BLE001 — any failure to connect counts
            last = exc
            if attempt < DB_ATTEMPTS - 1:
                await asyncio.sleep(DB_DELAY_SECONDS)

    return Finding(
        fatal=True,
        summary=f"Cannot reach the database after {DB_ATTEMPTS} attempts ({last}).",
        fix="Check DATABASE_URL. It must use the asyncpg driver: "
        "postgresql+asyncpg://…",
    )


async def check_auth() -> Finding | None:
    """Whether sign-in will work.

    `SUPABASE_URL` is the address a *browser* uses. In a normal deployment the
    proxy that serves it sits in front of this container, not behind it, so the
    API cannot reach that address and is not supposed to need to — with a
    shared JWT secret it verifies tokens itself and never calls out.

    Probing it unconditionally therefore warned on every correct install, which
    is worse than not checking: a warning that fires when everything is fine
    teaches people to ignore warnings. So it is only probed when the API
    genuinely depends on reaching it, which is when there is no shared secret
    and tokens must be verified against the published JWKS.
    """
    settings = get_settings()
    if not settings.supabase_url:
        return Finding(
            fatal=True,
            summary="SUPABASE_URL is not set, so no one can sign in.",
            fix="Point it at the address your browser uses to reach this install.",
        )

    if settings.supabase_jwt_secret:
        return None

    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/health"
    try:
        async with httpx.AsyncClient(timeout=AUTH_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            return Finding(
                fatal=False,
                summary=f"The auth service answered {response.status_code} at {url}.",
                fix="Sign-in will fail until this responds. Check SUPABASE_URL.",
            )
    except Exception as exc:  # noqa: BLE001
        return Finding(
            fatal=False,
            summary=f"Cannot reach the auth service at {url} ({exc}).",
            fix="With no SUPABASE_JWT_SECRET set, tokens can only be verified "
            "against the JWKS published there, so every request will be "
            "rejected as invalid until this responds.",
        )

    return None


def check_push() -> Finding | None:
    """Notifications are the product working while you are gone."""
    settings = get_settings()
    if settings.moonphase_vapid_public_key and settings.moonphase_vapid_private_key:
        return None
    return Finding(
        fatal=False,
        summary="Push notifications are off — no VAPID keypair is configured.",
        fix="Generate one with `python scripts/gen_vapid.py >> .env`. Without it "
        "you will not be told when an agent is waiting for you.",
    )


def check_monitor() -> Finding | None:
    if get_settings().moonphase_monitor_interval > 0:
        return None
    return Finding(
        fatal=False,
        summary="The session monitor is disabled (MOONPHASE_MONITOR_INTERVAL=0).",
        fix="Activity states, notifications and budget alerts all depend on it.",
    )


async def run() -> list[Finding]:
    """Every check, in the order a reader would want them.

    Returns the findings after logging them, and raises if any is fatal.
    """
    findings: list[Finding] = []

    for finding in (check_secret_key(), check_cors(), check_monitor(), check_push()):
        if finding is not None:
            findings.append(finding)

    for coroutine in (check_database(), check_auth()):
        finding = await coroutine
        if finding is not None:
            findings.append(finding)

    for finding in findings:
        finding.log()

    fatal = [finding for finding in findings if finding.fatal]
    if fatal:
        raise PreflightFailed(
            "Moonphase cannot start: "
            + " ".join(f"{item.summary} {item.fix}".strip() for item in fatal)
        )

    if not findings:
        log.info("preflight: configuration looks complete")
    return findings
