-- ===========================================================================
-- Moonphase initial schema
--
-- Design notes
--   * Tenancy is org-scoped from day one. Every user gets a personal org on
--     signup; shared orgs are the same table with is_personal = false, so
--     "invite a teammate" is a row in org_members, not a migration.
--   * RLS is enforced in the database rather than the API. The API connects as
--     a privileged role but does `SET LOCAL ROLE authenticated` plus
--     `SET LOCAL request.jwt.claims` per request, so policies below apply to
--     ordinary traffic. Background jobs opt out explicitly via service_role.
--   * Secrets live in the `private` schema, which is never exposed to
--     PostgREST and has no policies granting the authenticated role access.
--     Only the API (service_role) can read them, and only to hand them to an
--     SSH connection. They must never reach a client.
-- ===========================================================================

create extension if not exists pgcrypto;

create schema if not exists private;
revoke all on schema private from anon, authenticated;
grant usage on schema private to service_role;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

create type public.org_role as enum ('owner', 'admin', 'member', 'viewer');

create type public.server_status as enum (
  'pending',        -- row created, never contacted
  'bootstrapping',  -- installing key / docker
  'online',
  'offline',
  'error'
);

create type public.project_status as enum (
  'creating', 'running', 'stopped', 'error'
);

create type public.harness_kind as enum ('claude_code', 'opencode');

create type public.harness_auth_mode as enum ('oauth', 'api_key');

-- How we authenticate to a server.
--   password_bootstrap : user gave a password once; we installed our own key
--                        and discarded the password.
--   managed_key        : we generated a keypair; user installed the pubkey.
--   provided_key       : user pasted their own private key.
create type public.ssh_auth_mode as enum (
  'password_bootstrap', 'managed_key', 'provided_key'
);

-- ---------------------------------------------------------------------------
-- Organizations and membership
-- ---------------------------------------------------------------------------

create table public.organizations (
  id          uuid primary key default gen_random_uuid(),
  name        text not null check (length(trim(name)) between 1 and 80),
  slug        text not null unique check (slug ~ '^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$'),
  is_personal boolean not null default false,
  created_by  uuid references auth.users (id) on delete set null,
  created_at  timestamptz not null default now()
);

create table public.org_members (
  org_id     uuid not null references public.organizations (id) on delete cascade,
  user_id    uuid not null references auth.users (id) on delete cascade,
  role       public.org_role not null default 'member',
  created_at timestamptz not null default now(),
  primary key (org_id, user_id)
);

create index org_members_user_idx on public.org_members (user_id);

-- Membership helpers. SECURITY DEFINER so that policies on org_members can
-- call them without recursing into their own RLS check.
create or replace function public.is_org_member(p_org uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from public.org_members m
    where m.org_id = p_org and m.user_id = auth.uid()
  );
$$;

create or replace function public.org_role_of(p_org uuid)
returns public.org_role
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select m.role from public.org_members m
  where m.org_id = p_org and m.user_id = auth.uid();
$$;

create or replace function public.has_org_role(p_org uuid, variadic p_roles public.org_role[])
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select public.org_role_of(p_org) = any (p_roles);
$$;

-- Give every new user a personal org they own, so the app never has to handle
-- a "you have no organization" state.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_base text;
  v_slug text;
  v_org  uuid;
  v_n    int := 0;
begin
  v_base := regexp_replace(lower(split_part(coalesce(new.email, 'user'), '@', 1)), '[^a-z0-9]+', '-', 'g');
  v_base := trim(both '-' from v_base);
  if length(v_base) < 2 then
    v_base := 'user';
  end if;
  v_slug := left(v_base, 40);

  -- Slugs are globally unique; suffix until we find a free one.
  while exists (select 1 from public.organizations o where o.slug = v_slug) loop
    v_n := v_n + 1;
    v_slug := left(v_base, 40) || '-' || v_n::text;
  end loop;

  insert into public.organizations (name, slug, is_personal, created_by)
  values (coalesce(new.email, 'Personal'), v_slug, true, new.id)
  returning id into v_org;

  insert into public.org_members (org_id, user_id, role)
  values (v_org, new.id, 'owner');

  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Personal orgs are garbage-collected when their last member leaves. They
-- cannot be reached by anyone at that point (every policy goes through
-- org_members), and leaving them behind would slowly fill the table with
-- unreferenceable rows. Shared orgs are left alone: an empty team is a real
-- state an admin may be in the middle of re-staffing.
create or replace function public.cleanup_orphan_personal_org()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  delete from public.organizations o
  where o.id = old.org_id
    and o.is_personal
    and not exists (
      select 1 from public.org_members m where m.org_id = o.id
    );
  return old;
end;
$$;

create trigger on_org_member_removed
  after delete on public.org_members
  for each row execute function public.cleanup_orphan_personal_org();

-- ---------------------------------------------------------------------------
-- Servers
-- ---------------------------------------------------------------------------

create table public.servers (
  id                   uuid primary key default gen_random_uuid(),
  org_id               uuid not null references public.organizations (id) on delete cascade,
  name                 text not null check (length(trim(name)) between 1 and 64),
  host                 text not null,
  port                 int not null default 22 check (port between 1 and 65535),
  ssh_user             text not null,
  ssh_auth_mode        public.ssh_auth_mode not null,
  status               public.server_status not null default 'pending',
  status_detail        text,
  -- Pinned on first successful connect; a change afterwards is a hard failure.
  host_key_fingerprint text,
  docker_version       text,
  -- Public half of the keypair we generated, shown in the UI for manual install.
  managed_public_key   text,
  last_seen_at         timestamptz,
  created_by           uuid references auth.users (id) on delete set null,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  unique (org_id, name)
);

create index servers_org_idx on public.servers (org_id);

-- ---------------------------------------------------------------------------
-- Projects
-- ---------------------------------------------------------------------------

create table public.projects (
  id               uuid primary key default gen_random_uuid(),
  org_id           uuid not null references public.organizations (id) on delete cascade,
  server_id        uuid not null references public.servers (id) on delete cascade,
  name             text not null check (length(trim(name)) between 1 and 64),
  slug             text not null check (slug ~ '^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$'),
  harness          public.harness_kind not null default 'claude_code',
  repo_url         text,
  container_name   text,
  container_id     text,
  workspace_volume text,
  home_volume      text,
  status           public.project_status not null default 'creating',
  status_detail    text,
  -- Preview / zrok wiring, unused until v0.3.
  preview_port     int check (preview_port is null or preview_port between 1 and 65535),
  zrok_share_token text,
  preview_url      text,
  created_by       uuid references auth.users (id) on delete set null,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (server_id, slug)
);

create index projects_org_idx on public.projects (org_id);
create index projects_server_idx on public.projects (server_id);

-- One row per tmux session inside a project container. Today there is exactly
-- one ('moonphase'), but the harness column lives here rather than on the
-- project so a single workspace can eventually run Claude Code and OpenCode
-- side by side in separate panes.
create table public.project_sessions (
  id               uuid primary key default gen_random_uuid(),
  project_id       uuid not null references public.projects (id) on delete cascade,
  tmux_session     text not null default 'moonphase',
  harness          public.harness_kind not null,
  state            text not null default 'stopped' check (state in ('stopped', 'starting', 'running', 'error')),
  started_at       timestamptz,
  last_attached_at timestamptz,
  -- Path inside the container to the harness's own JSONL transcript; the phone
  -- client tails this instead of scraping the terminal.
  transcript_path  text,
  created_at       timestamptz not null default now(),
  unique (project_id, tmux_session)
);

create index project_sessions_project_idx on public.project_sessions (project_id);

-- ---------------------------------------------------------------------------
-- Secrets (private schema — never exposed to clients)
-- ---------------------------------------------------------------------------

create table private.server_credentials (
  server_id       uuid primary key references public.servers (id) on delete cascade,
  private_key_enc bytea,
  passphrase_enc  bytea,
  -- Only populated between "user submitted the form" and "key install verified".
  password_enc    bytea,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table private.harness_credentials (
  id             uuid primary key default gen_random_uuid(),
  org_id         uuid not null references public.organizations (id) on delete cascade,
  -- Null means "org-wide default, usable by any project in the org".
  project_id     uuid references public.projects (id) on delete cascade,
  harness        public.harness_kind not null,
  auth_mode      public.harness_auth_mode not null,
  label          text,
  api_key_enc    bytea,
  -- The harness's own credential file (e.g. ~/.claude/.credentials.json).
  oauth_blob_enc bytea,
  created_by     uuid references auth.users (id) on delete set null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create index harness_credentials_org_idx on private.harness_credentials (org_id);
create unique index harness_credentials_project_uniq
  on private.harness_credentials (project_id, harness)
  where project_id is not null;

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------

create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger servers_touch before update on public.servers
  for each row execute function public.touch_updated_at();
create trigger projects_touch before update on public.projects
  for each row execute function public.touch_updated_at();
create trigger server_credentials_touch before update on private.server_credentials
  for each row execute function public.touch_updated_at();
create trigger harness_credentials_touch before update on private.harness_credentials
  for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------

alter table public.organizations     enable row level security;
alter table public.org_members       enable row level security;
alter table public.servers           enable row level security;
alter table public.projects          enable row level security;
alter table public.project_sessions  enable row level security;
alter table private.server_credentials  enable row level security;
alter table private.harness_credentials enable row level security;

-- Organizations -------------------------------------------------------------

create policy organizations_select on public.organizations
  for select to authenticated
  using (public.is_org_member(id));

create policy organizations_insert on public.organizations
  for insert to authenticated
  with check (created_by = auth.uid() and is_personal = false);

create policy organizations_update on public.organizations
  for update to authenticated
  using (public.has_org_role(id, 'owner', 'admin'))
  with check (public.has_org_role(id, 'owner', 'admin'));

create policy organizations_delete on public.organizations
  for delete to authenticated
  using (public.has_org_role(id, 'owner') and is_personal = false);

-- Membership ----------------------------------------------------------------

create policy org_members_select on public.org_members
  for select to authenticated
  using (public.is_org_member(org_id));

create policy org_members_insert on public.org_members
  for insert to authenticated
  with check (public.has_org_role(org_id, 'owner', 'admin'));

create policy org_members_update on public.org_members
  for update to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin'))
  with check (public.has_org_role(org_id, 'owner', 'admin'));

-- Admins may remove others; anyone may remove themselves (leave the org).
create policy org_members_delete on public.org_members
  for delete to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin') or user_id = auth.uid());

-- Servers -------------------------------------------------------------------

create policy servers_select on public.servers
  for select to authenticated
  using (public.is_org_member(org_id));

create policy servers_insert on public.servers
  for insert to authenticated
  with check (public.has_org_role(org_id, 'owner', 'admin', 'member') and created_by = auth.uid());

create policy servers_update on public.servers
  for update to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin') or created_by = auth.uid())
  with check (public.has_org_role(org_id, 'owner', 'admin') or created_by = auth.uid());

create policy servers_delete on public.servers
  for delete to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin') or created_by = auth.uid());

-- Projects ------------------------------------------------------------------

create policy projects_select on public.projects
  for select to authenticated
  using (public.is_org_member(org_id));

create policy projects_insert on public.projects
  for insert to authenticated
  with check (public.has_org_role(org_id, 'owner', 'admin', 'member') and created_by = auth.uid());

create policy projects_update on public.projects
  for update to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin') or created_by = auth.uid())
  with check (public.has_org_role(org_id, 'owner', 'admin') or created_by = auth.uid());

create policy projects_delete on public.projects
  for delete to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin') or created_by = auth.uid());

-- Sessions ------------------------------------------------------------------

create policy project_sessions_select on public.project_sessions
  for select to authenticated
  using (exists (
    select 1 from public.projects p
    where p.id = project_id and public.is_org_member(p.org_id)
  ));

create policy project_sessions_write on public.project_sessions
  for all to authenticated
  using (exists (
    select 1 from public.projects p
    where p.id = project_id
      and (public.has_org_role(p.org_id, 'owner', 'admin', 'member'))
  ))
  with check (exists (
    select 1 from public.projects p
    where p.id = project_id
      and (public.has_org_role(p.org_id, 'owner', 'admin', 'member'))
  ));

-- Secrets: deliberately no policy for anon/authenticated. RLS is enabled and
-- no policy grants access, so both roles see zero rows even if a future change
-- accidentally exposes the schema. service_role bypasses RLS.

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

grant usage on schema public to anon, authenticated, service_role;

grant select, insert, update, delete on
  public.organizations, public.org_members, public.servers,
  public.projects, public.project_sessions
  to authenticated;

grant all on all tables in schema public to service_role;
grant all on all tables in schema private to service_role;
grant all on all sequences in schema public to service_role;

grant execute on function public.is_org_member(uuid) to authenticated, service_role;
grant execute on function public.org_role_of(uuid) to authenticated, service_role;
grant execute on function public.has_org_role(uuid, public.org_role[]) to authenticated, service_role;
