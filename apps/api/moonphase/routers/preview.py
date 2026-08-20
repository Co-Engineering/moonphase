"""Preview ports.

No configuration: Moonphase looks at what the container is actually listening
on and offers to tunnel it. A dev server that picks a different port after a
restart shows up on the next poll with no action from the user.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from .. import preview, runtime, ssh
from ..auth import Principal, current_principal
from ..config import get_settings
from ..runtime import CAN_CONTROL, CAN_OBSERVE, NotFound
from ..schemas import DetectedPortOut, PreviewOut, PreviewServiceOut
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["preview"])


def _url_for(local_port: int) -> str:
    settings = get_settings()
    return f"http://{settings.moonphase_preview_host}:{local_port}"


@router.get("/{project_id}/ports", response_model=list[DetectedPortOut])
async def list_ports(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> list[DetectedPortOut]:
    """Everything listening inside the container, and whether it is shared."""
    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_OBSERVE
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        conn_ssh = await ssh.pool.get(ctx.target)
        detected = await preview.detect_ports(conn_ssh, ctx.container)
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    active = preview.registry.for_project(str(project_id))
    return [
        DetectedPortOut(
            port=item.port,
            bind=item.bind,
            process=item.process,
            loopback_only=item.loopback_only,
            shared=item.port in active,
            url=_url_for(active[item.port].local_port) if item.port in active else None,
        )
        for item in detected
    ]


@router.post("/{project_id}/preview", response_model=PreviewOut)
async def open_preview(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> PreviewOut:
    """Start a proxy that puts the whole container on the caller's machine.

    This is the answer to a problem forwarding cannot solve. A page served from
    the container runs in a browser *here*, so when its code asks for
    `http://localhost:8000` it gets this machine's port 8000 — not the API it
    means. Renumbering does not help, because the address is the application's
    choice and it asks for the one it was written with.

    Pointing the browser at this proxy changes what those names mean instead.
    Every address it asks for resolves inside the container, so nothing has to
    be rewritten, nothing has to be declared, and an app that hardcodes a port
    or serves on 80 works the same as one that does everything properly.
    """
    # Control, not observation. A preview is a live network path into the
    # container: whoever holds it can POST to the app's own API and change
    # whatever that API changes. Someone shared in to watch a session can see
    # what the agent is doing; acting on it through a side door is a different
    # thing, and view-only has to mean it.
    try:
        ctx = await runtime.load_project_context(
            principal.claims, project_id, require=CAN_CONTROL
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # No proxy is started here any more.
    #
    # It used to open a SOCKS listener on this machine's loopback, which was
    # only ever reachable by a browser on this machine — true while the desktop
    # shell existed solely as a development build beside the API, and false for
    # every installed app. Those now carry the SOCKS stream over an
    # authenticated WebSocket instead, so this listener had no client left at
    # all: an unauthenticated path into the container, open on the server, for
    # nobody.
    #
    # What is left here is the part that was always the point of this call —
    # which ports the container is actually listening on, so the preview knows
    # where to start.
    settings = get_settings()
    services: list[PreviewServiceOut] = []
    try:
        conn_ssh = await ssh.pool.get(ctx.target)
        detected = await preview.detect_ports(conn_ssh, ctx.container)
        probed = await preview.probe_services(
            conn_ssh, ctx.container, [item.port for item in detected]
        )
        services = [
            PreviewServiceOut(
                port=item.port,
                kind=str((probed.get(item.port) or {}).get("kind") or "unknown"),
                title=(probed.get(item.port) or {}).get("title"),
                process=item.process,
            )
            for item in detected
        ]
        # Ordered so the first entry is the one to open, rather than the one
        # with the smallest number.
        services.sort(
            key=lambda item: preview.rank(item.port, item.kind, item.title)
        )
    except SSHError as exc:
        # Only a starting URL depends on this, so a slow container should not
        # stop the preview opening.
        log.debug("preview: could not inspect services: %s", exc)

    return PreviewOut(
        proxy_host=settings.moonphase_preview_host,
        services=services,
        container=ctx.container,
    )


@router.delete("/{project_id}/preview", status_code=status.HTTP_204_NO_CONTENT)
async def close_preview(
    project_id: UUID, principal: Principal = Depends(current_principal)
) -> None:
    try:
        await runtime.load_project_context(
            principal.claims, project_id, require=CAN_CONTROL
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project_id}/ports/{port}/share", response_model=DetectedPortOut)
async def share_port(
    project_id: UUID,
    port: int,
    principal: Principal = Depends(current_principal),
) -> DetectedPortOut:
    """Open a tunnel to a container port and return a URL that reaches it.

    Works regardless of how the dev server bound: the tunnel enters the
    container's own network namespace, so `127.0.0.1` inside is reachable
    without the port ever being published.
    """
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=422, detail="Port out of range.")

    try:
        ctx = await runtime.load_project_context(principal.claims, project_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        tunnel = await preview.registry.ensure(
            project_id=str(project_id),
            container=ctx.container,
            port=port,
            target=ctx.target,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not open a local listener: {exc}"
        ) from exc

    return DetectedPortOut(
        port=port,
        bind="container",
        shared=True,
        url=_url_for(tunnel.local_port),
    )


@router.delete("/{project_id}/ports/{port}/share", status_code=status.HTTP_204_NO_CONTENT)
async def unshare_port(
    project_id: UUID, port: int, principal: Principal = Depends(current_principal)
) -> None:
    del principal
    await preview.registry.close(str(project_id), port)
