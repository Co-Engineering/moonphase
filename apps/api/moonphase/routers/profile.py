"""Global workspace profile: settings, harness sign-in, GitHub.

Everything here is organization-scoped and applies to every project. That is
the point — signing in or configuring a setting is something you should do
once, not once per server and again per project.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from .. import github, login, queries, runtime, ssh
from ..auth import Principal, current_principal
from ..config import get_settings
from ..db import service_session, user_session
from ..harness import get as get_harness
from ..runtime import CAN_CONTROL, NotFound
from ..schemas import (
    GitHubDeviceOut,
    GitHubDeviceStart,
    GitHubTokenIn,
    HarnessApiKeyIn,
    HarnessLoginCode,
    HarnessLoginOut,
    HarnessLoginStart,
    WorkspaceProfileIn,
    WorkspaceProfileOut,
)
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/profile", tags=["profile"])


async def _resolve_org(principal: Principal, org_id: UUID | None) -> UUID:
    async with user_session(principal.claims) as conn:
        try:
            return await queries.resolve_org(conn, org_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _build_out(principal: Principal, org_id: UUID) -> WorkspaceProfileOut:
    async with user_session(principal.claims) as conn:
        row = await queries.get_profile(conn, org_id)
    if row is None:
        row = {
            "org_id": org_id,
            "claude_settings_json": None,
            "claude_md": None,
            "mcp_json": None,
            "env_vars": {},
            "git_user_name": None,
            "git_user_email": None,
        }

    async with service_session() as conn:
        harness_row = await queries.resolve_harness_credential_privileged(
            conn, org_id=org_id, project_id=org_id, harness="claude_code"
        )
        vcs_row = await queries.get_vcs_credential_privileged(conn, org_id, "github")

    env = row.get("env_vars") or {}
    if isinstance(env, str):
        import json

        try:
            env = json.loads(env)
        except json.JSONDecodeError:
            env = {}

    return WorkspaceProfileOut(
        org_id=org_id,
        claude_settings_json=row.get("claude_settings_json"),
        claude_md=row.get("claude_md"),
        mcp_json=row.get("mcp_json"),
        env_vars={str(k): str(v) for k, v in dict(env).items()},
        git_user_name=row.get("git_user_name"),
        git_user_email=row.get("git_user_email"),
        harness_connected=harness_row is not None,
        harness_auth_mode=harness_row.get("auth_mode") if harness_row else None,
        github_connected=vcs_row is not None,
        github_account=vcs_row.get("account") if vcs_row else None,
        github_scopes=vcs_row.get("scopes") if vcs_row else None,
    )


@router.get("", response_model=WorkspaceProfileOut)
async def get_profile(
    org_id: UUID | None = None, principal: Principal = Depends(current_principal)
) -> WorkspaceProfileOut:
    return await _build_out(principal, await _resolve_org(principal, org_id))


@router.put("", response_model=WorkspaceProfileOut)
async def update_profile(
    payload: WorkspaceProfileIn, principal: Principal = Depends(current_principal)
) -> WorkspaceProfileOut:
    """Save global settings.

    Takes effect on each project's next session restart. Existing containers
    are not touched here: rewriting config under a running agent mid-thought
    would be worse than a slightly stale setting.
    """
    org_id = await _resolve_org(principal, payload.org_id)
    async with user_session(principal.claims) as conn:
        try:
            await queries.upsert_profile(
                conn,
                org_id,
                claude_settings_json=payload.claude_settings_json,
                claude_md=payload.claude_md,
                mcp_json=payload.mcp_json,
                env_vars=payload.env_vars,
                git_user_name=payload.git_user_name,
                git_user_email=payload.git_user_email,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await _build_out(principal, org_id)


# ---------------------------------------------------------------------------
# Harness sign-in
# ---------------------------------------------------------------------------


async def _pick_login_server(
    principal: Principal, server_id: UUID | None
) -> dict[str, Any]:
    """Any online server can host the throwaway login container."""
    async with user_session(principal.claims) as conn:
        if server_id is not None:
            server = await queries.get_server(conn, server_id)
            if server is None:
                raise HTTPException(status_code=404, detail="Server not found.")
            if server["status"] != "online":
                raise HTTPException(
                    status_code=409, detail=f"Server is {server['status']}."
                )
            return server
        servers = await queries.list_servers(conn)

    online = [
        s for s in servers
        if s["status"] == "online" and s.get("access") in CAN_CONTROL
    ]
    if not online:
        raise HTTPException(
            status_code=409,
            detail=(
                "Signing in runs the harness in a throwaway container, so it needs "
                "one online server. Add a server first, or paste an API key instead."
            ),
        )
    return online[0]


def _login_out(session: login.LoginSession) -> HarnessLoginOut:
    # The pane is only useful once something is happening; sending it during
    # the URL step would just be noise.
    show_pane = session.state in {"verifying", "error"}
    return HarnessLoginOut(
        session_id=session.id,
        state=session.state,
        url=session.url,
        detail=session.detail,
        pane=session.pane if show_pane else None,
    )


async def _store_login_credential(
    principal: Principal, session: login.LoginSession
) -> None:
    """Persist whatever the flow produced, org-wide.

    Stored verbatim and unparsed: a token becomes an environment variable and a
    credentials file becomes a file, and neither needs Moonphase to understand
    its contents.
    """
    async with service_session() as conn:
        await queries.upsert_harness_credential_privileged(
            conn,
            org_id=UUID(session.org_id),
            project_id=None,
            harness=session.harness_kind,
            auth_mode="oauth",
            label="Signed in",
            api_key=None,
            oauth_token=session.oauth_token,
            oauth_blob=session.oauth_blob,
            created_by=principal.user_id,
        )


@router.post("/harness/login/start", response_model=HarnessLoginOut)
async def start_harness_login(
    payload: HarnessLoginStart, principal: Principal = Depends(current_principal)
) -> HarnessLoginOut:
    """Begin an interactive sign-in and return the URL to open."""
    org_id = await _resolve_org(principal, payload.org_id)
    server = await _pick_login_server(principal, payload.server_id)
    harness = get_harness(payload.harness)
    settings = get_settings()

    try:
        target = await runtime.load_server_target(
            principal.claims, server["id"], require=CAN_CONTROL
        )
        conn_ssh = await ssh.pool.get(target)
        session = await login.start(
            conn_ssh,
            org_id=str(org_id),
            server_id=str(server["id"]),
            harness=harness,
            image=settings.moonphase_runtime_image,
        )
    except (SSHError, NotFound) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _login_out(session)


@router.get("/harness/login/{session_id}", response_model=HarnessLoginOut)
async def poll_harness_login(
    session_id: str, principal: Principal = Depends(current_principal)
) -> HarnessLoginOut:
    """Advance a sign-in by one step and report where it got to.

    The OAuth exchange happens on the harness's own schedule, so progress is
    made here rather than in a long-lived request. Each poll does one bounded
    check and returns.
    """
    session = login.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such sign-in.")

    if session.state == "verifying":
        harness = get_harness(session.harness_kind)
        try:
            target = await runtime.load_server_target(
            principal.claims, UUID(session.server_id), require=CAN_CONTROL
        )
            conn_ssh = await ssh.pool.get(target)
            session = await login.advance(conn_ssh, session, harness)
        except (SSHError, NotFound) as exc:
            session.state = "error"
            session.detail = str(exc)

        if session.state == "complete":
            await _store_login_credential(principal, session)

    return _login_out(session)


@router.post("/harness/login/code", response_model=HarnessLoginOut)
async def submit_harness_code(
    payload: HarnessLoginCode, principal: Principal = Depends(current_principal)
) -> HarnessLoginOut:
    """Hand the pasted code to the waiting flow, then store what it produced."""
    session = login.get(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such sign-in.")

    try:
        target = await runtime.load_server_target(
            principal.claims, UUID(session.server_id), require=CAN_CONTROL
        )
        conn_ssh = await ssh.pool.get(target)
        # Types the code and returns at once; the client polls for the result.
        session = await login.submit_code(conn_ssh, session, payload.code)
    except (SSHError, NotFound) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _login_out(session)


@router.post("/harness/api-key", response_model=WorkspaceProfileOut)
async def set_harness_api_key(
    payload: HarnessApiKeyIn, principal: Principal = Depends(current_principal)
) -> WorkspaceProfileOut:
    """The non-interactive alternative to signing in."""
    org_id = await _resolve_org(principal, payload.org_id)
    async with service_session() as conn:
        await queries.upsert_harness_credential_privileged(
            conn,
            org_id=org_id,
            project_id=None,
            harness=payload.harness,
            auth_mode="api_key",
            label="API key",
            api_key=payload.api_key,
            oauth_blob=None,
            created_by=principal.user_id,
        )
    return await _build_out(principal, org_id)


@router.delete("/harness", response_model=WorkspaceProfileOut)
async def disconnect_harness(
    org_id: UUID | None = None,
    harness: str = "claude_code",
    principal: Principal = Depends(current_principal),
) -> WorkspaceProfileOut:
    resolved = await _resolve_org(principal, org_id)
    async with service_session() as conn:
        await queries.delete_harness_credential_privileged(conn, resolved, harness)
    return await _build_out(principal, resolved)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@router.post("/github/device/start", response_model=GitHubDeviceOut)
async def start_github_device(
    payload: GitHubDeviceStart, principal: Principal = Depends(current_principal)
) -> GitHubDeviceOut:
    settings = get_settings()
    if not settings.moonphase_github_client_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "No GitHub OAuth app is configured. Set MOONPHASE_GITHUB_CLIENT_ID, "
                "or connect with a personal access token instead."
            ),
        )

    org_id = await _resolve_org(principal, payload.org_id)
    try:
        flow = await github.start_device_flow(settings.moonphase_github_client_id)
    except github.GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    session = github.DeviceSession(
        id=secrets.token_urlsafe(16), org_id=str(org_id), flow=flow
    )
    github.put_session(session)

    return GitHubDeviceOut(
        session_id=session.id,
        state=session.state,
        user_code=flow.user_code,
        verification_uri=flow.verification_uri,
        interval=flow.interval,
    )


@router.get("/github/device/{session_id}", response_model=GitHubDeviceOut)
async def poll_github_device(
    session_id: str, principal: Principal = Depends(current_principal)
) -> GitHubDeviceOut:
    """Check whether the user has approved yet, and store the token if so."""
    settings = get_settings()
    session = github.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such GitHub sign-in.")

    if session.state == "complete":
        return GitHubDeviceOut(
            session_id=session.id,
            state=session.state,
            account=session.identity.account if session.identity else None,
        )

    if session.expired:
        session.state = "error"
        session.detail = "The device code expired. Start again."
        return GitHubDeviceOut(
            session_id=session.id, state=session.state, detail=session.detail
        )

    try:
        identity = await github.poll_device_flow(
            settings.moonphase_github_client_id, session.flow
        )
    except github.GitHubError as exc:
        session.state = "error"
        session.detail = str(exc)
        return GitHubDeviceOut(
            session_id=session.id, state=session.state, detail=session.detail
        )

    if identity is None:
        return GitHubDeviceOut(
            session_id=session.id,
            state=session.state,
            user_code=session.flow.user_code,
            verification_uri=session.flow.verification_uri,
            interval=session.flow.interval,
        )

    async with service_session() as conn:
        await queries.upsert_vcs_credential_privileged(
            conn,
            org_id=UUID(session.org_id),
            provider="github",
            auth_mode="oauth_device",
            account=identity.account,
            scopes=identity.scopes,
            token=identity.token,
            created_by=principal.user_id,
        )

    session.state = "complete"
    session.identity = identity
    return GitHubDeviceOut(
        session_id=session.id, state=session.state, account=identity.account
    )


@router.post("/github/token", response_model=WorkspaceProfileOut)
async def set_github_token(
    payload: GitHubTokenIn, principal: Principal = Depends(current_principal)
) -> WorkspaceProfileOut:
    org_id = await _resolve_org(principal, payload.org_id)
    try:
        identity = await github.verify_token(payload.token)
    except github.GitHubError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with service_session() as conn:
        await queries.upsert_vcs_credential_privileged(
            conn,
            org_id=org_id,
            provider="github",
            auth_mode="personal_token",
            account=identity.account,
            scopes=identity.scopes,
            token=identity.token,
            created_by=principal.user_id,
        )
    return await _build_out(principal, org_id)


@router.delete("/github", response_model=WorkspaceProfileOut)
async def disconnect_github(
    org_id: UUID | None = None, principal: Principal = Depends(current_principal)
) -> WorkspaceProfileOut:
    resolved = await _resolve_org(principal, org_id)
    async with service_session() as conn:
        await queries.delete_vcs_credential_privileged(conn, resolved, "github")
    return await _build_out(principal, resolved)


@router.get("/github/available")
async def github_device_available() -> dict[str, bool]:
    """Whether the device flow is configured, so the UI can hide it if not."""
    return {"device_flow": bool(get_settings().moonphase_github_client_id)}


# Re-exported for the app factory.
__all__ = ["router", "status"]
