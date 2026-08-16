"""Bridging helpers between "a request about a project" and "a live SSH channel".

Authorization and credential loading are deliberately two separate steps against
two different connections: the caller's RLS-scoped session decides whether they
may see the project at all, and only then does a privileged session decrypt the
key needed to reach it. A route that forgets the first step gets an empty row
rather than someone else's server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncssh

from . import queries, ssh
from .db import service_session, user_session
from .ssh import SSHError, SSHTarget

log = logging.getLogger(__name__)


class NotFound(Exception):
    """Row absent, or hidden from this caller by RLS. Same thing to the client."""


@dataclass
class ProjectContext:
    project: dict[str, Any]
    target: SSHTarget

    @property
    def container(self) -> str:
        name = self.project.get("container_name")
        if not name:
            raise SSHError("This project has no container yet.")
        return str(name)

    @property
    def harness(self) -> str:
        return str(self.project["harness"])


async def load_project_context(claims: dict[str, Any], project_id: UUID) -> ProjectContext:
    async with user_session(claims) as conn:
        project = await queries.get_project(conn, project_id)
    if project is None:
        raise NotFound(f"Project {project_id} not found.")

    async with service_session() as conn:
        target = await queries.load_ssh_target_privileged(conn, project["server_id"])
    if target is None:
        raise NotFound("The server backing this project no longer exists.")

    return ProjectContext(project=project, target=target)


async def load_server_target(claims: dict[str, Any], server_id: UUID) -> SSHTarget:
    async with user_session(claims) as conn:
        server = await queries.get_server(conn, server_id)
    if server is None:
        raise NotFound(f"Server {server_id} not found.")

    async with service_session() as conn:
        target = await queries.load_ssh_target_privileged(conn, server_id)
    if target is None:
        raise NotFound(f"Server {server_id} has no stored credentials.")
    return target


async def connection_for(target: SSHTarget) -> asyncssh.SSHClientConnection:
    """Pooled connection, reconnecting once if the cached one has gone stale."""
    try:
        return await ssh.pool.get(target)
    except SSHError:
        await ssh.pool.drop(target.server_id)
        raise


async def resolve_credential(
    org_id: UUID, project_id: UUID, harness_kind: str
) -> dict[str, Any] | None:
    async with service_session() as conn:
        return await queries.resolve_harness_credential_privileged(
            conn, org_id=org_id, project_id=project_id, harness=harness_kind
        )
