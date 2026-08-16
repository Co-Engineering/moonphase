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
    s.managed_public_key, s.last_seen_at, s.created_at
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
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into servers
              (org_id, name, host, port, ssh_user, ssh_auth_mode, status, created_by)
            values
              (:org_id, :name, :host, :port, :ssh_user, cast(:auth_mode as ssh_auth_mode),
               'bootstrapping', :created_by)
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
    coalesce(
        (select s.activity::text from project_sessions s
         where s.project_id = p.id
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
     where s.project_id = p.id
     order by case s.activity
                when 'awaiting_input' then 0
                when 'working' then 1
                when 'idle' then 2
                else 3
              end, s.created_at
     limit 1) as activity_detail,
    (select max(s.activity_at) from project_sessions s
     where s.project_id = p.id) as activity_at
"""


async def list_projects(
    conn: AsyncConnection, server_id: UUID | None = None
) -> list[dict[str, Any]]:
    clause = "where p.server_id = :server_id" if server_id else ""
    result = await conn.execute(
        text(
            f"""
            select {PROJECT_COLUMNS}, s.name as server_name
            from projects p
            join servers s on s.id = p.server_id
            {clause}
            order by p.created_at desc
            """
        ),
        {"server_id": server_id} if server_id else {},
    )
    return [_row_to_dict(r) for r in result]


async def get_project(conn: AsyncConnection, project_id: UUID) -> dict[str, Any] | None:
    result = await conn.execute(
        text(
            f"""
            select {PROJECT_COLUMNS}, s.name as server_name
            from projects p
            join servers s on s.id = p.server_id
            where p.id = :id
            """
        ),
        {"id": project_id},
    )
    row = result.first()
    return _row_to_dict(row) if row else None


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


async def upsert_session(
    conn: AsyncConnection,
    *,
    project_id: UUID,
    harness: str,
    tmux_session: str,
    state: str,
    transcript_path: str | None = None,
    mark_started: bool = False,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into project_sessions
              (project_id, tmux_session, harness, state, transcript_path, started_at)
            values
              (:project_id, :tmux_session, cast(:harness as harness_kind), :state,
               :transcript_path, case when :mark_started then now() else null end)
            on conflict (project_id, tmux_session) do update set
              state = excluded.state,
              transcript_path = coalesce(excluded.transcript_path,
                                         project_sessions.transcript_path),
              started_at = case when :mark_started then now()
                                else project_sessions.started_at end
            returning id, project_id, tmux_session, harness, state, started_at,
                      last_attached_at, transcript_path
            """
        ),
        {
            "project_id": project_id,
            "tmux_session": tmux_session,
            "harness": harness,
            "state": state,
            "transcript_path": transcript_path,
            "mark_started": mark_started,
        },
    )
    row = result.first()
    if row is None:
        raise PermissionError("Not allowed to manage sessions for this project.")
    return _row_to_dict(row)


async def get_sessions(conn: AsyncConnection, project_id: UUID) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            select id, project_id, tmux_session, harness, state, started_at,
                   last_attached_at, transcript_path,
                   activity::text as activity, activity_detail
            from project_sessions
            where project_id = :pid
            order by created_at, tmux_session
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
            order by project_id nulls last
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
            select id, org_id, claude_settings_json, claude_md, mcp_json,
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
    env_vars: dict[str, str],
    git_user_name: str | None,
    git_user_email: str | None,
) -> dict[str, Any]:
    result = await conn.execute(
        text(
            """
            insert into workspace_profiles
              (org_id, claude_settings_json, claude_md, mcp_json, env_vars,
               git_user_name, git_user_email)
            values
              (:org_id, :settings, :claude_md, :mcp, cast(:env as jsonb),
               :git_name, :git_email)
            on conflict (org_id) do update set
              claude_settings_json = excluded.claude_settings_json,
              claude_md            = excluded.claude_md,
              mcp_json             = excluded.mcp_json,
              env_vars             = excluded.env_vars,
              git_user_name        = excluded.git_user_name,
              git_user_email       = excluded.git_user_email
            returning id, org_id, claude_settings_json, claude_md, mcp_json,
                      env_vars, git_user_name, git_user_email, created_at, updated_at
            """
        ),
        {
            "org_id": org_id,
            "settings": claude_settings_json,
            "claude_md": claude_md,
            "mcp": mcp_json,
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
