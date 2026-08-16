"""Project lifecycle: provision a container, run a harness inside it."""

from __future__ import annotations

import logging
import shlex
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from .. import docker_remote, queries, runtime, sessions, ssh
from .. import harness as harness_registry
from ..auth import Principal, current_principal
from ..config import get_settings
from ..db import service_session, user_session
from ..harness import HarnessAuthMode, HarnessCredential
from ..runtime import NotFound
from ..schemas import (
    ProjectCreate,
    ProjectOut,
    SendKeysIn,
    SessionOut,
    SessionStartIn,
)
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _to_out(row: dict[str, Any]) -> ProjectOut:
    return ProjectOut.model_validate(row)


def _container_name(slug: str, project_id: UUID) -> str:
    # The id suffix keeps two orgs' identically-named projects from colliding
    # on a shared server, where Docker names are a flat global namespace.
    return f"mp-{slug}-{str(project_id)[:8]}"


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    server_id: UUID | None = None, principal: Principal = Depends(current_principal)
) -> list[ProjectOut]:
    async with user_session(principal.claims) as conn:
        rows = await queries.list_projects(conn, server_id)
    return [_to_out(r) for r in rows]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> ProjectOut:
    async with user_session(principal.claims) as conn:
        row = await queries.get_project(conn, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return _to_out(row)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate, principal: Principal = Depends(current_principal)
) -> ProjectOut:
    """Create the row, then provision the container.

    Provisioning failures leave the project in `error` with a readable detail
    rather than rolling back, so the user can fix the cause (bad repo URL, full
    disk) and retry without re-entering anything.
    """
    settings = get_settings()

    async with user_session(principal.claims) as conn:
        server = await queries.get_server(conn, payload.server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="Server not found.")
        if server["status"] != "online":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Server '{server['name']}' is {server['status']}. "
                    "Bootstrap it successfully before creating projects."
                ),
            )

        base_slug = queries.slugify(payload.name)
        slug = base_slug
        suffix = 1
        while not await queries.slug_is_free(conn, payload.server_id, slug):
            suffix += 1
            slug = f"{base_slug}-{suffix}"

        try:
            row = await queries.insert_project(
                conn,
                org_id=server["org_id"],
                server_id=payload.server_id,
                name=payload.name.strip(),
                slug=slug,
                harness=payload.harness,
                repo_url=payload.repo_url,
                container_name="",  # filled in below, once we know the id
                workspace_volume="",
                home_volume="",
                preview_port=payload.preview_port,
                created_by=principal.user_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        project_id = row["id"]
        container = _container_name(slug, project_id)
        workspace_volume = f"{container}-workspace"
        home_volume = f"{container}-home"
        await queries.set_project_container(
            conn,
            project_id,
            container_name=container,
            workspace_volume=workspace_volume,
            home_volume=home_volume,
        )

    # Optional per-project credential, stored before provisioning so the very
    # first session already has it.
    if payload.harness_auth_mode == "api_key" and payload.api_key:
        async with service_session() as conn:
            await queries.upsert_harness_credential_privileged(
                conn,
                org_id=server["org_id"],
                project_id=project_id,
                harness=payload.harness,
                auth_mode="api_key",
                label="Project key",
                api_key=payload.api_key,
                oauth_blob=None,
                created_by=principal.user_id,
            )

    try:
        container_id = await _provision_container(
            principal,
            server_id=payload.server_id,
            container=container,
            workspace_volume=workspace_volume,
            home_volume=home_volume,
            image=settings.moonphase_runtime_image,
            repo_url=payload.repo_url,
            preview_port=payload.preview_port,
            cpus=payload.cpus,
            memory=payload.memory,
        )
    except (SSHError, NotFound) as exc:
        async with user_session(principal.claims) as conn:
            await queries.update_project_state(
                conn, project_id, status="error", status_detail=str(exc)
            )
            refreshed = await queries.get_project(conn, project_id)
        assert refreshed is not None
        return _to_out(refreshed)

    async with user_session(principal.claims) as conn:
        await queries.update_project_state(
            conn, project_id, status="running", status_detail=None, container_id=container_id
        )
        await queries.upsert_session(
            conn,
            project_id=project_id,
            harness=payload.harness,
            tmux_session=sessions.DEFAULT_SESSION,
            state="stopped",
            transcript_path=harness_registry.get(payload.harness).transcript_dir(),
        )
        refreshed = await queries.get_project(conn, project_id)
    assert refreshed is not None
    return _to_out(refreshed)


async def _provision_container(
    principal: Principal,
    *,
    server_id: UUID,
    container: str,
    workspace_volume: str,
    home_volume: str,
    image: str,
    repo_url: str | None,
    preview_port: int | None,
    cpus: str | None,
    memory: str | None,
) -> str:
    target = await runtime.load_server_target(principal.claims, server_id)
    conn_ssh = await ssh.pool.get(target)

    if not await docker_remote.image_present(conn_ssh, image):
        log.info("pulling %s on server %s", image, server_id)
        pulled = await docker_remote.pull(conn_ssh, image)
        if not pulled.ok:
            raise SSHError(
                f"Could not pull image {image}: "
                f"{(pulled.stderr or pulled.stdout).strip()[:300]}"
            )

    await docker_remote.volume_create(conn_ssh, workspace_volume)
    await docker_remote.volume_create(conn_ssh, home_volume)

    existing = await docker_remote.inspect(conn_ssh, container)
    if existing is not None:
        await docker_remote.remove(conn_ssh, container)

    ports = {preview_port: preview_port} if preview_port else None
    container_id = await docker_remote.run_container(
        conn_ssh,
        name=container,
        image=image,
        workspace_volume=workspace_volume,
        home_volume=home_volume,
        published_ports=ports,
        cpus=cpus,
        memory=memory,
    )

    # The home volume shadows the image's /home/dev on first use, so ownership
    # has to be fixed from outside before the dev user touches it.
    await docker_remote.exec_capture(
        conn_ssh, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
        user="root", timeout=120,
    )

    if repo_url:
        clone = await docker_remote.exec_capture(
            conn_ssh,
            container,
            ["git", "clone", "--depth", "50", repo_url, "."],
            workdir="/workspace",
            timeout=900,
        )
        if not clone.ok:
            raise SSHError(
                "Container started but `git clone` failed: "
                f"{(clone.stderr or clone.stdout).strip()[:400]}"
            )

    return container_id


@router.post("/{project_id}/start", response_model=ProjectOut)
async def start_project(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> ProjectOut:
    """Start a stopped container (after a host reboot, say)."""
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn_ssh = await ssh.pool.get(ctx.target)
    try:
        await docker_remote.start(conn_ssh, ctx.container)
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        await queries.update_project_state(
            conn, project_id, status="running", status_detail=None
        )
        row = await queries.get_project(conn, project_id)
    assert row is not None
    return _to_out(row)


@router.post("/{project_id}/stop", response_model=ProjectOut)
async def stop_project(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> ProjectOut:
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn_ssh = await ssh.pool.get(ctx.target)
    await docker_remote.stop(conn_ssh, ctx.container)

    async with user_session(principal.claims) as conn:
        await queries.update_project_state(
            conn, project_id, status="stopped", status_detail=None
        )
        await queries.upsert_session(
            conn,
            project_id=project_id,
            harness=ctx.harness,
            tmux_session=sessions.DEFAULT_SESSION,
            state="stopped",
        )
        row = await queries.get_project(conn, project_id)
    assert row is not None
    return _to_out(row)


# --- sessions ---------------------------------------------------------------


@router.get("/{project_id}/sessions", response_model=list[SessionOut])
async def list_sessions(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> list[SessionOut]:
    async with user_session(principal.claims) as conn:
        project = await queries.get_project(conn, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        rows = await queries.get_sessions(conn, project_id)
    return [SessionOut.model_validate(r) for r in rows]


@router.post("/{project_id}/sessions/start", response_model=SessionOut)
async def start_session(
    project_id: UUID,
    payload: SessionStartIn | None = None,
    principal: Principal = Depends(current_principal),
) -> SessionOut:
    """Ensure the harness is running in tmux. Safe to call on every project open."""
    options = payload or SessionStartIn()
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    credential_row = await runtime.resolve_credential(
        ctx.project["org_id"], project_id, ctx.harness
    )
    credential = None
    if credential_row:
        credential = HarnessCredential(
            mode=HarnessAuthMode(credential_row["auth_mode"]),
            api_key=credential_row.get("api_key"),
            oauth_blob=credential_row.get("oauth_blob"),
        )

    conn_ssh = await ssh.pool.get(ctx.target)

    container = await docker_remote.inspect(conn_ssh, ctx.container)
    if container is None:
        raise HTTPException(
            status_code=409, detail="The project container no longer exists."
        )
    if container.state != "running":
        await docker_remote.start(conn_ssh, ctx.container)

    try:
        await sessions.ensure_session(
            conn_ssh,
            ctx.container,
            harness_kind=ctx.harness,
            credential=credential,
            restart=options.restart,
        )
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    authed = await sessions.is_authenticated(
        conn_ssh, ctx.container, harness_registry.get(ctx.harness)
    )

    async with user_session(principal.claims) as conn:
        row = await queries.upsert_session(
            conn,
            project_id=project_id,
            harness=ctx.harness,
            tmux_session=sessions.DEFAULT_SESSION,
            state="running",
            transcript_path=harness_registry.get(ctx.harness).transcript_dir(),
            mark_started=True,
        )
    out = SessionOut.model_validate(row)
    if not authed:
        log.info(
            "project %s session started without harness credentials; the user "
            "will need to sign in inside the terminal",
            project_id,
        )
    return out


@router.post("/{project_id}/sessions/keys", status_code=status.HTTP_204_NO_CONTENT)
async def send_keys(
    project_id: UUID,
    payload: SendKeysIn,
    principal: Principal = Depends(current_principal),
) -> None:
    """Type into the session without attaching.

    This is the write path the phone client uses to answer a permission prompt;
    the keystroke lands in the same tmux pane a desktop client is watching.
    """
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn_ssh = await ssh.pool.get(ctx.target)
    try:
        await sessions.send_keys(
            conn_ssh, ctx.container, payload.keys, enter=payload.enter
        )
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project_id}/sessions/snapshot")
async def snapshot(
    project_id: UUID,
    lines: int = 200,
    principal: Principal = Depends(current_principal),
) -> dict[str, str]:
    """Plain-text view of the pane, for previews and debugging."""
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn_ssh = await ssh.pool.get(ctx.target)
    text_out = await sessions.capture_pane(conn_ssh, ctx.container, lines=lines)
    return {"text": text_out}


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    delete_volumes: bool = False,
    principal: Principal = Depends(current_principal),
) -> None:
    """Remove a project. Volumes are kept unless explicitly discarded.

    Defaulting to keeping them means an accidental delete loses the container,
    not the work.
    """
    async with user_session(principal.claims) as conn:
        project = await queries.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
        conn_ssh = await ssh.pool.get(ctx.target)
        if project.get("container_name"):
            await docker_remote.remove(conn_ssh, project["container_name"])
        if delete_volumes:
            for volume in (project.get("workspace_volume"), project.get("home_volume")):
                if volume:
                    await docker_remote.volume_remove(conn_ssh, volume)
    except (SSHError, NotFound) as exc:
        # An unreachable server must not trap a stale row in the UI.
        log.warning("cleanup for project %s failed: %s", project_id, exc)

    async with user_session(principal.claims) as conn:
        deleted = await queries.delete_project(conn, project_id)
    if not deleted:
        raise HTTPException(status_code=403, detail="Not allowed to delete this project.")


@router.get("/{project_id}/logs")
async def container_logs(
    project_id: UUID,
    tail: int = 200,
    principal: Principal = Depends(current_principal),
) -> dict[str, str]:
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn_ssh = await ssh.pool.get(ctx.target)
    result = await ssh.run(
        conn_ssh,
        f"docker logs --tail {int(tail)} {shlex.quote(ctx.container)} 2>&1",
        timeout=60,
    )
    return {"logs": result.stdout}
