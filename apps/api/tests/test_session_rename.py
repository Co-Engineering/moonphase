"""Renaming a session changes its label, not what it is.

`tmux_session` names the actual tmux session and derives the session's home
directory, git worktree and branch (see
20260817160000_individual_sessions.sql) — renaming it would mean moving all
three inside a running container. `display_name` is the thing that actually
changes: a label, decoupled from the identifier, the same way project and
server renaming already work.

Runs against a local `supabase start`. Skipped when the database is
unreachable, same as test_sharing.py. No SSH/Docker needed: unlike closing a
session, renaming one never touches the server.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from moonphase import queries
from moonphase.auth import Principal
from moonphase.db import service_session, user_session
from moonphase.routers import projects
from moonphase.runtime import Forbidden
from moonphase.schemas import RenameIn


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


def _claims(user_id: str, email: str) -> dict:
    return {"sub": user_id, "email": email, "role": "authenticated", "aud": "authenticated"}


@pytest.fixture
async def owner_with_session():
    if not await _database_reachable():
        pytest.skip("Postgres is not reachable — run `supabase start`")
    suffix = uuid.uuid4().hex[:8]
    email = f"owner-{suffix}@example.test"
    user_id = await _make_user(email)
    claims = _claims(user_id, email)

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
        project = await queries.insert_project(
            conn,
            org_id=org_id,
            server_id=server["id"],
            name=f"proj-{suffix}",
            slug=f"proj-{suffix}",
            harness="claude_code",
            environment="debian",
            repo_url=None,
            container_name=f"mp-{suffix}",
            workspace_volume=f"mp-{suffix}-w",
            home_volume=f"mp-{suffix}-h",
            preview_port=None,
            created_by=user_id,
        )
        session = await queries.upsert_session(
            conn,
            project_id=project["id"],
            harness="claude_code",
            tmux_session="alice",
            state="running",
            user_id=user_id,
            workdir="/workspace-alice",
            home_dir="/home/dev/sessions/alice",
            branch="moonphase/alice",
        )

    principal = Principal(user_id=user_id, email=email, claims=claims)
    yield principal, project["id"], session["tmux_session"]

    async with service_session() as conn:
        await conn.execute(
            text("delete from auth.users where id = cast(:id as uuid)"), {"id": user_id}
        )


async def test_renaming_sets_the_display_name_and_nothing_else(owner_with_session) -> None:
    principal, project_id, tmux_session = owner_with_session

    out = await projects.rename_session(
        project_id, tmux_session, RenameIn(name="My cool session"), principal=principal
    )

    assert out.display_name == "My cool session"
    assert out.tmux_session == tmux_session
    assert out.workdir == "/workspace-alice"
    assert out.branch == "moonphase/alice"


async def test_restarting_the_session_does_not_clear_the_display_name(
    owner_with_session,
) -> None:
    """upsert_session runs on every session start (see routers/projects.py) —
    it must not silently wipe a name someone set."""
    principal, project_id, tmux_session = owner_with_session
    await projects.rename_session(
        project_id, tmux_session, RenameIn(name="Keep me"), principal=principal
    )

    async with user_session(principal.claims) as conn:
        row = await queries.upsert_session(
            conn,
            project_id=project_id,
            harness="claude_code",
            tmux_session=tmux_session,
            state="running",
            user_id=principal.user_id,
            workdir="/workspace-alice",
            home_dir="/home/dev/sessions/alice",
            branch="moonphase/alice",
            mark_started=True,
        )

    assert row["display_name"] == "Keep me"


async def test_someone_elses_collaborator_access_is_not_enough_to_rename_it(
    owner_with_session,
) -> None:
    """Watching, and even driving your own session in the same project, is
    fine. Renaming somebody else's is not yours to do."""
    principal, project_id, tmux_session = owner_with_session
    suffix = uuid.uuid4().hex[:8]
    stranger_email = f"collaborator-{suffix}@example.test"
    stranger_id = await _make_user(stranger_email)
    stranger = Principal(
        user_id=stranger_id, email=stranger_email, claims=_claims(stranger_id, stranger_email)
    )

    async with user_session(principal.claims) as conn:
        await queries.upsert_share(
            conn,
            "project",
            project_id,
            email=stranger_email,
            user_id=stranger_id,
            role="collaborator",
            created_by=principal.user_id,
        )

    try:
        with pytest.raises(Forbidden):
            await projects.rename_session(
                project_id, tmux_session, RenameIn(name="Not yours"), principal=stranger
            )
    finally:
        async with service_session() as conn:
            await conn.execute(
                text("delete from auth.users where id = cast(:id as uuid)"), {"id": stranger_id}
            )


async def test_an_empty_name_is_refused(owner_with_session) -> None:
    principal, project_id, tmux_session = owner_with_session
    with pytest.raises(HTTPException) as caught:
        await projects.rename_session(
            project_id, tmux_session, RenameIn(name="   "), principal=principal
        )
    assert caught.value.status_code == 400


async def test_a_name_over_64_characters_is_refused(owner_with_session) -> None:
    principal, project_id, tmux_session = owner_with_session
    with pytest.raises(HTTPException) as caught:
        await projects.rename_session(
            project_id, tmux_session, RenameIn(name="x" * 65), principal=principal
        )
    assert caught.value.status_code == 400
