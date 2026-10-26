"""Multiple harnesses in the same project.

A project has a default harness (e.g. Claude Code), but individual sessions
inside the same project can run different harnesses (e.g. Antigravity) side by side.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from moonphase.harness import SessionSpace
from moonphase.profile import WorkspaceProfile
from moonphase.routers import projects
from moonphase.schemas import SessionCreateIn, SessionStartIn

PRINCIPAL = SimpleNamespace(user_id=uuid4(), claims={}, email="dev@example.com")


@asynccontextmanager
async def _no_db(*a):
    yield None


@pytest.fixture
def mock_project_env(monkeypatch):
    state = SimpleNamespace(
        project_harness="claude_code",
        ensured_sessions=[],
        upserted_sessions=[],
        checked_accounts=[],
    )

    async def load_project_context(claims, project_id, *, require=None):
        return SimpleNamespace(
            project={"id": project_id, "org_id": "org", "status": "running"},
            target="target-ssh",
            container="container-1",
            harness=state.project_harness,
        )

    async def _start_container(principal, ctx):
        return object()

    async def existing_branch_names(conn, container):
        return set()

    async def _own_account(principal, account_id, harness):
        state.checked_accounts.append((account_id, harness))

    async def _profile_or_409(principal, ctx, session=None, account_id=None, harness_kind=None):
        return WorkspaceProfile(org_id=uuid4())

    async def _prepare_space(principal, ctx, name, profile, **kw):
        space = SessionSpace(home=f"/home/dev/sessions/{name}", workdir="/workspace")
        worktree = SimpleNamespace(workdir="/workspace", branch="main", note=None)
        return space, worktree

    async def ensure_session(
        conn, container, harness_kind, workspace_profile, session, space, **kw
    ):
        state.ensured_sessions.append((session, harness_kind, kw))

    async def upsert_session(conn, **kw):
        state.upserted_sessions.append(kw)
        return {
            "id": uuid4(),
            "project_id": kw["project_id"],
            "tmux_session": kw["tmux_session"],
            "display_name": None,
            "harness": kw["harness"],
            "state": kw["state"],
            "started_at": None,
            "last_attached_at": None,
            "user_id": kw["user_id"],
            "owner": "dev",
            "is_mine": True,
            "workdir": kw["workdir"],
            "home_dir": kw["home_dir"],
            "branch": kw["branch"],
            "transcript_path": kw["transcript_path"],
            "activity": "unknown",
            "activity_detail": None,
            "activity_at": None,
            "checked_at": None,
            "attached_clients": 0,
            "alive": True,
            "start_note": None,
        }

    async def set_session_harness_account(conn, project_id, name, account_id):
        pass

    monkeypatch.setattr(projects.runtime, "load_project_context", load_project_context)
    monkeypatch.setattr(projects, "_start_container", _start_container)
    monkeypatch.setattr(projects.workspaces, "existing_branch_names", existing_branch_names)
    monkeypatch.setattr(projects, "_own_account", _own_account)
    monkeypatch.setattr(projects, "_profile_or_409", _profile_or_409)
    monkeypatch.setattr(projects, "_prepare_space", _prepare_space)
    monkeypatch.setattr(projects.sessions, "ensure_session", ensure_session)
    monkeypatch.setattr(projects.queries, "upsert_session", upsert_session)
    monkeypatch.setattr(
        projects.queries, "set_session_harness_account", set_session_harness_account
    )
    monkeypatch.setattr(projects, "user_session", _no_db)
    monkeypatch.setattr(projects, "service_session", _no_db)

    return state


@pytest.mark.asyncio
async def test_create_session_defaults_to_project_harness(mock_project_env, monkeypatch) -> None:
    async def get_sessions(conn, project_id):
        return []

    monkeypatch.setattr(projects.queries, "get_sessions", get_sessions)

    project_id = uuid4()
    out = await projects.create_session(
        project_id,
        SessionCreateIn(name="test-session"),
        PRINCIPAL,
    )

    assert out.harness == "claude_code"
    assert len(mock_project_env.ensured_sessions) == 1
    session_name, harness_kind, _ = mock_project_env.ensured_sessions[0]
    assert session_name == "test-session"
    assert harness_kind == "claude_code"
    assert mock_project_env.upserted_sessions[0]["harness"] == "claude_code"


@pytest.mark.asyncio
async def test_create_session_with_different_harness_in_same_project(
    mock_project_env, monkeypatch
) -> None:
    async def get_sessions(conn, project_id):
        return []

    monkeypatch.setattr(projects.queries, "get_sessions", get_sessions)

    project_id = uuid4()
    account_id = uuid4()
    out = await projects.create_session(
        project_id,
        SessionCreateIn(name="agy-session", harness="antigravity", account_id=account_id),
        PRINCIPAL,
    )

    assert out.harness == "antigravity"
    assert len(mock_project_env.ensured_sessions) == 1
    session_name, harness_kind, _ = mock_project_env.ensured_sessions[0]
    assert session_name == "agy-session"
    assert harness_kind == "antigravity"
    assert mock_project_env.upserted_sessions[0]["harness"] == "antigravity"
    # Account verified against the session's harness, not the project's
    assert (account_id, "antigravity") in mock_project_env.checked_accounts


@pytest.mark.asyncio
async def test_start_session_preserves_stored_harness(mock_project_env, monkeypatch) -> None:
    existing_row = {
        "tmux_session": "stored-agy",
        "harness": "antigravity",
        "is_mine": True,
        "home_dir": "/home/dev/sessions/stored-agy",
        "workdir": "/workspace",
        "branch": "feature-x",
    }

    async def get_sessions(conn, project_id):
        return [existing_row]

    monkeypatch.setattr(projects.queries, "get_sessions", get_sessions)

    project_id = uuid4()
    out = await projects.start_session(
        project_id,
        SessionStartIn(session="stored-agy", restart=False),
        PRINCIPAL,
    )

    assert out.harness == "antigravity"
    session_name, harness_kind, _ = mock_project_env.ensured_sessions[0]
    assert session_name == "stored-agy"
    assert harness_kind == "antigravity"
    assert mock_project_env.upserted_sessions[0]["harness"] == "antigravity"


@pytest.mark.asyncio
async def test_an_existing_session_cannot_be_restarted_as_another_harness(
    mock_project_env, monkeypatch
) -> None:
    """Its HOME, transcripts and account are its harness's; another harness
    started over them would find none of its own."""
    from fastapi import HTTPException

    async def get_sessions(conn, project_id):
        return [
            {
                "tmux_session": "stored-agy",
                "harness": "antigravity",
                "is_mine": True,
                "home_dir": "/home/dev/sessions/stored-agy",
                "workdir": "/workspace",
                "branch": "feature-x",
            }
        ]

    monkeypatch.setattr(projects.queries, "get_sessions", get_sessions)

    with pytest.raises(HTTPException) as refused:
        await projects.start_session(
            uuid4(),
            SessionStartIn(session="stored-agy", restart=True, harness="claude_code"),
            PRINCIPAL,
        )

    assert refused.value.status_code == 409
    assert mock_project_env.ensured_sessions == []


@pytest.mark.asyncio
async def test_duplicate_session_preserves_source_harness(mock_project_env, monkeypatch) -> None:
    source_row = {
        "tmux_session": "source-session",
        "harness": "antigravity",
        "is_mine": True,
        "home_dir": "/home/dev/sessions/source-session",
        "workdir": "/workspace",
        "branch": "main",
        "harness_credential_id": None,
    }

    async def get_session(conn, project_id, name):
        return source_row

    async def get_sessions(conn, project_id):
        return [source_row]

    async def ensure_home(conn, container, space):
        pass

    async def copy_path(conn, container, src, dst):
        pass

    async def snapshot_worktree(conn, container, workdir, **kw):
        return "commit-sha"

    async def get_session_config(conn, project_id, name):
        return {}

    async def update_session_config(conn, **kw):
        pass

    monkeypatch.setattr(projects.queries, "get_session", get_session)
    monkeypatch.setattr(projects.queries, "get_sessions", get_sessions)
    monkeypatch.setattr(projects.queries, "get_session_config", get_session_config)
    monkeypatch.setattr(projects.queries, "update_session_config", update_session_config)
    monkeypatch.setattr(projects.sessions, "ensure_home", ensure_home)
    monkeypatch.setattr(projects.docker_remote, "copy_path", copy_path)
    monkeypatch.setattr(projects.workspaces, "snapshot_worktree", snapshot_worktree)

    project_id = uuid4()
    out = await projects.duplicate_session(project_id, "source-session", PRINCIPAL)

    assert out.harness == "antigravity"
    _, harness_kind, kw = mock_project_env.ensured_sessions[0]
    assert harness_kind == "antigravity"
    assert kw.get("resume") is True
    assert mock_project_env.upserted_sessions[0]["harness"] == "antigravity"
