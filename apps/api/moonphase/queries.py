"""Data access.

Two conventions worth knowing before reading:

* Functions taking a `conn` do not filter by user. That is not an oversight —
  callers pass an RLS-scoped connection from `db.user_session`, and Postgres
  applies the policies. Adding a redundant `where org_id = ...` here would
  encourage the habit of trusting the application layer instead.
* Functions named `*_privileged` require a `db.service_session` and touch the
  `private` schema. They are the only path to plaintext secrets.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .crypto import decrypt, encrypt
from .ssh import SSHTarget

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str, fallback: str = "project") -> str:
    slug = _SLUG_STRIP.sub("-", value.lower()).strip("-")[:48]
    if len(slug) < 2:
        slug = fallback
    if not slug[0].isalnum():
        slug = f"p{slug}"
    return slug


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


async def list_organizations(conn: AsyncConnection) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            select o.id, o.name, o.slug, o.is_personal, o.created_at, m.role
            from organizations o
            join org_members m on m.org_id = o.id and m.user_id = auth.uid()
            order by o.is_personal desc, o.name
            """
        )
    )
    return [_row_to_dict(r) for r in result]


async def personal_org_id(conn: AsyncConnection) -> UUID | None:
    result = await conn.execute(
        text(
            """
            select o.id
            from organizations o
            join org_members m on m.org_id = o.id and m.user_id = auth.uid()
            where o.is_personal
            order by o.created_at
            limit 1
            """
        )
    )
    row = result.first()
    return row[0] if row else None


async def resolve_org(conn: AsyncConnection, org_id: UUID | None) -> UUID:
    """Pick the org for a write, defaulting to the caller's personal org."""
    if org_id is not None:
        check = await conn.execute(
            text("select id from organizations where id = :id"), {"id": org_id}
        )
        if check.first() is None:
            raise PermissionError("Organization not found or not accessible.")
        return org_id
    personal = await personal_org_id(conn)
    if personal is None:
        raise PermissionError("No personal organization exists for this user.")
    return personal


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------


SERVER_COLUMNS = """
    s.id, s.org_id, s.name, s.host, s.port, s.ssh_user, s.ssh_auth_mode,
    s.status, s.status_detail, s.host_key_fingerprint, s.docker_version,
    s.managed_public_key, s.last_seen_at, s.created_at,
    public.server_access(s.id) as access,
    not public.is_org_member(s.org_id) as shared,
    (select count(*) from server_shares sh where sh.server_id = s.id) as share_count
"""


async def list_servers(conn: AsyncConnection) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            f"""
            select {SERVER_COLUMNS},
                   (select count(*) from projects p where p.server_id = s.id) as project_count
            from servers s
            order by s.created_at desc
            """
        )
    )
    return [_row_to_dict(r) for r in result]


async def get_server(conn: AsyncConnection, server_id: UUID) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            f"""
            select {SERVER_COLUMNS},
                   (select count(*) from projects p where p.server_id = s.id) as project_count
            from servers s
            where s.id = :id
            """
        ),
        {"id": server_id},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


async def insert_server(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    name: str,
    host: str,
    port: int,
    ssh_user: str,
    auth_mode: str,
    created_by: str,
    host_key_fingerprint: str | None = None,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into servers
              (org_id, name, host, port, ssh_user, ssh_auth_mode, status, created_by,
               host_key_fingerprint)
            values
              (:org_id, :name, :host, :port, :ssh_user, cast(:auth_mode as ssh_auth_mode),
               'bootstrapping', :created_by, :host_key_fingerprint)
            returning id, org_id, name, host, port, ssh_user, ssh_auth_mode, status,
                      status_detail, host_key_fingerprint, docker_version,
                      managed_public_key, last_seen_at, created_at
            """
        ),
        {
            "org_id": org_id,
            "name": name,
            "host": host,
            "port": port,
            "ssh_user": ssh_user,
            "auth_mode": auth_mode,
            "created_by": created_by,
            "host_key_fingerprint": host_key_fingerprint,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to create a server in this organization.")
    return _row_to_dict(row)


async def update_server_state(
    conn: AsyncConnection,
    server_id: UUID,
    *,
    status: str | None = None,
    status_detail: str | None = None,
    host_key_fingerprint: str | None = None,
    docker_version: str | None = None,
    managed_public_key: str | None = None,
    touch_last_seen: bool = False,
) -> None:
    sets: list[str] = []
    params: dict[str, Any] = {"id": server_id}
    if status is not None:
        sets.append("status = cast(:status as server_status)")
        params["status"] = status
    # status_detail is cleared on success, so an explicit None must be
    # distinguishable from "not provided" — the caller passes status to signal
    # intent and we always write the detail alongside it.
    if status is not None:
        sets.append("status_detail = :status_detail")
        params["status_detail"] = status_detail
    if host_key_fingerprint is not None:
        sets.append("host_key_fingerprint = :fp")
        params["fp"] = host_key_fingerprint
    if docker_version is not None:
        sets.append("docker_version = :dv")
        params["dv"] = docker_version
    if managed_public_key is not None:
        sets.append("managed_public_key = :mpk")
        params["mpk"] = managed_public_key
    if touch_last_seen:
        sets.append("last_seen_at = now()")
    if not sets:
        return
    await conn.execute(
        text(f"update servers set {', '.join(sets)} where id = :id"), params
    )


async def delete_server(conn: AsyncConnection, server_id: UUID) -> bool:
    result = await conn.execute(
        text("delete from servers where id = :id returning id"), {"id": server_id}
    )
    return result.first() is not None


# --- credentials (privileged) ----------------------------------------------


async def store_server_credentials_privileged(
    conn: AsyncConnection,
    server_id: UUID,
    *,
    private_key: str | None = None,
    passphrase: str | None = None,
    password: str | None = None,
) -> None:
    await conn.execute(
        text(
            """
            insert into private.server_credentials
              (server_id, private_key_enc, passphrase_enc, password_enc)
            values (:server_id, :pk, :pp, :pw)
            on conflict (server_id) do update set
              private_key_enc = coalesce(excluded.private_key_enc,
                                         private.server_credentials.private_key_enc),
              passphrase_enc  = coalesce(excluded.passphrase_enc,
                                         private.server_credentials.passphrase_enc),
              password_enc    = excluded.password_enc
            """
        ),
        {
            "server_id": server_id,
            "pk": encrypt(private_key),
            "pp": encrypt(passphrase),
            "pw": encrypt(password),
        },
    )


async def discard_server_password_privileged(
    conn: AsyncConnection, server_id: UUID
) -> None:
    """Destroy the bootstrap password once key login is proven."""
    await conn.execute(
        text(
            "update private.server_credentials set password_enc = null "
            "where server_id = :id"
        ),
        {"id": server_id},
    )


async def load_ssh_target_privileged(
    conn: AsyncConnection, server_id: UUID
) -> SSHTarget | None:
    """Decrypt a server's credentials into a connectable target."""
    result = await conn.execute(
        text(
            """
            select s.id, s.host, s.port, s.ssh_user, s.host_key_fingerprint,
                   c.private_key_enc, c.passphrase_enc, c.password_enc
            from servers s
            left join private.server_credentials c on c.server_id = s.id
            where s.id = :id
            """
        ),
        {"id": server_id},
    )
    row = result.first()
    if row is None:
        return None
    data = _row_to_dict(row)
    return SSHTarget(
        server_id=str(data["id"]),
        host=data["host"],
        port=data["port"],
        username=data["ssh_user"],
        private_key=decrypt(data["private_key_enc"]),
        passphrase=decrypt(data["passphrase_enc"]),
        password=decrypt(data["password_enc"]),
        known_host_key_fp=data["host_key_fingerprint"],
    )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


PROJECT_COLUMNS = """
    p.id, p.org_id, p.server_id, p.name, p.slug, p.harness, p.environment, p.repo_url,
    p.container_name, p.container_id, p.workspace_volume, p.home_volume,
    p.status, p.status_detail, p.preview_port, p.preview_url, p.created_at,
    -- Scoped to the caller's own sessions. In a shared project "waiting for
    -- you" must mean you: someone else's agent needing its owner is not a
    -- thing you can act on, and a sidebar dot you cannot clear is noise.
    coalesce(
        (select s.activity::text from project_sessions s
         where s.project_id = p.id and s.user_id = auth.uid()
         order by case s.activity
                    when 'awaiting_input' then 0
                    when 'working' then 1
                    when 'idle' then 2
                    else 3
                  end, s.created_at
         limit 1),
        'unknown'
    ) as activity,
    (select s.activity_detail from project_sessions s
     where s.project_id = p.id and s.user_id = auth.uid()
     order by case s.activity
                when 'awaiting_input' then 0
                when 'working' then 1
                when 'idle' then 2
                else 3
              end, s.created_at
     limit 1) as activity_detail,
    (select max(s.activity_at) from project_sessions s
     where s.project_id = p.id and s.user_id = auth.uid()) as activity_at,
    (select max(s.checked_at) from project_sessions s
     where s.project_id = p.id and s.user_id = auth.uid()) as checked_at,
    public.project_access(p.id) as access,
    not public.is_org_member(p.org_id) as shared,
    (select count(*) from project_shares ph where ph.project_id = p.id) as share_count,
    -- Not `servers.name`: a project shared with you may live on a machine you
    -- have no access to, and joining the row would either leak its address or
    -- drop the project from your list entirely.
    public.server_label(p.server_id) as server_name
"""


async def list_projects(
    conn: AsyncConnection, server_id: UUID | None = None
) -> list[dict[str, Any]]:
    clause = "where p.server_id = :server_id" if server_id else ""
    result = await conn.execute(
        text(
            f"""
            select {PROJECT_COLUMNS}
            from projects p
            {clause}
            order by p.created_at desc
            """
        ),
        {"server_id": server_id} if server_id else {},
    )
    return [_row_to_dict(r) for r in result]


async def get_project(conn: AsyncConnection, project_id: UUID) -> dict[str, Any] | None:
    result = await conn.execute(
        text(f"select {PROJECT_COLUMNS} from projects p where p.id = :id"),
        {"id": project_id},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------

# Both tables have the same shape, and the API surface for them is identical.
# Keeping one implementation parameterised by table beats two that drift.
_SHARE_TABLES = {
    "server": ("server_shares", "server_id"),
    "project": ("project_shares", "project_id"),
}

SHARE_COLUMNS = "id, user_id, email, role::text as role, created_at"


def _share_table(kind: str) -> tuple[str, str]:
    try:
        return _SHARE_TABLES[kind]
    except KeyError:
        raise ValueError(f"Not a shareable resource: {kind!r}") from None


async def access_level(
    conn: AsyncConnection, kind: str, resource_id: UUID
) -> str | None:
    """What the caller may do with this resource, straight from the database.

    The same function the RLS policies use, so a route cannot be more
    permissive than the row-level rules it is running under.
    """
    fn = "server_access" if kind == "server" else "project_access"
    result = await conn.execute(
        text(f"select public.{fn}(:id)"), {"id": resource_id}
    )
    return result.scalar_one_or_none()


async def list_shares(
    conn: AsyncConnection, kind: str, resource_id: UUID
) -> list[dict[str, Any]]:
    table, column = _share_table(kind)
    result = await conn.execute(
        text(
            f"select {SHARE_COLUMNS} from {table} where {column} = :id "
            "order by created_at"
        ),
        {"id": resource_id},
    )
    return [_row_to_dict(r) for r in result]


async def upsert_share(
    conn: AsyncConnection,
    kind: str,
    resource_id: UUID,
    *,
    email: str,
    user_id: str | None,
    role: str,
    created_by: str,
) -> dict[str, Any]:
    """Grant access, or change the role of an existing grant.

    `user_id` is null when the invitee has no account yet; a trigger on
    auth.users fills it in when they sign up.
    """
    table, column = _share_table(kind)
    result = await conn.execute(
        text(
            f"""
            insert into {table} ({column}, user_id, email, role, created_by)
            values (:resource, cast(:user_id as uuid), :email,
                    cast(:role as share_role), :created_by)
            on conflict ({column}, lower(email)) do update set
              role    = excluded.role,
              user_id = coalesce({table}.user_id, excluded.user_id)
            returning {SHARE_COLUMNS}
            """
        ),
        {
            "resource": resource_id,
            "user_id": user_id,
            "email": email,
            "role": role,
            "created_by": created_by,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to share this.")
    return _row_to_dict(row)


async def update_share_role(
    conn: AsyncConnection, kind: str, resource_id: UUID, share_id: UUID, role: str
) -> dict[str, Any] | None:
    table, column = _share_table(kind)
    result = await conn.execute(
        text(
            f"""
            update {table} set role = cast(:role as share_role)
            where id = :share_id and {column} = :resource
            returning {SHARE_COLUMNS}
            """
        ),
        {"role": role, "share_id": share_id, "resource": resource_id},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


async def delete_share(
    conn: AsyncConnection, kind: str, resource_id: UUID, share_id: UUID
) -> bool:
    table, column = _share_table(kind)
    result = await conn.execute(
        text(
            f"delete from {table} where id = :share_id and {column} = :resource "
            "returning id"
        ),
        {"share_id": share_id, "resource": resource_id},
    )
    return result.first() is not None


async def find_user_by_email_privileged(
    conn: AsyncConnection, email: str
) -> dict[str, Any] | None:
    """Resolve an invitee. Requires a service session: auth.users is not ours.

    Whether this returns anything is observable to the person sharing — they
    see "invited" rather than "shared" — which does disclose that an address
    has an account here. On a self-hosted instance among colleagues that is a
    fair trade for not silently dropping invitations.
    """
    result = await conn.execute(
        text("select id, email from auth.users where lower(email) = lower(:e) limit 1"),
        {"e": email},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


async def is_org_member_by_user(
    conn: AsyncConnection, org_id: UUID, user_id: str
) -> bool:
    """Used to explain "they are already on your team" rather than sharing."""
    result = await conn.execute(
        text(
            "select 1 from org_members where org_id = :o and user_id = cast(:u as uuid)"
        ),
        {"o": org_id, "u": user_id},
    )
    return result.first() is not None


async def slug_is_free(conn: AsyncConnection, server_id: UUID, slug: str) -> bool:
    result = await conn.execute(
        text("select 1 from projects where server_id = :s and slug = :slug"),
        {"s": server_id, "slug": slug},
    )
    return result.first() is None


async def insert_project(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    server_id: UUID,
    name: str,
    slug: str,
    harness: str,
    environment: str,
    repo_url: str | None,
    container_name: str,
    workspace_volume: str,
    home_volume: str,
    preview_port: int | None,
    created_by: str,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into projects
              (org_id, server_id, name, slug, harness, environment, repo_url,
               container_name, workspace_volume, home_volume, preview_port,
               status, created_by)
            values
              (:org_id, :server_id, :name, :slug, cast(:harness as harness_kind),
               :environment, :repo_url, :container_name, :workspace_volume,
               :home_volume, :preview_port, 'creating', :created_by)
            returning id, org_id, server_id, name, slug, harness, environment,
                      repo_url, container_name, container_id, workspace_volume,
                      home_volume, status, status_detail, preview_port,
                      preview_url, created_at
            """
        ),
        {
            "org_id": org_id,
            "server_id": server_id,
            "name": name,
            "slug": slug,
            "harness": harness,
            "environment": environment,
            "repo_url": repo_url,
            "container_name": container_name,
            "workspace_volume": workspace_volume,
            "home_volume": home_volume,
            "preview_port": preview_port,
            "created_by": created_by,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to create a project in this organization.")
    return _row_to_dict(row)


async def set_project_container(
    conn: AsyncConnection,
    project_id: UUID,
    *,
    container_name: str,
    workspace_volume: str,
    home_volume: str,
) -> None:
    """Fill in the container naming, which depends on the id we just generated."""
    await conn.execute(
        text(
            "update projects set container_name = :c, workspace_volume = :w, "
            "home_volume = :h where id = :id"
        ),
        {
            "c": container_name,
            "w": workspace_volume,
            "h": home_volume,
            "id": project_id,
        },
    )


async def update_project_state(
    conn: AsyncConnection,
    project_id: UUID,
    *,
    status: str | None = None,
    status_detail: str | None = None,
    container_id: str | None = None,
    preview_url: str | None = None,
) -> None:
    sets: list[str] = []
    params: dict[str, Any] = {"id": project_id}
    if status is not None:
        sets.append("status = cast(:status as project_status)")
        params["status"] = status
        sets.append("status_detail = :detail")
        params["detail"] = status_detail
    if container_id is not None:
        sets.append("container_id = :cid")
        params["cid"] = container_id
    if preview_url is not None:
        sets.append("preview_url = :purl")
        params["purl"] = preview_url
    if not sets:
        return
    await conn.execute(
        text(f"update projects set {', '.join(sets)} where id = :id"), params
    )


async def delete_project(conn: AsyncConnection, project_id: UUID) -> bool:
    result = await conn.execute(
        text("delete from projects where id = :id returning id"), {"id": project_id}
    )
    return result.first() is not None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


SESSION_COLUMNS = """
    id, project_id, tmux_session, harness, state, started_at, last_attached_at,
    transcript_path, user_id, workdir, home_dir, branch, activity_at, checked_at,
    activity::text as activity, activity_detail, display_name,
    (user_id = auth.uid()) as is_mine,
    public.session_owner_label(user_id) as owner
"""


async def upsert_session(
    conn: AsyncConnection,
    *,
    project_id: UUID,
    harness: str,
    tmux_session: str,
    state: str,
    user_id: str,
    workdir: str,
    home_dir: str,
    branch: str | None = None,
    transcript_path: str | None = None,
    mark_started: bool = False,
) -> dict[str, Any]:
    """Create or refresh a session row.

    `user_id` is not defaulted and not optional: a session with no owner is a
    session running on nobody's account, and the whole point of the ownership
    column is that there is no such thing. The insert policy checks it matches
    the caller, so passing someone else's is refused by the database.
    """
    result = await conn.execute(
        text(
            f"""
            insert into project_sessions
              (project_id, tmux_session, harness, state, transcript_path,
               user_id, workdir, home_dir, branch, started_at)
            values
              (:project_id, :tmux_session, cast(:harness as harness_kind), :state,
               :transcript_path, cast(:user_id as uuid), :workdir, :home_dir,
               :branch, case when :mark_started then now() else null end)
            on conflict (project_id, tmux_session) do update set
              state = excluded.state,
              transcript_path = coalesce(excluded.transcript_path,
                                         project_sessions.transcript_path),
              workdir  = excluded.workdir,
              home_dir = excluded.home_dir,
              branch   = coalesce(excluded.branch, project_sessions.branch),
              started_at = case when :mark_started then now()
                                else project_sessions.started_at end
            returning {SESSION_COLUMNS}
            """
        ),
        {
            "project_id": project_id,
            "tmux_session": tmux_session,
            "harness": harness,
            "state": state,
            "transcript_path": transcript_path,
            "user_id": user_id,
            "workdir": workdir,
            "home_dir": home_dir,
            "branch": branch,
            "mark_started": mark_started,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError(
            "Not allowed to run a session in this project, or that session "
            "belongs to someone else."
        )
    return _row_to_dict(row)


async def list_all_sessions(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Every session in every project the caller can see, in one query.

    The sidebar lists sessions under their projects, and asking per project
    would be one request each. This touches only the database — no SSH, no
    container — because listing what exists must not cost a connection to a
    machine that might be asleep.
    """
    result = await conn.execute(
        text(
            f"""
            select {SESSION_COLUMNS},
                   (select p.name from projects p
                    where p.id = project_sessions.project_id) as project_name
            from project_sessions
            order by project_id, (user_id = auth.uid()) desc, created_at
            """
        )
    )
    return [_row_to_dict(r) for r in result]


async def get_session(
    conn: AsyncConnection, project_id: UUID, tmux_session: str
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            f"select {SESSION_COLUMNS} from project_sessions "
            "where project_id = :pid and tmux_session = :ts"
        ),
        {"pid": project_id, "ts": tmux_session},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


async def rename_session(
    conn: AsyncConnection, project_id: UUID, tmux_session: str, display_name: str
) -> dict[str, Any] | None:
    """Change what a session is called. Not `tmux_session`, `workdir` or
    `branch` — those are what the session *is*, derived once at creation and
    load-bearing afterward; this is only the label shown in the sidebar.
    """
    result = await conn.execute(
        text(
            f"""
            update project_sessions set display_name = :display_name
            where project_id = :pid and tmux_session = :ts
            returning {SESSION_COLUMNS}
            """
        ),
        {"display_name": display_name, "pid": project_id, "ts": tmux_session},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


async def get_sessions(conn: AsyncConnection, project_id: UUID) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            f"""
            select {SESSION_COLUMNS}
            from project_sessions
            where project_id = :pid
            -- Yours first: in a shared project the list is mostly other
            -- people's, and the one you can actually type into should not be
            -- something you have to hunt for.
            order by (user_id = auth.uid()) desc, created_at, tmux_session
            """
        ),
        {"pid": project_id},
    )
    return [_row_to_dict(r) for r in result]


async def delete_session_row(
    conn: AsyncConnection, project_id: UUID, tmux_session: str
) -> bool:
    result = await conn.execute(
        text(
            "delete from project_sessions "
            "where project_id = :pid and tmux_session = :ts returning id"
        ),
        {"pid": project_id, "ts": tmux_session},
    )
    return result.first() is not None


async def mark_sessions_stopped(conn: AsyncConnection, project_id: UUID) -> None:
    """Whole container went down, so every session in it did too."""
    await conn.execute(
        text("update project_sessions set state = 'stopped' where project_id = :pid"),
        {"pid": project_id},
    )


async def count_sessions(conn: AsyncConnection, project_id: UUID) -> int:
    result = await conn.execute(
        text("select count(*) from project_sessions where project_id = :pid"),
        {"pid": project_id},
    )
    return int(result.scalar_one())


async def touch_attached(
    conn: AsyncConnection, project_id: UUID, tmux_session: str
) -> None:
    await conn.execute(
        text(
            "update project_sessions set last_attached_at = now(), state = 'running' "
            "where project_id = :pid and tmux_session = :ts"
        ),
        {"pid": project_id, "ts": tmux_session},
    )


# ---------------------------------------------------------------------------
# Harness credentials (privileged)
# ---------------------------------------------------------------------------


async def upsert_harness_credential_privileged(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    project_id: UUID | None,
    harness: str,
    auth_mode: str,
    label: str | None,
    api_key: str | None,
    oauth_blob: str | None,
    created_by: str,
    oauth_token: str | None = None,
) -> dict[str, Any]:
    if project_id is not None:
        # One credential per (project, harness); replacing is the common case.
        await conn.execute(
            text(
                "delete from private.harness_credentials "
                "where project_id = :pid and harness = cast(:h as harness_kind)"
            ),
            {"pid": project_id, "h": harness},
        )
    # The org-wide row is replaced in place. It used to be a plain insert, which
    # meant signing in again appended a second row and resolution could keep
    # handing back the old one — "I signed in and nothing changed", with the UI
    # insisting it had worked.
    result = await conn.execute(
        text(
            """
            insert into private.harness_credentials
              (org_id, project_id, harness, auth_mode, label, api_key_enc,
               oauth_token_enc, oauth_blob_enc, created_by)
            values
              (:org_id, :project_id, cast(:harness as harness_kind),
               cast(:auth_mode as harness_auth_mode), :label, :api_key,
               :oauth_token, :oauth, :created_by)
            on conflict (org_id, harness) where project_id is null do update set
              auth_mode       = excluded.auth_mode,
              label           = excluded.label,
              api_key_enc     = excluded.api_key_enc,
              oauth_token_enc = excluded.oauth_token_enc,
              oauth_blob_enc  = excluded.oauth_blob_enc,
              created_by      = excluded.created_by
            returning id, org_id, project_id, harness, auth_mode, label, created_at
            """
        ),
        {
            "org_id": org_id,
            "project_id": project_id,
            "harness": harness,
            "auth_mode": auth_mode,
            "label": label,
            "api_key": encrypt(api_key),
            "oauth_token": encrypt(oauth_token),
            "oauth": encrypt(oauth_blob),
            "created_by": created_by,
        },
    )
    row = result.first()
    if row is None:
        raise RuntimeError("Failed to store harness credential.")
    return _row_to_dict(row)


async def resolve_harness_credential_privileged(
    conn: AsyncConnection, *, org_id: UUID, project_id: UUID, harness: str
) -> dict[str, Any] | None:
    """Project-specific credential if there is one, else the org default."""
    result = await conn.execute(
        text(
            """
            select auth_mode, api_key_enc, oauth_token_enc, oauth_blob_enc
            from private.harness_credentials
            where harness = cast(:h as harness_kind)
              and (project_id = :pid or (project_id is null and org_id = :org_id))
            -- Project-specific first, then newest. The tiebreaker is belt and
            -- braces now that a unique index rules out sibling org-wide rows.
            order by project_id nulls last, updated_at desc
            limit 1
            """
        ),
        {"h": harness, "pid": project_id, "org_id": org_id},
    )
    row = result.first()
    if row is None:
        return None
    data = _row_to_dict(row)
    return {
        "auth_mode": data["auth_mode"],
        "api_key": decrypt(data["api_key_enc"]),
        "oauth_token": decrypt(data["oauth_token_enc"]),
        "oauth_blob": decrypt(data["oauth_blob_enc"]),
    }


# ---------------------------------------------------------------------------
# Workspace profile
# ---------------------------------------------------------------------------


async def get_profile(conn: AsyncConnection, org_id: UUID) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            select id, org_id, claude_settings_json, claude_md, mcp_json, skills_json,
                   env_vars, git_user_name, git_user_email, created_at, updated_at
            from workspace_profiles
            where org_id = :org_id
            """
        ),
        {"org_id": org_id},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


async def upsert_profile(
    conn: AsyncConnection,
    org_id: UUID,
    *,
    claude_settings_json: str | None,
    claude_md: str | None,
    mcp_json: str | None,
    skills: dict[str, str],
    env_vars: dict[str, str],
    git_user_name: str | None,
    git_user_email: str | None,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into workspace_profiles
              (org_id, claude_settings_json, claude_md, mcp_json, skills_json,
               env_vars, git_user_name, git_user_email)
            values
              (:org_id, :settings, :claude_md, :mcp, cast(:skills as jsonb),
               cast(:env as jsonb), :git_name, :git_email)
            on conflict (org_id) do update set
              claude_settings_json = excluded.claude_settings_json,
              claude_md            = excluded.claude_md,
              mcp_json             = excluded.mcp_json,
              skills_json          = excluded.skills_json,
              env_vars             = excluded.env_vars,
              git_user_name        = excluded.git_user_name,
              git_user_email       = excluded.git_user_email
            returning id, org_id, claude_settings_json, claude_md, mcp_json, skills_json,
                      env_vars, git_user_name, git_user_email, created_at, updated_at
            """
        ),
        {
            "org_id": org_id,
            "settings": claude_settings_json,
            "claude_md": claude_md,
            "mcp": mcp_json,
            "skills": json.dumps(skills),
            "env": json.dumps(env_vars),
            "git_name": git_user_name,
            "git_email": git_user_email,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to edit this organization's profile.")
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Claude config, scoped to a project or a single session
#
# Same four fields as workspace_profiles (claude_settings_json, claude_md,
# mcp_json, skills_json), one layer down. RLS on the underlying table is what
# actually enforces who may write these — a project needs admin/write access,
# a session additionally needs to be yours — so these functions are thin.
# ---------------------------------------------------------------------------

CLAUDE_CONFIG_COLUMNS = "claude_settings_json, claude_md, mcp_json, skills_json"


async def get_project_config(
    conn: AsyncConnection, project_id: UUID
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(f"select {CLAUDE_CONFIG_COLUMNS} from projects where id = :id"),
        {"id": project_id},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


async def update_project_config(
    conn: AsyncConnection,
    project_id: UUID,
    *,
    claude_settings_json: str | None,
    claude_md: str | None,
    mcp_json: str | None,
    skills: dict[str, str],
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            f"""
            update projects set
              claude_settings_json = :settings,
              claude_md            = :claude_md,
              mcp_json             = :mcp,
              skills_json           = cast(:skills as jsonb)
            where id = :id
            returning {CLAUDE_CONFIG_COLUMNS}
            """
        ),
        {
            "id": project_id,
            "settings": claude_settings_json,
            "claude_md": claude_md,
            "mcp": mcp_json,
            "skills": json.dumps(skills),
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to edit this project's configuration.")
    return _row_to_dict(row)


async def get_session_config(
    conn: AsyncConnection, project_id: UUID, tmux_session: str
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            f"select {CLAUDE_CONFIG_COLUMNS} from project_sessions "
            "where project_id = :pid and tmux_session = :ts"
        ),
        {"pid": project_id, "ts": tmux_session},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


async def update_session_config(
    conn: AsyncConnection,
    project_id: UUID,
    tmux_session: str,
    *,
    claude_settings_json: str | None,
    claude_md: str | None,
    mcp_json: str | None,
    skills: dict[str, str],
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            f"""
            update project_sessions set
              claude_settings_json = :settings,
              claude_md            = :claude_md,
              mcp_json             = :mcp,
              skills_json           = cast(:skills as jsonb)
            where project_id = :pid and tmux_session = :ts
            returning {CLAUDE_CONFIG_COLUMNS}
            """
        ),
        {
            "pid": project_id,
            "ts": tmux_session,
            "settings": claude_settings_json,
            "claude_md": claude_md,
            "mcp": mcp_json,
            "skills": json.dumps(skills),
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError(
            "Not allowed to edit this session's configuration, or it does not exist."
        )
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Push subscriptions
# ---------------------------------------------------------------------------


async def upsert_push_subscription(
    conn: AsyncConnection,
    *,
    user_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> None:
    await conn.execute(
        text(
            """
            insert into push_subscriptions
              (user_id, endpoint, p256dh, auth, user_agent)
            values (cast(:user_id as uuid), :endpoint, :p256dh, :auth, :user_agent)
            on conflict (endpoint) do update set
              user_id    = excluded.user_id,
              p256dh     = excluded.p256dh,
              auth       = excluded.auth,
              user_agent = excluded.user_agent
            """
        ),
        {
            "user_id": user_id,
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": user_agent,
        },
    )


async def delete_push_subscription(conn: AsyncConnection, endpoint: str) -> None:
    await conn.execute(
        text("delete from push_subscriptions where endpoint = :e"), {"e": endpoint}
    )


async def list_own_push_subscriptions(conn: AsyncConnection) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            "select endpoint, p256dh, auth from push_subscriptions "
            "where user_id = auth.uid()"
        )
    )
    return [_row_to_dict(r) for r in result]


async def has_push_subscription(conn: AsyncConnection) -> bool:
    result = await conn.execute(
        text("select 1 from push_subscriptions where user_id = auth.uid() limit 1")
    )
    return result.first() is not None


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


async def list_environments(conn: AsyncConnection) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            select id, org_id, key, display_name, description, base_image,
                   setup_script, created_at, updated_at
            from environments
            order by display_name
            """
        )
    )
    return [_row_to_dict(r) for r in result]


async def upsert_environment(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    key: str,
    display_name: str,
    description: str | None,
    base_image: str,
    setup_script: str | None,
    created_by: str,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into environments
              (org_id, key, display_name, description, base_image, setup_script,
               created_by)
            values
              (:org_id, :key, :display_name, :description, :base_image,
               :setup_script, :created_by)
            on conflict (org_id, key) do update set
              display_name = excluded.display_name,
              description  = excluded.description,
              base_image   = excluded.base_image,
              setup_script = excluded.setup_script
            returning id, org_id, key, display_name, description, base_image,
                      setup_script, created_at, updated_at
            """
        ),
        {
            "org_id": org_id,
            "key": key,
            "display_name": display_name,
            "description": description,
            "base_image": base_image,
            "setup_script": setup_script,
            "created_by": created_by,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to define environments in this organization.")
    return _row_to_dict(row)


async def delete_environment(conn: AsyncConnection, org_id: UUID, key: str) -> bool:
    result = await conn.execute(
        text(
            "delete from environments where org_id = :org_id and key = :key "
            "returning id"
        ),
        {"org_id": org_id, "key": key},
    )
    return result.first() is not None


async def count_projects_using_environment(
    conn: AsyncConnection, org_id: UUID, key: str
) -> int:
    result = await conn.execute(
        text(
            "select count(*) from projects where org_id = :org_id and environment = :key"
        ),
        {"org_id": org_id, "key": key},
    )
    return int(result.scalar_one())


async def environment_usage(conn: AsyncConnection, org_id: UUID) -> dict[str, int]:
    """Project counts for every environment at once.

    One grouped query rather than one per environment: the catalogue grows
    with what users define, and a listing should not get slower as it does.
    """
    result = await conn.execute(
        text(
            "select environment, count(*) as n from projects "
            "where org_id = :org_id group by environment"
        ),
        {"org_id": org_id},
    )
    return {str(row[0]): int(row[1]) for row in result}


# ---------------------------------------------------------------------------
# VCS credentials (privileged)
# ---------------------------------------------------------------------------


async def upsert_vcs_credential_privileged(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    provider: str,
    auth_mode: str,
    account: str | None,
    scopes: str | None,
    token: str,
    created_by: str,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into private.vcs_credentials
              (org_id, provider, auth_mode, account, scopes, token_enc, created_by)
            values
              (:org_id, cast(:provider as vcs_provider),
               cast(:auth_mode as vcs_auth_mode), :account, :scopes, :token, :created_by)
            on conflict (org_id, provider) do update set
              auth_mode  = excluded.auth_mode,
              account    = excluded.account,
              scopes     = excluded.scopes,
              token_enc  = excluded.token_enc
            returning id, org_id, provider, auth_mode, account, scopes, created_at
            """
        ),
        {
            "org_id": org_id,
            "provider": provider,
            "auth_mode": auth_mode,
            "account": account,
            "scopes": scopes,
            "token": encrypt(token),
            "created_by": created_by,
        },
    )
    row = result.first()
    if row is None:
        raise RuntimeError("Failed to store the version-control credential.")
    return _row_to_dict(row)


async def get_vcs_credential_privileged(
    conn: AsyncConnection, org_id: UUID, provider: str = "github"
) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            """
            select provider, auth_mode, account, scopes, token_enc, created_at
            from private.vcs_credentials
            where org_id = :org_id and provider = cast(:provider as vcs_provider)
            """
        ),
        {"org_id": org_id, "provider": provider},
    )
    row = result.first()
    if row is None:
        return None
    data = _row_to_dict(row)
    data["token"] = decrypt(data.pop("token_enc"))
    return data


async def delete_vcs_credential_privileged(
    conn: AsyncConnection, org_id: UUID, provider: str = "github"
) -> None:
    await conn.execute(
        text(
            "delete from private.vcs_credentials "
            "where org_id = :org_id and provider = cast(:provider as vcs_provider)"
        ),
        {"org_id": org_id, "provider": provider},
    )


async def delete_harness_credential_privileged(
    conn: AsyncConnection, org_id: UUID, harness: str
) -> None:
    """Remove the org-wide credential, leaving per-project overrides alone."""
    await conn.execute(
        text(
            "delete from private.harness_credentials "
            "where org_id = :org_id and harness = cast(:h as harness_kind) "
            "and project_id is null"
        ),
        {"org_id": org_id, "h": harness},
    )


# ---------------------------------------------------------------------------
# MCP server OAuth credentials
#
# One per (org, server name), org-wide only — a given org typically has one
# identity for a third-party service, and a per-project override would mean
# re-authenticating the same server once per project for no benefit. See the
# migration for what `credential_json` actually holds.
# ---------------------------------------------------------------------------


async def get_mcp_oauth_credentials_privileged(
    conn: AsyncConnection, org_id: UUID
) -> dict[str, str]:
    """Every connected MCP server for this org, as {server_name: credential_json}."""
    result = await conn.execute(
        text(
            "select server_name, credential_json_enc from private.mcp_oauth_credentials "
            "where org_id = :org_id"
        ),
        {"org_id": org_id},
    )
    return {str(row[0]): decrypt(row[1]) or "" for row in result}


async def list_mcp_oauth_credentials_privileged(
    conn: AsyncConnection, org_id: UUID
) -> list[dict[str, Any]]:
    """Metadata only, for a "connected servers" list — never the token itself."""
    result = await conn.execute(
        text(
            "select id, server_name, created_at, updated_at "
            "from private.mcp_oauth_credentials where org_id = :org_id "
            "order by server_name"
        ),
        {"org_id": org_id},
    )
    return [_row_to_dict(r) for r in result]


async def upsert_mcp_oauth_credential_privileged(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    server_name: str,
    credential_json: str,
    created_by: str | None,
) -> None:
    await conn.execute(
        text(
            """
            insert into private.mcp_oauth_credentials
              (org_id, server_name, credential_json_enc, created_by)
            values (:org_id, :name, :cred, cast(:created_by as uuid))
            on conflict (org_id, server_name) do update set
              credential_json_enc = excluded.credential_json_enc,
              created_by      = excluded.created_by
            """
        ),
        {
            "org_id": org_id,
            "name": server_name,
            "cred": encrypt(credential_json),
            "created_by": created_by,
        },
    )


async def delete_mcp_oauth_credential_privileged(
    conn: AsyncConnection, org_id: UUID, server_name: str
) -> None:
    await conn.execute(
        text(
            "delete from private.mcp_oauth_credentials "
            "where org_id = :org_id and server_name = :name"
        ),
        {"org_id": org_id, "name": server_name},
    )


async def list_harness_credentials(
    conn: AsyncConnection, org_ids: list[UUID]
) -> list[dict[str, Any]]:
    """Metadata only — never the material itself."""
    if not org_ids:
        return []
    result = await conn.execute(
        text(
            """
            select id, org_id, project_id, harness, auth_mode, label, created_at
            from private.harness_credentials
            where org_id = any(:org_ids)
            order by created_at desc
            """
        ),
        {"org_ids": org_ids},
    )
    return [_row_to_dict(r) for r in result]


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


async def record_usage_privileged(
    conn: AsyncConnection,
    *,
    user_id: str,
    project_id: UUID,
    project_name: str,
    session_id: UUID,
    events: list[Any],
) -> int:
    """Store what a session consumed. Re-reading a transcript is harmless.

    Conflicts are ignored rather than updated: the provider's count for a
    message does not change, so a row that already exists is the same row.
    """
    if not events:
        return 0
    await conn.execute(
        text(
            """
            insert into usage_events
              (user_id, project_id, project_name, session_id, model, message_id,
               at, input_tokens, output_tokens, cache_read_tokens,
               cache_write_5m_tokens, cache_write_1h_tokens, thinking_tokens)
            values
              (cast(:user_id as uuid), :project_id, :project_name, :session_id,
               :model, :message_id, :at, :input_tokens, :output_tokens,
               :cache_read_tokens, :cache_write_5m_tokens, :cache_write_1h_tokens,
               :thinking_tokens)
            on conflict (user_id, message_id) do nothing
            """
        ),
        [
            {
                "user_id": user_id,
                "project_id": project_id,
                "project_name": project_name,
                "session_id": session_id,
                "model": event.model,
                "message_id": event.message_id,
                "at": event.at,
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "cache_read_tokens": event.cache_read_tokens,
                "cache_write_5m_tokens": event.cache_write_5m_tokens,
                "cache_write_1h_tokens": event.cache_write_1h_tokens,
                "thinking_tokens": event.thinking_tokens,
            }
            for event in events
        ],
    )
    return len(events)


async def set_usage_cursors_privileged(
    conn: AsyncConnection, *, session_id: UUID, cursors: dict[str, int]
) -> None:
    """Remember where to resume in each transcript.

    Replaced wholesale rather than merged: a file that is no longer in the
    directory should stop being tracked, and merging would keep every
    transcript a session has ever had in the row forever.
    """
    await conn.execute(
        text(
            "update project_sessions set usage_cursors = cast(:c as jsonb) "
            "where id = :id"
        ),
        {"c": json.dumps(cursors), "id": session_id},
    )


# Every tier, summed the same way in each query. Written once because a typo in
# one of three copies would show a plausible number that is quietly wrong.
USAGE_SUMS = (
    "sum(input_tokens) as input_tokens, "
    "sum(output_tokens) as output_tokens, "
    "sum(cache_read_tokens) as cache_read_tokens, "
    "sum(cache_write_5m_tokens) as cache_write_5m_tokens, "
    "sum(cache_write_1h_tokens) as cache_write_1h_tokens, "
    "sum(thinking_tokens) as thinking_tokens"
)


async def usage_since(conn: AsyncConnection, since: Any) -> list[dict[str, Any]]:
    """Totals per model since a point in time. RLS scopes it to the caller."""
    result = await conn.execute(
        text(f"select model, {USAGE_SUMS} from usage_events where at >= :since group by model"),
        {"since": since},
    )
    return [_row_to_dict(r) for r in result]


async def usage_by_project(conn: AsyncConnection, since: Any) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            f"""
            select project_id, coalesce(project_name, 'removed project') as project_name,
                   model, {USAGE_SUMS}
            from usage_events where at >= :since
            group by project_id, project_name, model
            """
        ),
        {"since": since},
    )
    return [_row_to_dict(r) for r in result]


async def usage_buckets(
    conn: AsyncConnection, since: Any, bucket: str = "hour"
) -> list[dict[str, Any]]:
    """A coarse series, so a week of use can be seen as a shape."""
    unit = "day" if bucket == "day" else "hour"
    result = await conn.execute(
        text(
            f"""
            select date_trunc('{unit}', at) as bucket, model, {USAGE_SUMS}
            from usage_events where at >= :since
            group by bucket, model order by bucket
            """
        ),
        {"since": since},
    )
    return [_row_to_dict(r) for r in result]


async def first_harness_credential_privileged(
    conn: AsyncConnection, user_id: str
) -> str | None:
    """How this person pays, which decides which usage number leads.

    Read from the credential they actually connected rather than asked for as
    a preference: someone on a subscription cares how much of the window has
    gone, someone on an API key cares what it cost, and neither should have to
    tell us which they are.
    """
    result = await conn.execute(
        text(
            """
            select hc.auth_mode::text
            from private.harness_credentials hc
            join org_members m on m.org_id = hc.org_id
            where m.user_id = cast(:user_id as uuid) and hc.project_id is null
            order by hc.updated_at desc
            limit 1
            """
        ),
        {"user_id": user_id},
    )
    row = result.first()
    return str(row[0]) if row else None


async def list_model_prices(conn: AsyncConnection) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            "select org_id, model, input_per_m, output_per_m, updated_at "
            "from model_prices order by model"
        )
    )
    return [_row_to_dict(r) for r in result]


async def upsert_model_price(
    conn: AsyncConnection,
    *,
    org_id: UUID,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into model_prices (org_id, model, input_per_m, output_per_m)
            values (:org_id, :model, :input_per_m, :output_per_m)
            on conflict (org_id, model) do update set
              input_per_m = excluded.input_per_m,
              output_per_m = excluded.output_per_m,
              updated_at = now()
            returning org_id, model, input_per_m, output_per_m, updated_at
            """
        ),
        {
            "org_id": org_id,
            "model": model,
            "input_per_m": input_per_m,
            "output_per_m": output_per_m,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to set prices for this organization.")
    return _row_to_dict(row)


async def delete_model_price(conn: AsyncConnection, org_id: UUID, model: str) -> bool:
    result = await conn.execute(
        text(
            "delete from model_prices where org_id = :org_id and model = :model "
            "returning model"
        ),
        {"org_id": org_id, "model": model},
    )
    return result.first() is not None


async def usage_times(conn: AsyncConnection, since: Any) -> list[Any]:
    """Just the timestamps, for working out where a limit window opened."""
    result = await conn.execute(
        text("select at from usage_events where at >= :since order by at"),
        {"since": since},
    )
    return [row[0] for row in result]


async def usage_between(
    conn: AsyncConnection, start: Any, end: Any
) -> list[dict[str, Any]]:
    """Per-model totals inside a window, which is anchored rather than trailing."""
    result = await conn.execute(
        text(
            f"select model, {USAGE_SUMS} from usage_events "
            "where at >= :start and at < :end group by model"
        ),
        {"start": start, "end": end},
    )
    return [_row_to_dict(r) for r in result]


async def get_usage_limits(conn: AsyncConnection) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            "select session_tokens, weekly_tokens, alert_percent, "
            "alerted_window, alerted_week from usage_limits limit 1"
        )
    )
    row = result.first()
    return _row_to_dict(row) if row is not None else None


async def set_usage_limits(
    conn: AsyncConnection,
    *,
    user_id: UUID,
    session_tokens: int | None,
    weekly_tokens: int | None,
    alert_percent: int | None = None,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into usage_limits
              (user_id, session_tokens, weekly_tokens, alert_percent)
            values (:user_id, :session_tokens, :weekly_tokens, :alert_percent)
            on conflict (user_id) do update set
              session_tokens = excluded.session_tokens,
              weekly_tokens  = excluded.weekly_tokens,
              alert_percent  = excluded.alert_percent,
              updated_at     = now()
            returning session_tokens, weekly_tokens, alert_percent
            """
        ),
        {
            "user_id": user_id,
            "session_tokens": session_tokens,
            "weekly_tokens": weekly_tokens,
            "alert_percent": alert_percent,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to set limits.")
    return _row_to_dict(row)


async def limits_to_check_privileged(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Everyone who has asked to be warned before they run out."""
    result = await conn.execute(
        text(
            "select user_id, session_tokens, weekly_tokens, alert_percent, "
            "alerted_window, alerted_week from usage_limits "
            "where alert_percent is not null"
        )
    )
    return [_row_to_dict(r) for r in result]


async def usage_times_for_privileged(
    conn: AsyncConnection, user_id: Any, since: Any
) -> list[Any]:
    result = await conn.execute(
        text(
            "select at from usage_events where user_id = :u and at >= :since "
            "order by at"
        ),
        {"u": user_id, "since": since},
    )
    return [row[0] for row in result]


async def usage_total_between_privileged(
    conn: AsyncConnection, user_id: Any, start: Any, end: Any
) -> int:
    result = await conn.execute(
        text(
            "select coalesce(sum(input_tokens + output_tokens + cache_read_tokens "
            "+ cache_write_5m_tokens + cache_write_1h_tokens), 0) "
            "from usage_events where user_id = :u and at >= :start and at < :end"
        ),
        {"u": user_id, "start": start, "end": end},
    )
    row = result.first()
    return int(row[0]) if row else 0


async def mark_alerted_privileged(
    conn: AsyncConnection, *, user_id: Any, column: str, anchor: Any
) -> None:
    """Record which window an alert has fired for, so it fires once.

    The column is chosen from a fixed pair rather than passed through, because
    it is interpolated into SQL and anything else would be an injection point.
    """
    if column not in {"alerted_window", "alerted_week"}:
        raise ValueError(f"Not an alert column: {column}")
    await conn.execute(
        text(f"update usage_limits set {column} = :anchor where user_id = :u"),
        {"anchor": anchor, "u": user_id},
    )


async def get_auth_methods_privileged(conn: AsyncConnection) -> dict[str, Any]:
    """Everything needed to render GoTrue's configuration, secrets included."""
    row = (
        await conn.execute(
            text(
                """
                select m.*, s.google_client_secret, s.microsoft_client_secret,
                       s.smtp_password, i.public_url, i.signup_open
                  from auth_methods m
                  cross join private.auth_secrets s
                  cross join instance_settings i
                 limit 1
                """
            )
        )
    ).first()
    if row is None:
        return {}
    found = _row_to_dict(row)
    for key in ("google_client_secret", "microsoft_client_secret", "smtp_password"):
        if found.get(key):
            found[key] = decrypt(found[key])
    return found


async def get_auth_methods(conn: AsyncConnection) -> dict[str, Any]:
    """The half a client may see. No secrets, by construction."""
    row = (await conn.execute(text("select * from auth_methods limit 1"))).first()
    return _row_to_dict(row) if row is not None else {}


async def set_auth_methods(
    conn: AsyncConnection, *, fields: dict[str, Any]
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            update auth_methods set
              password_enabled    = :password_enabled,
              magic_link_enabled  = :magic_link_enabled,
              smtp_host           = :smtp_host,
              smtp_port           = :smtp_port,
              smtp_user           = :smtp_user,
              smtp_sender         = :smtp_sender,
              google_enabled      = :google_enabled,
              google_client_id    = :google_client_id,
              microsoft_enabled   = :microsoft_enabled,
              microsoft_client_id = :microsoft_client_id,
              microsoft_tenant    = :microsoft_tenant,
              updated_at          = now()
            returning *
            """
        ),
        fields,
    )
    row = result.first()
    if row is None:
        raise PermissionError("Only an owner can change how people sign in.")
    return _row_to_dict(row)


async def set_auth_secrets_privileged(
    conn: AsyncConnection, *, secrets: dict[str, str | None]
) -> None:
    """Only what was supplied. A blank field means "leave it alone", because a
    write form cannot show a secret back and would otherwise erase it.

    That is what this said and not what it did: only `None` was skipped, and an
    empty string wrote NULL. The settings form loads every secret as `""` —
    it cannot do otherwise, since the API never sends a secret to a client — so
    saving the screen erased whichever secrets had not been retyped. Someone
    who set up Microsoft sign-in and later changed anything else on that screen
    was left with a client id, no secret, and "Unsupported provider: missing
    OAuth secret" from the auth service.

    To remove a secret, turn its provider off.
    """
    sets, params = [], {}
    for key, value in secrets.items():
        if not value:
            continue
        sets.append(f"{key} = :{key}")
        params[key] = encrypt(value)
    if not sets:
        return
    await conn.execute(
        text(f"update private.auth_secrets set {', '.join(sets)}, updated_at = now()"),
        params,
    )
