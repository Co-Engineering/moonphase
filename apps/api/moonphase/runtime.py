"""Bridging helpers between "a request about a project" and "a live SSH channel".

Authorization and credential loading are deliberately two separate steps against
two different connections: the caller's RLS-scoped session decides whether they
may see the project at all, and only then does a privileged session decrypt the
key needed to reach it. A route that forgets the first step gets an empty row
rather than someone else's server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import asyncssh

from . import queries, ssh
from .db import service_session, user_session
from .harness import SessionSpace
from .harness import get as get_harness
from .profile import (
    VcsCredential,
    WorkspaceProfile,
    credential_from_row,
    parse_json_object,
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


async def load_session_space(
    claims: dict[str, Any], project_id: UUID, name: str
) -> tuple[SessionSpace, dict[str, Any]]:
    """Where a named session keeps its state, and the row that says so.

    Read from the database rather than derived from the name, because sessions
    created before sessions had owners still live in the shared home and
    checkout, and their transcripts are not where a freshly derived path would
    look for them.
    """
    async with user_session(claims) as conn:
        row = await queries.get_session(conn, project_id, name)
    if row is None:
        raise NotFound(f"No session called {name!r} in this project.")
    return (
        SessionSpace(home=str(row["home_dir"]), workdir=str(row["workdir"])),
        row,
    )


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


class NoCredential(Exception):
    """The person starting this session has not connected the harness."""


def _with_env_layers(
    profile: WorkspaceProfile,
    project_row: dict[str, Any] | None,
    session_row: dict[str, Any] | None,
) -> WorkspaceProfile:
    """Layer project- and session-level env vars over the org profile's.

    Unlike settings/CLAUDE.md/MCP servers this is not routed through the
    harness's `compose_project_layers` — env vars apply the same way
    regardless of which harness a project runs, so this combines them
    directly rather than only for harnesses that know about Claude Code's
    own config shape. Most specific scope wins a key collision, the same
    precedence the Claude-specific fields already use.
    """
    project_env = parse_json_object((project_row or {}).get("env_vars"))
    session_env = parse_json_object((session_row or {}).get("env_vars"))
    if not project_env and not session_env:
        return profile
    return replace(
        profile,
        env_vars={
            **profile.env_vars,
            **{str(k): str(v) for k, v in project_env.items()},
            **{str(k): str(v) for k, v in session_env.items()},
        },
    )


async def load_session_profile(
    claims: dict[str, Any],
    project: dict[str, Any],
    harness_kind: str,
    session: str | None = None,
) -> WorkspaceProfile:
    """Everything one person brings to a session they are about to start.

    Read against the *caller*, not the project. A session belongs to whoever
    started it and runs on their coding subscription — nobody's work may run on
    someone else's account, which is both a licensing matter and the only way
    usage means anything. So the settings, the harness credential and the git
    identity all come from the caller's own organization, and a collaborator
    joining a shared project brings their own.

    RLS is the right scope here for exactly that reason: the caller reading
    their own organization is the whole intent, and a row they cannot see is a
    row that is not theirs to use.

    A project-specific harness credential still wins over the org-wide one, but
    only when the project is the caller's own. Otherwise a project override set
    by its owner would quietly pull their account back into someone else's
    session, which is the thing this exists to prevent.

    Project- and session-level Claude config (settings, CLAUDE.md, MCP
    servers, skills) layer on top of the org profile via the harness itself —
    see `Harness.compose_project_layers` — so a harness with no such concept
    is unaffected. Env vars layer the same way but are combined directly
    here instead, since they apply regardless of harness. `session` is the
    tmux session name; omitted for a session that does not exist yet, which
    simply means no session-level layer.
    """
    async with user_session(claims) as conn:
        org_id = await queries.personal_org_id(conn)
        if org_id is None:
            raise NotFound("You have no personal organization.")
        row = await queries.get_profile(conn, org_id)
        project_row = await queries.get_project_config(conn, project["id"])
        session_row = (
            await queries.get_session_config(conn, project["id"], session)
            if session
            else None
        )

    project_id = project["id"] if project.get("org_id") == org_id else None

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
        mcp_oauth = await queries.get_mcp_oauth_credentials_privileged(conn, org_id)

    vcs = None
    if vcs_row and vcs_row.get("token"):
        vcs = VcsCredential(
            provider=str(vcs_row["provider"]),
            token=str(vcs_row["token"]),
            account=vcs_row.get("account"),
        )

    base = profile_from_row(
        row,
        harness_credential=credential_from_row(credential_row),
        vcs_credential=vcs,
    )
    base.mcp_oauth = mcp_oauth
    composed = get_harness(harness_kind).compose_project_layers(
        base, project_row, session_row
    )
    return _with_env_layers(composed, project_row, session_row)


async def load_session_profile_privileged(
    org_id: UUID, project: dict[str, Any], harness_kind: str, session: str | None = None
) -> WorkspaceProfile:
    """`load_session_profile`, for a caller with no JWT of its own.

    The monitor resuming a session after a reboot is the one caller of this:
    it already knows whose session it is and which org that resolves to (see
    `queries.personal_org_id_for_user_privileged`) from the row it is
    reconciling, not from a request. Every read goes through the privileged
    connection directly — there is no RLS to scope against without a caller,
    so `org_id` must already be verified by whoever called this rather than
    trusted blindly.
    """
    async with service_session() as conn:
        row = await queries.get_profile(conn, org_id)
        project_row = await queries.get_project_config(conn, project["id"])
        session_row = (
            await queries.get_session_config(conn, project["id"], session)
            if session
            else None
        )
        credential_row = await queries.resolve_harness_credential_privileged(
            conn, org_id=org_id, project_id=project["id"], harness=harness_kind
        )
        vcs_row = await queries.get_vcs_credential_privileged(conn, org_id, "github")
        mcp_oauth = await queries.get_mcp_oauth_credentials_privileged(conn, org_id)

    if row is None:
        row = {"org_id": org_id, "env_vars": {}}

    vcs = None
    if vcs_row and vcs_row.get("token"):
        vcs = VcsCredential(
            provider=str(vcs_row["provider"]),
            token=str(vcs_row["token"]),
            account=vcs_row.get("account"),
        )

    base = profile_from_row(
        row,
        harness_credential=credential_from_row(credential_row),
        vcs_credential=vcs,
    )
    base.mcp_oauth = mcp_oauth
    composed = get_harness(harness_kind).compose_project_layers(
        base, project_row, session_row
    )
    return _with_env_layers(composed, project_row, session_row)
