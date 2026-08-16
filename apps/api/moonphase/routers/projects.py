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
    socks,
    ssh,
    workspaces,
)
from .. import harness as harness_registry
from ..auth import Principal, current_principal
from ..config import get_settings
from ..db import service_session, user_session
from ..harness import SessionSpace
from ..runtime import (
    CAN_CONTROL,
    CAN_DELETE,
    CAN_OBSERVE,
    Forbidden,
    NotFound,
)
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
        # Deliberately no session row here. A session belongs to a person and
        # runs on their subscription, so it is created the first time someone
        # opens the project — not speculatively at provision time on behalf of
        # whoever happened to click Create.
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
    await socks.registry.close(str(project_id))

    async with user_session(principal.claims) as conn:
        await queries.update_project_state(
            conn, project_id, status="stopped", status_detail=None
        )
        # Every session in the container went down with it, not just the
        # caller's — leaving other people's rows saying "running" would have
        # the sidebar claim their agent is still working.
        await queries.mark_sessions_stopped(conn, project_id)
        row = await queries.get_project(conn, project_id)
    assert row is not None
    return _to_out(row)


# --- sessions ---------------------------------------------------------------
#
# A session belongs to one person. Sharing a project shares the code and the
# machine, never the coding subscription behind them: you drive your own
# sessions and may watch anybody's, so no work ever runs on an account other
# than its owner's. Each session gets its own HOME — credentials, harness
# config, history, git identity — and its own git worktree, so two agents in
# one container neither authenticate as each other nor overwrite each other.


def _session_name_for(principal: Principal, taken: set[str]) -> str:
    """A default session name derived from who is asking.

    Names used to be incidental ("moonphase"); now they identify a person in a
    list other people also appear in, and they end up in a branch name. The
    local part of the email is the closest thing to a handle we have without
    asking for one.
    """
    base = sessions.sanitise_name((principal.email or "session").split("@")[0])
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


async def _prepare_space(
    principal: Principal,
    ctx: runtime.ProjectContext,
    name: str,
    profile: Any,
) -> tuple[Any, str, str]:
    """Give a session somewhere private to live. Returns (space, workdir, branch)."""
    conn_ssh = await ssh.pool.get(ctx.target)

    container = await docker_remote.inspect(conn_ssh, ctx.container)
    if container is None:
        raise HTTPException(status_code=409, detail="The project container is gone.")
    if container.state != "running":
        await docker_remote.start(conn_ssh, ctx.container)

    workdir, branch = await workspaces.ensure_worktree(
        conn_ssh,
        ctx.container,
        name,
        author_name=profile.git_user_name or (principal.email or "Moonphase"),
        author_email=profile.git_user_email or (principal.email or "moonphase@localhost"),
    )
    return sessions.space_for(name, workdir), workdir, branch


async def _profile_or_409(
    principal: Principal, ctx: runtime.ProjectContext
) -> Any:
    profile = await runtime.load_session_profile(
        principal.claims, ctx.project, ctx.harness
    )
    if not profile.has_harness_auth:
        harness_name = harness_registry.get(ctx.harness).display_name
        raise HTTPException(
            status_code=409,
            detail=(
                f"{harness_name} is not connected to your account. A session "
                "runs on the subscription of whoever started it, so it has to "
                "be yours — connect it in Settings."
            ),
        )
    return profile


@router.get("/{project_id}/sessions", response_model=list[SessionOut])
async def list_sessions(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> list[SessionOut]:
    """Every session in this project, yours first.

    Live liveness and attached device counts come from tmux rather than the
    database on purpose: a stored count goes stale the moment a client drops,
    and a wrong "2 devices attached" is worse than not showing one.
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
        except (SSHError, NotFound, Forbidden) as exc:
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
    payload: SessionCreateIn | None = None,
    principal: Principal = Depends(current_principal),
) -> SessionOut:
    """Start a session of your own in this project.

    Yours in every sense that matters: your credentials, your git identity,
    your branch. Someone else's session in the same project is unaffected by
    this one existing, and neither can see the other's account.
    """
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        existing = await queries.get_sessions(conn, project_id)
    taken = {str(row["tmux_session"]) for row in existing}

    requested = payload.name if payload and payload.name else None
    name = sessions.sanitise_name(requested) if requested else _session_name_for(
        principal, taken
    )
    if name in taken:
        raise HTTPException(
            status_code=409, detail=f"This project already has a session called {name!r}."
        )

    profile = await _profile_or_409(principal, ctx)
    space, workdir, branch = await _prepare_space(principal, ctx, name, profile)
    conn_ssh = await ssh.pool.get(ctx.target)

    try:
        await sessions.ensure_session(
            conn_ssh,
            ctx.container,
            harness_kind=ctx.harness,
            workspace_profile=profile,
            session=name,
            space=space,
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
            user_id=principal.user_id,
            workdir=workdir,
            home_dir=space.home,
            branch=branch,
            transcript_path=harness_registry.get(ctx.harness).transcript_dir(space),
            mark_started=True,
        )
    return SessionOut.model_validate({**row, "alive": True, "attached_clients": 0})


@router.post("/{project_id}/sessions/start", response_model=SessionOut)
async def start_session(
    project_id: UUID,
    payload: SessionStartIn | None = None,
    principal: Principal = Depends(current_principal),
) -> SessionOut:
    """Make sure the caller has a running session here. Safe to call on open.

    With no name it resolves to the caller's own session, creating one if this
    is their first time in the project. It will never adopt somebody else's:
    that would run their subscription on this caller's keystrokes.
    """
    options = payload or SessionStartIn()

    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        rows = await queries.get_sessions(conn, project_id)

    taken = {str(row["tmux_session"]) for row in rows}
    mine = [row for row in rows if row.get("is_mine")]

    if options.session:
        name = sessions.sanitise_name(options.session)
        owned = next((r for r in rows if str(r["tmux_session"]) == name), None)
        if owned is not None and not owned.get("is_mine"):
            raise Forbidden(
                f"Session {name!r} belongs to someone else. You can watch it, "
                "but starting or restarting it would run it on their account."
            )
    elif mine:
        name = str(mine[0]["tmux_session"])
    else:
        name = _session_name_for(principal, taken)

    profile = await _profile_or_409(principal, ctx)

    # A session's home and checkout are fixed when it is created. Moving a
    # running one would point it at a directory its harness has never seen and
    # leave its real state orphaned — so an existing session keeps what it was
    # given, and only a restart (which recreates it anyway) adopts the current
    # layout. That is also the upgrade path for sessions made before sessions
    # had owners, which still live in the container's shared home.
    existing = next((r for r in rows if str(r["tmux_session"]) == name), None)
    if existing is not None and not options.restart:
        space = SessionSpace(
            home=str(existing["home_dir"]), workdir=str(existing["workdir"])
        )
        workdir, branch = space.workdir, existing.get("branch")
        conn_ssh = await ssh.pool.get(ctx.target)
    else:
        space, workdir, branch = await _prepare_space(principal, ctx, name, profile)
        conn_ssh = await ssh.pool.get(ctx.target)

    try:
        await sessions.ensure_session(
            conn_ssh,
            ctx.container,
            harness_kind=ctx.harness,
            workspace_profile=profile,
            session=name,
            space=space,
            restart=options.restart,
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
            user_id=principal.user_id,
            workdir=workdir,
            home_dir=space.home,
            branch=branch,
            transcript_path=harness_registry.get(ctx.harness).transcript_dir(space),
            mark_started=True,
        )
    return SessionOut.model_validate(row)


@router.delete(
    "/{project_id}/sessions/{name}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_session(
    project_id: UUID, name: str, principal: Principal = Depends(current_principal)
) -> None:
    """Kill a session and forget it.

    Yours to remove, or the project owner's to reclaim. The worktree goes with
    it; the branch does not, because it may hold the only copy of work and a
    "close this session" button should not be able to destroy that.
    """
    session_name = sessions.sanitise_name(name)

    async with user_session(principal.claims) as conn:
        project = await queries.get_project(conn, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        row = await queries.get_session(conn, project_id, session_name)
    if row is None:
        raise HTTPException(status_code=404, detail="No such session.")
    if not row.get("is_mine") and project.get("access") != "admin":
        raise Forbidden(
            "That session belongs to someone else. Only they, or an owner of "
            "the project, can remove it."
        )

    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
        conn_ssh = await ssh.pool.get(ctx.target)
        await sessions.kill_session(conn_ssh, ctx.container, session_name)
        await workspaces.remove_worktree(conn_ssh, ctx.container, session_name)
    except (SSHError, NotFound) as exc:
        # The row must still go: an unreachable server should not leave a
        # session the user cannot remove.
        log.warning("could not clean up session %s: %s", session_name, exc)

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
    """Detach every device from one of your sessions, without disturbing it.

    The escape hatch for phantom clients: `docker exec` leaves its process
    running when a client vanishes, so a crashed app or a killed backend can
    leave attachments behind that still constrain the window size.
    """
    session_name = sessions.sanitise_name(name)
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await _require_own_session(principal, project_id, session_name)

    conn_ssh = await ssh.pool.get(ctx.target)
    detached = await sessions.detach_all_clients(conn_ssh, ctx.container, session_name)
    return {"detached": detached}


async def _require_own_session(
    principal: Principal, project_id: UUID, name: str
) -> dict[str, Any]:
    """Anything that puts input into a session must prove it is the caller's.

    Watching somebody else's work is fine and deliberate. Typing into it is
    not: their harness is authenticated as them, so every keystroke would be
    billed to and attributed to a person who is not in the room.
    """
    async with user_session(principal.claims) as conn:
        row = await queries.get_session(conn, project_id, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No session called {name!r}.")
    if not row.get("is_mine"):
        raise Forbidden(
            f"Session {name!r} belongs to someone else. You can watch it, but "
            "typing into it would run on their account."
        )
    return row


@router.post("/{project_id}/sessions/keys", status_code=status.HTTP_204_NO_CONTENT)
async def send_keys(
    project_id: UUID,
    payload: SendKeysIn,
    session: str | None = None,
    principal: Principal = Depends(current_principal),
) -> None:
    """Type into one of your sessions without attaching.

    This is the write path the phone client uses to answer a permission prompt;
    the keystroke lands in the same tmux pane your desktop is watching.
    """
    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    name = sessions.sanitise_name(session) if session else None
    if name is None:
        async with user_session(principal.claims) as conn:
            rows = await queries.get_sessions(conn, project_id)
        mine = [r for r in rows if r.get("is_mine")]
        if not mine:
            raise HTTPException(
                status_code=409, detail="You have no session in this project yet."
            )
        name = str(mine[0]["tmux_session"])
    else:
        await _require_own_session(principal, project_id, name)

    conn_ssh = await ssh.pool.get(ctx.target)
    try:
        await sessions.send_keys(
            conn_ssh, ctx.container, payload.keys, session=name, enter=payload.enter
        )
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project_id}/sessions/snapshot")
async def snapshot(
    project_id: UUID,
    lines: int = 200,
    session: str | None = None,
    principal: Principal = Depends(current_principal),
) -> dict[str, str]:
    """Plain-text view of a pane, for previews and debugging."""
    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    name = sessions.sanitise_name(session) if session else sessions.DEFAULT_SESSION
    conn_ssh = await ssh.pool.get(ctx.target)
    text_out = await sessions.capture_pane(
        conn_ssh, ctx.container, session=name, lines=lines
    )
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
    await socks.registry.close(str(project_id))

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
