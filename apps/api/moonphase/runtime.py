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
from .profile import (
    VcsCredential,
    WorkspaceProfile,
    credential_from_row,
    profile_from_row,
)
from .ssh import SSHError, SSHTarget

log = logging.getLogger(__name__)


class NotFound(Exception):
    """Row absent, or hidden from this caller by RLS. Same thing to the client."""


class Forbidden(Exception):
    """Visible to this caller, but not at the level this operation needs.

    Distinct from NotFound on purpose. Once someone has been shown a project,
    telling them "you may watch this but not type into it" is more useful than
    pretending it vanished, and discloses nothing they cannot already see.
    """


# What each level may do. A plain ordering would be wrong: 'host' — you own the
# machine, not the project — outranks 'read' for reclaiming resources and sits
# below it for everything to do with the conversation.
CAN_OBSERVE = frozenset({"admin", "write", "read"})
CAN_CONTROL = frozenset({"admin", "write"})
CAN_ADMINISTER = frozenset({"admin"})
CAN_DELETE = frozenset({"admin", "host"})


def _describe(required: frozenset[str], access: str | None) -> str:
    if required is CAN_CONTROL:
        if access == "read":
            return "You have view-only access to this project."
        if access == "host":
            return (
                "This project belongs to someone else; you can see it because it "
                "runs on your server."
            )
        return "You do not have permission to change this project."
    if required is CAN_OBSERVE:
        return (
            "This project belongs to someone else. You can see that it runs on "
            "your server, but not what it is doing."
        )
    return "Only an owner of this project can do that."


@dataclass
class ProjectContext:
    project: dict[str, Any]
    target: SSHTarget

    @property
    def access(self) -> str:
        return str(self.project.get("access") or "read")

    @property
    def container(self) -> str:
        name = self.project.get("container_name")
        if not name:
            raise SSHError("This project has no container yet.")
        return str(name)

    @property
    def harness(self) -> str:
        return str(self.project["harness"])


async def load_project_context(
    claims: dict[str, Any],
    project_id: UUID,
    *,
    require: frozenset[str] = CAN_CONTROL,
) -> ProjectContext:
    """Authorize, then load the credentials needed to act.

    `require` defaults to CAN_CONTROL because most callers are about to do
    something to the container. Read-only routes must ask for CAN_OBSERVE
    explicitly, which makes the weaker check visible at the call site rather
    than implied by its absence.
    """
    async with user_session(claims) as conn:
        project = await queries.get_project(conn, project_id)
    if project is None:
        raise NotFound(f"Project {project_id} not found.")

    access = str(project.get("access") or "")
    if access not in require:
        raise Forbidden(_describe(require, access or None))

    async with service_session() as conn:
        target = await queries.load_ssh_target_privileged(conn, project["server_id"])
    if target is None:
        raise NotFound("The server backing this project no longer exists.")

    return ProjectContext(project=project, target=target)


async def load_server_target(
    claims: dict[str, Any],
    server_id: UUID,
    *,
    require: frozenset[str] = CAN_ADMINISTER,
) -> SSHTarget:
    """A connection to the machine itself.

    Defaults to CAN_ADMINISTER: reaching a server directly, rather than through
    a project on it, is a maintenance operation. Being lent the machine to run
    work on does not make you its administrator.
    """
    async with user_session(claims) as conn:
        server = await queries.get_server(conn, server_id)
    if server is None:
        raise NotFound(f"Server {server_id} not found.")

    access = str(server.get("access") or "")
    if access not in require:
        raise Forbidden(
            "This server is shared with you; only its owner can administer it."
        )

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


async def load_profile(
    claims: dict[str, Any], org_id: UUID, project_id: UUID | None, harness_kind: str
) -> WorkspaceProfile:
    """Assemble the global profile plus whatever credentials apply.

    Settings are read through the caller's RLS session; secrets through a
    service session. A project-specific harness credential wins over the
    org-wide one, so a single project can be pinned to a different account
    without disturbing the global sign-in.
    """
    async with user_session(claims) as conn:
        row = await queries.get_profile(conn, org_id)

    if row is None:
        # The signup trigger creates one, so this only happens for orgs made
        # before the profile migration. Treat it as empty rather than failing.
        row = {"org_id": org_id, "env_vars": {}}

    async with service_session() as conn:
        credential_row = await queries.resolve_harness_credential_privileged(
            conn,
            org_id=org_id,
            project_id=project_id or org_id,
            harness=harness_kind,
        )
        vcs_row = await queries.get_vcs_credential_privileged(conn, org_id, "github")

    vcs = None
    if vcs_row and vcs_row.get("token"):
        vcs = VcsCredential(
            provider=str(vcs_row["provider"]),
            token=str(vcs_row["token"]),
            account=vcs_row.get("account"),
        )

    return profile_from_row(
        row,
        harness_credential=credential_from_row(credential_row),
        vcs_credential=vcs,
    )
