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
from ..runtime import CAN_OBSERVE, NotFound
from ..schemas import DetectedPortOut
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
