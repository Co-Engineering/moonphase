"""Tenancy tests.

These exercise the promise the schema makes: a caller sees their own
organizations and nothing else, and secrets in the `private` schema are
unreachable from the `authenticated` role no matter what a route handler does.

Runs against a local `supabase start`. Skipped when the database is unreachable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text  # noqa: E402

from moonphase import queries  # noqa: E402
from moonphase.db import service_session, user_session  # noqa: E402


async def _database_reachable() -> bool:
    try:
        async with service_session() as conn:
            await conn.execute(text("select 1"))
        return True
    except Exception:
        return False


def claims_for(user_id: str, email: str) -> dict[str, object]:
    """The subset of a GoTrue access token the policies actually read."""
    return {
        "sub": user_id,
        "email": email,
        "role": "authenticated",
        "aud": "authenticated",
    }


async def _make_user(email: str) -> str:
    """Insert directly into auth.users so the signup trigger fires."""
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
async def two_users():
    if not await _database_reachable():
        pytest.skip("Postgres is not reachable — run `supabase start`")
    suffix = uuid.uuid4().hex[:8]
    alice = await _make_user(f"alice-{suffix}@example.test")
    bob = await _make_user(f"bob-{suffix}@example.test")
    yield alice, bob
    async with service_session() as conn:
        await conn.execute(
            text("delete from auth.users where id = any(cast(:ids as uuid[]))"),
            {"ids": [alice, bob]},
        )


async def test_signup_creates_personal_org(two_users) -> None:
    alice, _ = two_users
    async with user_session(claims_for(alice, "alice@example.test")) as conn:
        orgs = await queries.list_organizations(conn)
    assert len(orgs) == 1, f"expected exactly one personal org, got {orgs}"
    assert orgs[0]["is_personal"] is True
    assert orgs[0]["role"] == "owner"


async def test_servers_are_invisible_across_tenants(two_users) -> None:
    alice, bob = two_users
    alice_claims = claims_for(alice, "alice@example.test")
    bob_claims = claims_for(bob, "bob@example.test")

    async with user_session(alice_claims) as conn:
        org_id = await queries.personal_org_id(conn)
        assert org_id is not None
        server = await queries.insert_server(
            conn,
            org_id=org_id,
            name="alice-box",
            host="10.0.0.1",
            port=22,
            ssh_user="deploy",
            auth_mode="managed_key",
            created_by=alice,
        )

    async with user_session(alice_claims) as conn:
        assert len(await queries.list_servers(conn)) == 1

    # The whole point: Bob must not see it, and must not be able to fetch it
    # by id even though he knows the id.
    async with user_session(bob_claims) as conn:
        assert await queries.list_servers(conn) == []
        assert await queries.get_server(conn, server["id"]) is None


async def test_cannot_create_server_in_someone_elses_org(two_users) -> None:
    alice, bob = two_users
    async with user_session(claims_for(alice, "a@example.test")) as conn:
        alice_org = await queries.personal_org_id(conn)

    async with user_session(claims_for(bob, "b@example.test")) as conn:
        # resolve_org is the guard the routes rely on.
        with pytest.raises(PermissionError):
            await queries.resolve_org(conn, alice_org)


async def test_private_schema_is_unreachable_from_authenticated(two_users) -> None:
    alice, _ = two_users
    async with user_session(claims_for(alice, "a@example.test")) as conn:
        with pytest.raises(Exception) as excinfo:
            await conn.execute(text("select * from private.server_credentials"))
    message = str(excinfo.value).lower()
    assert "permission denied" in message or "does not exist" in message, message


async def test_service_session_bypasses_rls(two_users) -> None:
    alice, _ = two_users
    async with user_session(claims_for(alice, "a@example.test")) as conn:
        org_id = await queries.personal_org_id(conn)
        server = await queries.insert_server(
            conn,
            org_id=org_id,
            name="cred-box",
            host="10.0.0.2",
            port=22,
            ssh_user="deploy",
            auth_mode="managed_key",
            created_by=alice,
        )

    # Credentials round-trip through encryption and are only readable here.
    async with service_session() as conn:
        await queries.store_server_credentials_privileged(
            conn, server["id"], private_key="PRIVATE-KEY-MATERIAL", password="hunter2"
        )
        target = await queries.load_ssh_target_privileged(conn, server["id"])
    assert target is not None
    assert target.private_key == "PRIVATE-KEY-MATERIAL"
    assert target.password == "hunter2"

    # And the stored bytes are genuinely ciphertext, not just base64 of itself.
    async with service_session() as conn:
        row = await conn.execute(
            text("select private_key_enc from private.server_credentials where server_id = :i"),
            {"i": server["id"]},
        )
        blob = bytes(row.scalar_one())
    assert b"PRIVATE-KEY-MATERIAL" not in blob

    async with service_session() as conn:
        await queries.discard_server_password_privileged(conn, server["id"])
        target = await queries.load_ssh_target_privileged(conn, server["id"])
    assert target is not None
    assert target.password is None
    assert target.private_key == "PRIVATE-KEY-MATERIAL"
