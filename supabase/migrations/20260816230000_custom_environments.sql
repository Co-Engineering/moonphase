-- ===========================================================================
-- User-defined project environments.
--
-- An environment is a base image plus optional setup commands. Moonphase
-- layers its own requirements (tmux, socat, the harness, Node) over that base
-- and builds the result on the managed server, so adding one needs no registry
-- and no release of Moonphase itself.
--
-- Built-in environments stay in application code: they are part of the
-- product, and keeping them out of the table means a fresh install has sensible
-- options before anyone has configured anything. Rows here are additions to
-- that list, scoped to an organization.
-- ===========================================================================

create table public.environments (
  id           uuid primary key default gen_random_uuid(),
  org_id       uuid not null references public.organizations (id) on delete cascade,

  -- Stable identifier stored on projects. Immutable once created, because a
  -- project row references it by value.
  key          text not null check (key ~ '^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$'),
  display_name text not null check (length(trim(display_name)) between 1 and 64),
  description  text,

  -- Anything Docker can pull. Must be Debian or Ubuntu family: the build
  -- installs Moonphase's requirements with apt.
  base_image   text not null check (length(trim(base_image)) between 1 and 200),

  -- Extra shell commands run as root during the build, one per line.
  setup_script text,

  created_by   uuid references auth.users (id) on delete set null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  unique (org_id, key)
);

create index environments_org_idx on public.environments (org_id);

create trigger environments_touch before update on public.environments
  for each row execute function public.touch_updated_at();

alter table public.environments enable row level security;

create policy environments_select on public.environments
  for select to authenticated
  using (public.is_org_member(org_id));

create policy environments_insert on public.environments
  for insert to authenticated
  with check (public.has_org_role(org_id, 'owner', 'admin', 'member'));

create policy environments_update on public.environments
  for update to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin', 'member'))
  with check (public.has_org_role(org_id, 'owner', 'admin', 'member'));

create policy environments_delete on public.environments
  for delete to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin'));

grant select, insert, update, delete on public.environments to authenticated;
grant all on all tables in schema public to service_role;
