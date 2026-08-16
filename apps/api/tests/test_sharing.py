"""Sharing servers and projects with individual people.

The interesting cases are all about the seams between the two ways access can
arrive — organization membership and an individual grant — and about what
lending someone a machine does *not* give them.

Runs against a local `supabase start`. Skipped when the database is unreachable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from moonphase import queries
from moonphase.db import service_session, user_session


async def _database_reachable() -> bool:
    try:
        async with service_session() as conn:
            await conn.execute(text("select 1"))
        return True
    except Exception:
        return False


def claims_for(user_id: str, email: str) -> dict[str, object]:
    return {
        "sub": user_id,
        "email": email,
        "role": "authenticated",
        "aud": "authenticated",
    }


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


class Person:
    def __init__(self, user_id: str, email: str) -> None:
        self.id = user_id
        self.email = email
        self.claims = claims_for(user_id, email)


@pytest.fixture
async def cast_of_three():
    """Alice owns things, Bob is shared with, Carol is a stranger."""
    if not await _database_reachable():
        pytest.skip("Postgres is not reachable — run `supabase start`")
    suffix = uuid.uuid4().hex[:8]
    people = [
        Person(await _make_user(f"{name}-{suffix}@example.test"), f"{name}-{suffix}@example.test")
        for name in ("alice", "bob", "carol")
    ]
    yield tuple(people)
    async with service_session() as conn:
        await conn.execute(
            text("delete from auth.users where id = any(cast(:ids as uuid[]))"),
            {"ids": [p.id for p in people]},
        )


async def _server_for(person: Person, name: str = "box") -> dict:
    async with user_session(person.claims) as conn:
        org_id = await queries.personal_org_id(conn)
        server = await queries.insert_server(
            conn,
            org_id=org_id,
            name=f"{name}-{uuid.uuid4().hex[:6]}",
            host="10.0.0.9",
            port=22,
            ssh_user="deploy",
            auth_mode="managed_key",
            created_by=person.id,
        )
        await queries.update_server_state(conn, server["id"], status="online")
    return server


async def _project_for(
    person: Person, server: dict, *, org_id=None, name: str = "app"
) -> dict:
    slug = f"{name}-{uuid.uuid4().hex[:6]}"
    async with user_session(person.claims) as conn:
        if org_id is None:
            org_id = await queries.personal_org_id(conn)
        return await queries.insert_project(
            conn,
            org_id=org_id,
            server_id=server["id"],
            name=name,
            slug=slug,
            harness="claude_code",
            environment="debian",
            repo_url=None,
            container_name=f"mp-{slug}",
            workspace_volume=f"mp-{slug}-w",
            home_volume=f"mp-{slug}-h",
            preview_port=None,
            created_by=person.id,
        )


async def _share(owner: Person, kind: str, resource_id, to: str, role: str) -> dict:
    async with user_session(owner.claims) as conn:
        async with service_session() as svc:
            user = await queries.find_user_by_email_privileged(svc, to)
        return await queries.upsert_share(
            conn,
            kind,
            resource_id,
            email=to,
            user_id=str(user["id"]) if user else None,
            role=role,
            created_by=owner.id,
        )


# --- the baseline it has to preserve ------------------------------------------


async def test_a_stranger_still_sees_nothing(cast_of_three) -> None:
    alice, _, carol = cast_of_three
    server = await _server_for(alice)
    await _project_for(alice, server)

    async with user_session(carol.claims) as conn:
        assert await queries.list_servers(conn) == []
        assert await queries.list_projects(conn) == []
        assert await queries.access_level(conn, "server", server["id"]) is None


# --- sharing a server ----------------------------------------------------------


async def test_sharing_a_server_grants_use_but_never_administration(
    cast_of_three,
) -> None:
    alice, bob, _ = cast_of_three
    server = await _server_for(alice)

    async with user_session(bob.claims) as conn:
        assert await queries.list_servers(conn) == []

    await _share(alice, "server", server["id"], bob.email, "collaborator")

    async with user_session(bob.claims) as conn:
        visible = await queries.list_servers(conn)
        assert [s["id"] for s in visible] == [server["id"]]
        assert visible[0]["shared"] is True
        assert visible[0]["access"] == "write"
        # Being lent a machine is not being given it.
        assert await queries.delete_server(conn, server["id"]) is False

    async with user_session(alice.claims) as conn:
        mine = await queries.list_servers(conn)
        assert mine[0]["shared"] is False
        assert mine[0]["access"] == "admin"
        assert mine[0]["share_count"] == 1


async def test_a_server_viewer_cannot_create_projects_on_it(cast_of_three) -> None:
    alice, bob, _ = cast_of_three
    server = await _server_for(alice)
    await _share(alice, "server", server["id"], bob.email, "viewer")

    async with user_session(bob.claims) as conn:
        assert await queries.access_level(conn, "server", server["id"]) == "read"
        with pytest.raises(ProgrammingError):
            await _project_for(bob, server)


async def test_a_project_on_a_lent_machine_belongs_to_the_person_who_made_it(
    cast_of_three,
) -> None:
    alice, bob, carol = cast_of_three
    server = await _server_for(alice)
    await _share(alice, "server", server["id"], bob.email, "collaborator")

    async with user_session(bob.claims) as conn:
        bob_org = await queries.personal_org_id(conn)
    project = await _project_for(bob, server, org_id=bob_org)

    async with user_session(bob.claims) as conn:
        assert await queries.access_level(conn, "project", project["id"]) == "admin"
        listed = await queries.list_projects(conn)
        assert [p["id"] for p in listed] == [project["id"]]
        # Bob can see which machine it is on by name, and nothing else about it.
        assert listed[0]["server_name"] == server["name"]

    # Alice owns the hardware: she can see that something is running on it and
    # can reclaim it, but not read it.
    async with user_session(alice.claims) as conn:
        assert await queries.access_level(conn, "project", project["id"]) == "host"
        hers = await queries.list_projects(conn)
        assert [p["id"] for p in hers] == [project["id"]]
        assert hers[0]["shared"] is True

    async with user_session(carol.claims) as conn:
        assert await queries.list_projects(conn) == []


async def test_the_host_can_see_that_it_exists_but_not_what_it_is_doing(
    cast_of_three,
) -> None:
    alice, bob, _ = cast_of_three
    server = await _server_for(alice)
    await _share(alice, "server", server["id"], bob.email, "collaborator")
    async with user_session(bob.claims) as conn:
        bob_org = await queries.personal_org_id(conn)
    project = await _project_for(bob, server, org_id=bob_org)

    async with user_session(bob.claims) as conn:
        await queries.upsert_session(
            conn,
            project_id=project["id"],
            harness="claude_code",
            tmux_session="moonphase",
            state="running",
        )
        assert len(await queries.get_sessions(conn, project["id"])) == 1

    # The transcript, the activity state and the terminal all hang off sessions.
    # Not seeing them is what separates 'host' from 'read'.
    async with user_session(alice.claims) as conn:
        assert await queries.get_sessions(conn, project["id"]) == []

    # But she can take her machine back.
    async with user_session(alice.claims) as conn:
        assert await queries.delete_project(conn, project["id"]) is True


# --- sharing a project ---------------------------------------------------------


async def test_a_project_viewer_can_watch_and_nothing_else(cast_of_three) -> None:
    alice, bob, _ = cast_of_three
    server = await _server_for(alice)
    project = await _project_for(alice, server)
    await _share(alice, "project", project["id"], bob.email, "viewer")

    async with user_session(bob.claims) as conn:
        assert await queries.access_level(conn, "project", project["id"]) == "read"
        listed = await queries.list_projects(conn)
        assert [p["id"] for p in listed] == [project["id"]]
        assert listed[0]["shared"] is True

        # The server it runs on stays private; only its name comes through.
        assert await queries.list_servers(conn) == []
        assert listed[0]["server_name"] == server["name"]

        # No lifecycle, no sessions, no deleting.
        await queries.update_project_state(conn, project["id"], status="stopped")
        assert await queries.delete_project(conn, project["id"]) is False
        with pytest.raises(ProgrammingError):
            await queries.upsert_session(
                conn,
                project_id=project["id"],
                harness="claude_code",
                tmux_session="moonphase",
                state="running",
            )

    async with user_session(alice.claims) as conn:
        unchanged = await queries.get_project(conn, project["id"])
    assert unchanged is not None
    assert unchanged["status"] == "creating", "a viewer must not change state"


async def test_a_project_collaborator_can_drive_it(cast_of_three) -> None:
    alice, bob, _ = cast_of_three
    server = await _server_for(alice)
    project = await _project_for(alice, server)
    await _share(alice, "project", project["id"], bob.email, "collaborator")

    async with user_session(bob.claims) as conn:
        assert await queries.access_level(conn, "project", project["id"]) == "write"
        await queries.upsert_session(
            conn,
            project_id=project["id"],
            harness="claude_code",
            tmux_session="moonphase",
            state="running",
        )
        await queries.update_project_state(conn, project["id"], status="running")
        # Driving is not owning.
        assert await queries.delete_project(conn, project["id"]) is False

    async with user_session(alice.claims) as conn:
        row = await queries.get_project(conn, project["id"])
    assert row is not None and row["status"] == "running"


async def test_a_share_recipient_cannot_re_share(cast_of_three) -> None:
    alice, bob, carol = cast_of_three
    server = await _server_for(alice)
    project = await _project_for(alice, server)
    await _share(alice, "project", project["id"], bob.email, "collaborator")

    with pytest.raises(ProgrammingError):
        await _share(bob, "project", project["id"], carol.email, "viewer")

    async with user_session(carol.claims) as conn:
        assert await queries.list_projects(conn) == []


async def test_revoking_takes_the_access_away(cast_of_three) -> None:
    alice, bob, _ = cast_of_three
    server = await _server_for(alice)
    project = await _project_for(alice, server)
    share = await _share(alice, "project", project["id"], bob.email, "collaborator")

    async with user_session(alice.claims) as conn:
        assert await queries.delete_share(conn, "project", project["id"], share["id"])

    async with user_session(bob.claims) as conn:
        assert await queries.list_projects(conn) == []
        assert await queries.access_level(conn, "project", project["id"]) is None


async def test_a_recipient_can_walk_away_from_a_share(cast_of_three) -> None:
    alice, bob, _ = cast_of_three
    server = await _server_for(alice)
    await _share(alice, "server", server["id"], bob.email, "viewer")

    async with user_session(bob.claims) as conn:
        # He sees his own row and can drop it, without being an admin.
        mine = await queries.list_shares(conn, "server", server["id"])
        assert [s["email"] for s in mine] == [bob.email]
        assert await queries.delete_share(conn, "server", server["id"], mine[0]["id"])
        assert await queries.list_servers(conn) == []


async def test_a_viewer_does_not_see_the_other_recipients(cast_of_three) -> None:
    alice, bob, carol = cast_of_three
    server = await _server_for(alice)
    await _share(alice, "server", server["id"], bob.email, "viewer")
    await _share(alice, "server", server["id"], carol.email, "viewer")

    async with user_session(alice.claims) as conn:
        assert len(await queries.list_shares(conn, "server", server["id"])) == 2
    async with user_session(bob.claims) as conn:
        assert [s["email"] for s in await queries.list_shares(conn, "server", server["id"])] == [
            bob.email
        ]


async def test_changing_the_role_takes_effect(cast_of_three) -> None:
    alice, bob, _ = cast_of_three
    server = await _server_for(alice)
    project = await _project_for(alice, server)
    share = await _share(alice, "project", project["id"], bob.email, "viewer")

    async with user_session(bob.claims) as conn:
        assert await queries.access_level(conn, "project", project["id"]) == "read"

    async with user_session(alice.claims) as conn:
        await queries.update_share_role(
            conn, "project", project["id"], share["id"], "collaborator"
        )

    async with user_session(bob.claims) as conn:
        assert await queries.access_level(conn, "project", project["id"]) == "write"

    # Re-sharing the same address is a role change, not a duplicate row.
    await _share(alice, "project", project["id"], bob.email, "viewer")
    async with user_session(alice.claims) as conn:
        rows = await queries.list_shares(conn, "project", project["id"])
    assert len(rows) == 1 and rows[0]["role"] == "viewer"


# --- sharing with someone who has not signed up yet ---------------------------


async def test_a_share_made_before_signup_is_claimed_on_signup(cast_of_three) -> None:
    alice, _, _ = cast_of_three
    server = await _server_for(alice)
    invitee = f"newcomer-{uuid.uuid4().hex[:8]}@example.test"

    row = await _share(alice, "server", server["id"], invitee, "collaborator")
    assert row["user_id"] is None, "nobody to attach it to yet"

    newcomer_id = await _make_user(invitee)
    try:
        newcomer = Person(newcomer_id, invitee)
        async with user_session(newcomer.claims) as conn:
            assert await queries.access_level(conn, "server", server["id"]) == "write"
            assert [s["id"] for s in await queries.list_servers(conn)] == [server["id"]]
    finally:
        async with service_session() as conn:
            await conn.execute(
                text("delete from auth.users where id = cast(:i as uuid)"),
                {"i": newcomer_id},
            )


async def test_the_claim_is_case_insensitive(cast_of_three) -> None:
    alice, _, _ = cast_of_three
    server = await _server_for(alice)
    address = f"Mixed-{uuid.uuid4().hex[:8]}@Example.Test"

    # The API lowercases, but a share row written any other way must still find
    # its person — an address is not case sensitive and users will type both.
    await _share(alice, "server", server["id"], address.lower(), "viewer")
    newcomer_id = await _make_user(address)
    try:
        async with user_session(claims_for(newcomer_id, address)) as conn:
            assert await queries.access_level(conn, "server", server["id"]) == "read"
    finally:
        async with service_session() as conn:
            await conn.execute(
                text("delete from auth.users where id = cast(:i as uuid)"),
                {"i": newcomer_id},
            )


# --- the hole the sharing work closed -----------------------------------------


async def test_a_project_cannot_be_attached_to_an_invisible_server(
    cast_of_three,
) -> None:
    """Foreign keys are not subject to RLS.

    Before sharing existed, the insert policy checked only the organization the
    project was going into. Nothing stopped a caller putting someone else's
    server_id on a row in their own org — and the API would then have connected
    to that machine with its owner's credentials. Only the route stood in the
    way. Now the database does too.
    """
    alice, _, carol = cast_of_three
    server = await _server_for(alice)

    with pytest.raises(ProgrammingError) as excinfo:
        await _project_for(carol, server)
    assert "policy" in str(excinfo.value).lower() or "denied" in str(excinfo.value).lower()

    async with user_session(alice.claims) as conn:
        assert await queries.list_projects(conn) == []
