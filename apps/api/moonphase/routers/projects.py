"""Project lifecycle: provision a container, run a harness inside it."""

from __future__ import annotations

import logging
import shlex
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from .. import (
    docker_remote,
    environments,
    imagebuild,
    preview,
    queries,
    runtime,
    sessions,
    ssh,
)
from .. import harness as harness_registry
from ..auth import Principal, current_principal
from ..config import get_settings
from ..db import service_session, user_session
from ..runtime import CAN_CONTROL, CAN_DELETE, CAN_OBSERVE, Forbidden, NotFound
from ..schemas import (
    ProjectCreate,
    ProjectOut,
    SendKeysIn,
    SessionCreateIn,
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
    del settings  # image comes from the project's environment, below

    async with user_session(principal.claims) as conn:
        environment_rows = await queries.list_environments(conn)
    known = {env.key for env in environments.merge(environment_rows)}
    if payload.environment and payload.environment not in known:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown environment {payload.environment!r}. "
                f"Available: {', '.join(sorted(known))}."
            ),
        )
    environment = environments.resolve(payload.environment, environment_rows)

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

        # A project on a server someone lent you is yours, not theirs: it
        # belongs to your organization, runs on your Claude account, and is
        # invisible to them beyond the fact that it exists.
        if server["shared"]:
            owning_org = await queries.personal_org_id(conn)
            if owning_org is None:
                raise HTTPException(
                    status_code=409,
                    detail="You have no personal organization to own this project.",
                )
        else:
            owning_org = server["org_id"]

    async with service_session() as conn:
        credential = await queries.resolve_harness_credential_privileged(
            conn, org_id=owning_org, project_id=owning_org,
            harness=payload.harness,
        )
    if credential is None:
        harness_name = harness_registry.get(payload.harness).display_name
        raise HTTPException(
            status_code=409,
            detail=(
                f"{harness_name} is not connected. Sign in once in Settings and "
                "every project will use it."
            ),
        )

    async with user_session(principal.claims) as conn:
        base_slug = queries.slugify(payload.name)
        slug = base_slug
        suffix = 1
        while not await queries.slug_is_free(conn, payload.server_id, slug):
            suffix += 1
            slug = f"{base_slug}-{suffix}"

        try:
            row = await queries.insert_project(
                conn,
                org_id=owning_org,
                server_id=payload.server_id,
                name=payload.name.strip(),
                slug=slug,
                harness=payload.harness,
                environment=environment.key,
                repo_url=payload.repo_url,
                container_name="",  # filled in below, once we know the id
                workspace_volume="",
                home_volume="",
                preview_port=None,
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

    try:
        container_id = await _provision_container(
            principal,
            server_id=payload.server_id,
            container=container,
            workspace_volume=workspace_volume,
            home_volume=home_volume,
            environment=environment,
            repo_url=payload.repo_url,
            preview_port=None,
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
    environment: environments.Environment,
    repo_url: str | None,
    preview_port: int | None,
    cpus: str | None,
    memory: str | None,
) -> str:
    target = await runtime.load_server_target(
        principal.claims, server_id, require=CAN_CONTROL
    )
    conn_ssh = await ssh.pool.get(target)

    # Environments are recipes, not published images: build on the server the
    # first time one is used. The tag encodes the recipe, so this is a no-op
    # afterwards and a fresh build whenever the definition changes.
    built = await imagebuild.ensure_image(
        conn_ssh,
        tag=environment.image,
        base_image=environment.base_image,
        setup_script=environment.setup_script,
    )
    if built:
        log.info("built %s on server %s", environment.image, server_id)
    image = environment.image

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
    # The ports behind them are gone; leaving listeners open would advertise
    # previews that refuse every connection.
    await preview.registry.close_project(str(project_id))

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
    """Sessions in this project, with live liveness and attached device counts.

    The counts come from tmux rather than the database on purpose: a stored
    count goes stale the moment a client drops, and a wrong "2 devices
    attached" is worse than not showing one.
    """
    async with user_session(principal.claims) as conn:
        project = await queries.get_project(conn, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        rows = await queries.get_sessions(conn, project_id)

    live: dict[str, int] = {}
    if project.get("container_name") and project["status"] == "running":
        try:
            ctx = await runtime.load_project_context(
                principal.claims, project_id, require=CAN_OBSERVE
            )
            conn_ssh = await ssh.pool.get(ctx.target)
            live = await sessions.client_counts(conn_ssh, ctx.container)
        except (SSHError, NotFound) as exc:
            # An unreachable server should not blank the list; the rows are
            # still the truth about which sessions exist.
            log.debug("could not read live session state: %s", exc)

    return [
        SessionOut.model_validate(
            {
                **row,
                "alive": row["tmux_session"] in live,
                "attached_clients": live.get(str(row["tmux_session"]), 0),
            }
        )
        for row in rows
    ]


@router.post(
    "/{project_id}/sessions",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    project_id: UUID,
    payload: SessionCreateIn,
    principal: Principal = Depends(current_principal),
) -> SessionOut:
    """Add a second agent to a project.

    Sessions share the workspace volume, so two agents can work the same
    checkout — which is the point, and also why the UI should make it obvious
    they are not isolated from each other.
    """
    name = sessions.sanitise_name(payload.name)

    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        existing = await queries.get_sessions(conn, project_id)
    if any(row["tmux_session"] == name for row in existing):
        raise HTTPException(
            status_code=409, detail=f"This project already has a session called {name!r}."
        )

    workspace_profile = await runtime.load_profile(
        principal.claims, ctx.project["org_id"], project_id, ctx.harness
    )

    conn_ssh = await ssh.pool.get(ctx.target)
    container = await docker_remote.inspect(conn_ssh, ctx.container)
    if container is None:
        raise HTTPException(status_code=409, detail="The project container is gone.")
    if container.state != "running":
        await docker_remote.start(conn_ssh, ctx.container)

    try:
        await sessions.ensure_session(
            conn_ssh,
            ctx.container,
            harness_kind=ctx.harness,
            workspace_profile=workspace_profile,
            session=name,
        )
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        row = await queries.upsert_session(
            conn,
            project_id=project_id,
            harness=ctx.harness,
            tmux_session=name,
            state="running",
            transcript_path=harness_registry.get(ctx.harness).transcript_dir(),
            mark_started=True,
        )
    return SessionOut.model_validate({**row, "alive": True, "attached_clients": 0})


@router.delete(
    "/{project_id}/sessions/{name}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_session(
    project_id: UUID, name: str, principal: Principal = Depends(current_principal)
) -> None:
    """Kill a session and forget it.

    Refuses the last one: a project with no session has no terminal to open and
    no obvious way back, so removing it would be a trap rather than a choice.
    Use Stop to shut the whole project down instead.
    """
    session_name = sessions.sanitise_name(name)

    async with user_session(principal.claims) as conn:
        project = await queries.get_project(conn, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        total = await queries.count_sessions(conn, project_id)

    if total <= 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "This is the project's only session. Stop the project instead, or "
                "add another session first."
            ),
        )

    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
        conn_ssh = await ssh.pool.get(ctx.target)
        await sessions.kill_session(conn_ssh, ctx.container, session_name)
    except (SSHError, NotFound) as exc:
        # The row must still go: an unreachable server should not leave a
        # session the user cannot remove.
        log.warning("could not kill tmux session %s: %s", session_name, exc)

    async with user_session(principal.claims) as conn:
        removed = await queries.delete_session_row(conn, project_id, session_name)
    if not removed:
        raise HTTPException(status_code=404, detail="No such session.")


@router.post(
    "/{project_id}/sessions/{name}/detach-clients",
    status_code=status.HTTP_200_OK,
)
async def detach_clients(
    project_id: UUID, name: str, principal: Principal = Depends(current_principal)
) -> dict[str, int]:
    """Detach every device from a session, without disturbing the session.

    The escape hatch for phantom clients: `docker exec` leaves its process
    running when a client vanishes, so a crashed app or a killed backend can
    leave attachments behind that still constrain the window size.
    """
    session_name = sessions.sanitise_name(name)
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn_ssh = await ssh.pool.get(ctx.target)
    detached = await sessions.detach_all_clients(conn_ssh, ctx.container, session_name)
    return {"detached": detached}


@router.post("/{project_id}/sessions/start", response_model=SessionOut)
async def start_session(
    project_id: UUID,
    payload: SessionStartIn | None = None,
    principal: Principal = Depends(current_principal),
) -> SessionOut:
    """Ensure the harness is running in tmux. Safe to call on every project open."""
    options = payload or SessionStartIn()
    session_name = sessions.sanitise_name(options.session or sessions.DEFAULT_SESSION)
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    workspace_profile = await runtime.load_profile(
        principal.claims, ctx.project["org_id"], project_id, ctx.harness
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
            workspace_profile=workspace_profile,
            session=session_name,
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
            tmux_session=session_name,
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
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
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

    Open to the owner, and to whoever owns the machine it is running on —
    reclaiming your own hardware should not require going behind Moonphase's
    back with `docker rm`. Everyone else, including a collaborator who can
    otherwise drive the session, gets a 403.
    """
    async with user_session(principal.claims) as conn:
        project = await queries.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if project.get("access") not in CAN_DELETE:
        raise Forbidden(
            "Only the owner of this project, or the owner of the server it runs "
            "on, can delete it."
        )

    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_DELETE
        )
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

    await preview.registry.close_project(str(project_id))

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
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn_ssh = await ssh.pool.get(ctx.target)
    result = await ssh.run(
        conn_ssh,
        f"docker logs --tail {int(tail)} {shlex.quote(ctx.container)} 2>&1",
        timeout=60,
    )
    return {"logs": result.stdout}
