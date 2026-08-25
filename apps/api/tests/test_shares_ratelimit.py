"""Share creation is rate limited per caller, end to end through the router.

Unlike test_ratelimit.py (the limiter in isolation), this exercises the real
`_create` handler in routers/shares.py — the admin check, the privileged
email lookup, and the limiter wired together — so a regression that drops
the check from the handler, not just from the limiter itself, is caught.

Runs against a local `supabase start`. Skipped when the database is
unreachable, same as test_sharing.py.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from moonphase import queries
from moonphase.auth import Principal
from moonphase.db import service_session, user_session
from moonphase.ratelimit import RateLimiter
from moonphase.routers import shares
from moonphase.schemas import ShareIn


async def _database_reachable() -> bool:
    try:
        async with service_session() as conn:
            await conn.execute(text("select 1"))
        return True
    except Exception:
        return False


async def _make_user(email: str) -> str:
    user_id = str(uuid.uuid4())
    async with service_session() as conn:
        await conn.execute(
            text(
                """
                insert into auth.users
                  (id, instance_id, aud, role, email, encrypted_password,
                   email_confirmed_at, created_at, updated_at)
                values
                  (cast(:id as uuid), '00000000-0000-0000-0000-000000000000',
                   'authenticated', 'authenticated', :email, 'x', now(), now(), now())
                """
            ),
            {"id": user_id, "email": email},
        )
    return user_id


@pytest.fixture
async def owner():
    """A person who owns a server, so they are an admin somewhere."""
    if not await _database_reachable():
        pytest.skip("Postgres is not reachable — run `supabase start`")
    suffix = uuid.uuid4().hex[:8]
    email = f"owner-{suffix}@example.test"
    user_id = await _make_user(email)
    claims = {"sub": user_id, "email": email, "role": "authenticated", "aud": "authenticated"}
    principal = Principal(user_id=user_id, email=email, claims=claims)

    async with user_session(claims) as conn:
        org_id = await queries.personal_org_id(conn)
        server = await queries.insert_server(
            conn,
            org_id=org_id,
            name=f"box-{suffix}",
            host="10.0.0.9",
            port=22,
            ssh_user="deploy",
            auth_mode="managed_key",
            created_by=user_id,
        )

    yield principal, server["id"]

    async with service_session() as conn:
        await conn.execute(
            text("delete from auth.users where id = cast(:id as uuid)"), {"id": user_id}
        )


async def test_a_burst_of_invitations_is_refused_past_the_limit(owner, monkeypatch) -> None:
    principal, server_id = owner
    monkeypatch.setattr(shares, "_CREATE_RATE_LIMITER", RateLimiter(max_calls=2, window_seconds=60))

    for _ in range(2):
        target = f"{uuid.uuid4().hex[:8]}@example.test"
        await shares._create("server", server_id, ShareIn(email=target), principal)

    with pytest.raises(HTTPException) as caught:
        target = f"{uuid.uuid4().hex[:8]}@example.test"
        await shares._create("server", server_id, ShareIn(email=target), principal)

    assert caught.value.status_code == 429
    assert "Retry-After" in caught.value.headers


async def test_a_caller_with_no_admin_access_never_reaches_the_limiter(owner, monkeypatch) -> None:
    """Being refused for lack of access must not consume the shared rate
    budget — only callers who can actually resolve an email get metered."""
    principal, server_id = owner
    limiter = RateLimiter(max_calls=1, window_seconds=60)
    monkeypatch.setattr(shares, "_CREATE_RATE_LIMITER", limiter)

    suffix = uuid.uuid4().hex[:8]
    stranger_email = f"stranger-{suffix}@example.test"
    stranger_id = await _make_user(stranger_email)
    stranger_claims = {
        "sub": stranger_id, "email": stranger_email,
        "role": "authenticated", "aud": "authenticated",
    }
    stranger = Principal(user_id=stranger_id, email=stranger_email, claims=stranger_claims)

    try:
        with pytest.raises(HTTPException) as caught:
            target = f"{uuid.uuid4().hex[:8]}@example.test"
            await shares._create("server", server_id, ShareIn(email=target), stranger)
        # 404, not 403: row-level security hides a server this account cannot
        # see, so there is nothing to be forbidden from. That is the stronger
        # answer of the two — 403 would confirm the server exists to someone
        # who is not allowed to know that. 403 is for a caller who can see it
        # and is not its owner.
        assert caught.value.status_code == 404

        # The budget is still untouched: the legitimate owner can still invite.
        target = f"{uuid.uuid4().hex[:8]}@example.test"
        await shares._create("server", server_id, ShareIn(email=target), principal)
    finally:
        async with service_session() as conn:
            await conn.execute(
                text("delete from auth.users where id = cast(:id as uuid)"), {"id": stranger_id}
            )
