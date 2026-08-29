"""Server management: add, bootstrap, inspect, remove."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .. import docker_remote, provision, queries, ssh
from ..auth import Principal, current_principal
from ..config import get_settings
from ..db import service_session, user_session
from ..runtime import CAN_ADMINISTER, Forbidden, NotFound, load_server_target
from ..schemas import RenameIn, ServerBootstrapOut, ServerCreate, ServerOut
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
    """Create a server row and start bootstrapping it in the background.

    Bootstrapping installs a key, probes for Docker and often installs it, which
    on a cold machine takes minutes. Holding an HTTP request open for that long
    is a bet against every proxy, browser and phone network between here and
    the person waiting — and it is a bet that loses: the browser gave up on a
    bootstrap that went on to succeed, and reported a network error for a server
    that was coming up fine.

    So this returns as soon as the row exists, and the client watches the
    server's status the same way the sidebar already does.
    """
    try:
        payload.validate_credentials()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if (
        not get_settings().moonphase_ssh_trust_on_first_use
        and not payload.expected_host_key_fingerprint
    ):
        raise HTTPException(
            status_code=422,
            detail="MOONPHASE_SSH_TRUST_ON_FIRST_USE is disabled, so a server "
            "cannot be added without its expected host key fingerprint.",
        )

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
                host_key_fingerprint=payload.expected_host_key_fingerprint,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except IntegrityError as exc:
            # Names are unique per organization. Retrying an add after a failure
            # hits this every time, and a raw constraint violation is not an
            # answer anyone can act on.
            if "servers_org_id_name_key" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"You already have a server called “{payload.name.strip()}”.",
                ) from exc
            raise

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

    # Deliberately not awaited. Failures are recorded on the row, which is what
    # the client is watching; nothing here has anywhere better to report them.
    task = asyncio.create_task(
        _bootstrap_in_background(
            principal,
            server_id,
            auto_install_docker=payload.auto_install_docker,
            install_sysbox=payload.auto_install_sysbox,
        )
    )
    _background.add(task)
    task.add_done_callback(_background.discard)

    return ServerBootstrapOut(
        server=_to_out({**row, "status": "bootstrapping"}),
        status="bootstrapping",
        detail="Connecting…",
    )


# Held so the event loop does not collect a task that nothing is awaiting.
_background: set[asyncio.Task] = set()


async def _bootstrap_in_background(
    principal: Principal,
    server_id: UUID,
    *,
    auto_install_docker: bool,
    install_sysbox: bool = False,
) -> None:
    try:
        await _run_bootstrap(
            principal,
            server_id,
            auto_install_docker=auto_install_docker,
            install_sysbox=install_sysbox,
        )
    except Exception as exc:  # noqa: BLE001 — the row is the only place to report
        log.warning("bootstrap of %s failed: %s", server_id, exc)
        async with service_session() as conn:
            await queries.update_server_state(
                conn,
                server_id,
                status="error",
                status_detail=str(exc)[:500],
            )


@router.post("/{server_id}/bootstrap", response_model=ServerBootstrapOut)
async def rerun_bootstrap(
    server_id: UUID,
    auto_install_docker: bool = True,
    install_sysbox: bool = False,
    principal: Principal = Depends(current_principal),
) -> ServerBootstrapOut:
    """Retry onboarding — used after the user installs a managed public key.

    Also doubles as the "Install Sysbox" action on an already-online server:
    call with `install_sysbox=true` to add that capability without a
    dedicated endpoint. It re-verifies Docker in the same pass, same as a
    plain retry would.
    """
    await _require_admin(principal, server_id)
    return await _run_bootstrap(
        principal,
        server_id,
        auto_install_docker=auto_install_docker,
        install_sysbox=install_sysbox,
    )


async def _require_admin(principal: Principal, server_id: UUID) -> dict:
    """Visible is not the same as yours.

    Every route below reaches the machine itself rather than a workload on it,
    so a share is never enough. Checked here rather than relying on
    `load_server_target`, because bootstrap loads its credentials directly and
    would otherwise let anyone the server was shared with re-run it.
    """
    async with user_session(principal.claims) as conn:
        row = await queries.get_server(conn, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    if row.get("access") not in CAN_ADMINISTER:
        raise Forbidden(
            "This server is shared with you. Only its owner can administer it."
        )
    return row


async def _run_bootstrap(
    principal: Principal,
    server_id: UUID,
    *,
    auto_install_docker: bool,
    install_sysbox: bool = False,
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
            install_sysbox=install_sysbox,
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
            sysbox_checked=result.sysbox_checked,
            sysbox_version=result.sysbox_version,
            sysbox_status_detail=result.sysbox_status_detail,
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
    await _require_admin(principal, server_id)
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
    server = await _require_admin(principal, server_id)

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


@router.patch("/{server_id}", response_model=ServerOut)
async def rename_server(
    server_id: UUID,
    payload: RenameIn,
    principal: Principal = Depends(current_principal),
) -> ServerOut:
    """Change what a server is called.

    The name only. Nothing else about a server is safe to edit in place — the
    address, the login and the key are what Moonphase authenticated against, and
    changing one without re-bootstrapping would leave a record that no longer
    describes the machine it points at.
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A server needs a name.")
    if len(name) > 64:
        # The database caps this at 64. Truncating silently would rename
        # it to something nobody typed; the error says the number.
        raise HTTPException(
            status_code=400,
            detail="That name is too long — 64 characters at most.",
        )

    try:
        async with user_session(principal.claims) as conn:
            result = await conn.execute(
                text(
                    "update servers set name = :name "
                    "where id = :id returning id"
                ),
                {"name": name, "id": str(server_id)},
            )
            renamed = result.first()
            row = await queries.get_server(conn, server_id) if renamed else None
    except IntegrityError as exc:
        if "servers_org_id_name_key" in str(exc):
            raise HTTPException(
                status_code=409, detail=f"You already have a server called “{name}”."
            ) from exc
        raise

    if row is None:
        # No row matched, which under RLS means either it does not exist or it
        # is not yours to rename. Which of those is not disclosed.
        raise HTTPException(status_code=404, detail="Server not found.")
    return _to_out(row)
