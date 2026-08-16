-- ===========================================================================
-- Global workspace profile
--
-- Signing in and configuring a harness once per project does not scale past
-- about two projects. Everything a user would otherwise retype lives here,
-- scoped to the organization, and is materialised into every project
-- container when its session starts.
--
-- The split is deliberate:
--   * non-secret configuration (settings, CLAUDE.md, git identity) is in
--     `public` so clients can read and edit it under RLS;
--   * tokens are in `private`, unreadable by the authenticated role, and
--     surfaced to clients only as "connected / not connected".
-- ===========================================================================

create table public.workspace_profiles (
  id         uuid primary key default gen_random_uuid(),
  org_id     uuid not null unique references public.organizations (id) on delete cascade,

  -- Harness configuration, applied to every container. Stored as text rather
  -- than jsonb because it is round-tripped verbatim into files the harness
  -- owns, and reformatting someone's config is rude.
  claude_settings_json text,
  claude_md            text,
  mcp_json             text,

  -- Extra environment for every session, e.g. shared API keys the agent needs.
  env_vars jsonb not null default '{}'::jsonb,

  -- Git identity, so commits made by the agent are not authored by "dev".
  git_user_name  text,
  git_user_email text,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger workspace_profiles_touch before update on public.workspace_profiles
  for each row execute function public.touch_updated_at();

-- Version-control credentials. Separate from harness credentials because a
-- GitHub token is used by git and gh, not by the agent's own auth.
create type public.vcs_provider as enum ('github');

create type public.vcs_auth_mode as enum ('oauth_device', 'personal_token');

create table private.vcs_credentials (
  id         uuid primary key default gen_random_uuid(),
  org_id     uuid not null references public.organizations (id) on delete cascade,
  provider   public.vcs_provider not null,
  auth_mode  public.vcs_auth_mode not null,
  -- Login name, kept in the clear so the UI can show "connected as @you".
  account    text,
  scopes     text,
  token_enc  bytea not null,
  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, provider)
);

create trigger vcs_credentials_touch before update on private.vcs_credentials
  for each row execute function public.touch_updated_at();

-- Org-wide harness credentials (project_id is null) need their own uniqueness
-- constraint; the v0.1 index only covered the per-project case, so a second
-- global sign-in would have silently created a duplicate row.
create unique index harness_credentials_org_uniq
  on private.harness_credentials (org_id, harness)
  where project_id is null;

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------

alter table public.workspace_profiles enable row level security;
alter table private.vcs_credentials   enable row level security;

create policy workspace_profiles_select on public.workspace_profiles
  for select to authenticated
  using (public.is_org_member(org_id));

create policy workspace_profiles_insert on public.workspace_profiles
  for insert to authenticated
  with check (public.has_org_role(org_id, 'owner', 'admin', 'member'));

create policy workspace_profiles_update on public.workspace_profiles
  for update to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin', 'member'))
  with check (public.has_org_role(org_id, 'owner', 'admin', 'member'));

create policy workspace_profiles_delete on public.workspace_profiles
  for delete to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin'));

-- private.vcs_credentials deliberately has no policy: RLS is on and nothing
-- grants the authenticated role access, so tokens are service_role only.

grant select, insert, update, delete on public.workspace_profiles to authenticated;
grant all on all tables in schema public to service_role;
grant all on all tables in schema private to service_role;

-- Every org gets an empty profile, so the API never has to branch on "no row".
insert into public.workspace_profiles (org_id)
select id from public.organizations
on conflict (org_id) do nothing;

create or replace function public.handle_new_organization()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.workspace_profiles (org_id)
  values (new.id)
  on conflict (org_id) do nothing;
  return new;
end;
$$;

create trigger on_organization_created
  after insert on public.organizations
  for each row execute function public.handle_new_organization();
