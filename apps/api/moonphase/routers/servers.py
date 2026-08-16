"""Server management: add, bootstrap, inspect, remove."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from .. import docker_remote, provision, queries, ssh
from ..auth import Principal, current_principal
from ..db import service_session, user_session
from ..runtime import NotFound, load_server_target
from ..schemas import ServerBootstrapOut, ServerCreate, ServerOut
from ..ssh import HostKeyMismatch, SSHError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/servers", tags=["servers"])


def _to_out(row: dict[str, Any]) -> ServerOut:
    return ServerOut.model_validate({**row, "project_count": row.get("project_count", 0)})


@router.get("", response_model=list[ServerOut])
async def list_servers(principal: Principal = Depends(current_principal)) -> list[ServerOut]:
    async with user_session(principal.claims) as conn:
        rows = await queries.list_servers(conn)
    return [_to_out(r) for r in rows]


@router.get("/{server_id}", response_model=ServerOut)
async def get_server(
    server_id: UUID, principal: Principal = Depends(current_principal)
) -> ServerOut:
    async with user_session(principal.claims) as conn:
        row = await queries.get_server(conn, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    return _to_out(row)


@router.post("", response_model=ServerBootstrapOut, status_code=status.HTTP_201_CREATED)
async def create_server(
    payload: ServerCreate, principal: Principal = Depends(current_principal)
) -> ServerBootstrapOut:
    """Create a server row, then immediately try to bootstrap it.

    The row is committed before bootstrapping so a slow or failing SSH attempt
    still leaves something in the UI to retry against, rather than losing what
    the user typed.
    """
    try:
        payload.validate_credentials()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async with user_session(principal.claims) as conn:
        try:
            org_id = await queries.resolve_org(conn, payload.org_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            row = await queries.insert_server(
                conn,
                org_id=org_id,
                name=payload.name.strip(),
                host=payload.host,
                port=payload.port,
                ssh_user=payload.ssh_user.strip(),
                auth_mode=payload.auth_mode,
                created_by=principal.user_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    server_id = row["id"]

    # Stash whatever the user gave us before doing anything with it, so a crash
    # mid-bootstrap does not strand a server we cannot retry.
    async with service_session() as conn:
        await queries.store_server_credentials_privileged(
            conn,
            server_id,
            private_key=payload.private_key,
            passphrase=payload.passphrase,
            password=payload.password,
        )

    return await _run_bootstrap(
        principal,
        server_id,
        auto_install_docker=payload.auto_install_docker,
    )


@router.post("/{server_id}/bootstrap", response_model=ServerBootstrapOut)
async def rerun_bootstrap(
    server_id: UUID,
    auto_install_docker: bool = True,
    principal: Principal = Depends(current_principal),
) -> ServerBootstrapOut:
    """Retry onboarding — used after the user installs a managed public key."""
    async with user_session(principal.claims) as conn:
        row = await queries.get_server(conn, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    return await _run_bootstrap(principal, server_id, auto_install_docker=auto_install_docker)


async def _run_bootstrap(
    principal: Principal, server_id: UUID, *, auto_install_docker: bool
) -> ServerBootstrapOut:
    async with user_session(principal.claims) as conn:
        server = await queries.get_server(conn, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found.")

    async with service_session() as conn:
        target = await queries.load_ssh_target_privileged(conn, server_id)

    # A pooled connection from a previous attempt would skip the very
    # authentication we are trying to verify.
    await ssh.pool.drop(str(server_id))

    try:
        result = await provision.bootstrap(
            server_id=str(server_id),
            server_name=server["name"],
            host=server["host"],
            port=server["port"],
            ssh_user=server["ssh_user"],
            auth_mode=server["ssh_auth_mode"],
            password=target.password if target else None,
            provided_private_key=(
                target.private_key
                if target and server["ssh_auth_mode"] == "provided_key"
                else None
            ),
            provided_passphrase=target.passphrase if target else None,
            existing_private_key=(
                target.private_key
                if target and server["ssh_auth_mode"] == "managed_key"
                else None
            ),
            existing_public_key=server.get("managed_public_key"),
            known_host_key_fp=server.get("host_key_fingerprint"),
            auto_install_docker=auto_install_docker,
        )
    except HostKeyMismatch as exc:
        async with user_session(principal.claims) as conn:
            await queries.update_server_state(
                conn, server_id, status="error", status_detail=str(exc)
            )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SSHError as exc:
        async with user_session(principal.claims) as conn:
            await queries.update_server_state(
                conn, server_id, status="error", status_detail=str(exc)
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Persist anything the bootstrap generated or learned.
    if result.generated_private_key:
        async with service_session() as conn:
            await queries.store_server_credentials_privileged(
                conn, server_id, private_key=result.generated_private_key
            )
    if result.password_can_be_discarded and result.status == "online":
        async with service_session() as conn:
            await queries.discard_server_password_privileged(conn, server_id)

    status_map = {
        "online": "online",
        "error": "error",
        "awaiting_key_install": "pending",
    }
    async with user_session(principal.claims) as conn:
        await queries.update_server_state(
            conn,
            server_id,
            status=status_map.get(result.status, "error"),
            status_detail=result.detail,
            host_key_fingerprint=result.host_key_fingerprint,
            docker_version=result.docker_version,
            managed_public_key=result.generated_public_key,
            touch_last_seen=result.status == "online",
        )
        refreshed = await queries.get_server(conn, server_id)

    assert refreshed is not None
    return ServerBootstrapOut(
        server=_to_out(refreshed),
        status=result.status,
        detail=result.detail,
        public_key_to_install=(
            refreshed.get("managed_public_key")
            if result.status == "awaiting_key_install"
            else None
        ),
    )


@router.post("/{server_id}/test", response_model=ServerOut)
async def test_server(
    server_id: UUID, principal: Principal = Depends(current_principal)
) -> ServerOut:
    """Liveness check: connect, confirm Docker still answers, update status."""
    try:
        target = await load_server_target(principal.claims, server_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        conn_ssh = await ssh.pool.get(target)
        info = await docker_remote.probe(conn_ssh)
    except SSHError as exc:
        await ssh.pool.drop(str(server_id))
        async with user_session(principal.claims) as conn:
            await queries.update_server_state(
                conn, server_id, status="offline", status_detail=str(exc)
            )
            row = await queries.get_server(conn, server_id)
        assert row is not None
        return _to_out(row)

    healthy = info.installed and info.usable_by_user
    async with user_session(principal.claims) as conn:
        await queries.update_server_state(
            conn,
            server_id,
            status="online" if healthy else "error",
            status_detail=None if healthy else info.detail,
            docker_version=info.server_version,
            touch_last_seen=healthy,
        )
        row = await queries.get_server(conn, server_id)
    assert row is not None
    return _to_out(row)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: UUID,
    revoke_key: bool = True,
    principal: Principal = Depends(current_principal),
) -> None:
    """Remove a server. By default, also revoke the key we installed on it.

    Revocation is best effort: an unreachable machine must not block the user
    from removing a stale row, but they are told when it did not happen.
    """
    async with user_session(principal.claims) as conn:
        server = await queries.get_server(conn, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found.")

    if revoke_key and server.get("managed_public_key"):
        try:
            target = await load_server_target(principal.claims, server_id)
            conn_ssh = await ssh.pool.get(target)
            await provision.remove_public_key(
                conn_ssh, server["managed_public_key"], server["name"]
            )
        except (SSHError, NotFound) as exc:
            log.warning("could not revoke key on server %s: %s", server_id, exc)

    await ssh.pool.drop(str(server_id))

    async with user_session(principal.claims) as conn:
        deleted = await queries.delete_server(conn, server_id)
    if not deleted:
        raise HTTPException(status_code=403, detail="Not allowed to delete this server.")
