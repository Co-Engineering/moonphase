-- ===========================================================================
-- Sharing servers and projects with individual people
--
-- Organizations already answer "my team can use everything we own". This adds
-- the other half: handing one machine, or one running agent, to one person.
--
-- Design notes
--   * A share is keyed on an email address, not a user id. You can share with
--     someone who has not signed up yet; the row is claimed by a trigger when
--     they do. Without this, sharing in a self-hosted install means telling
--     your colleague to register first and then coming back, which nobody does.
--   * `project_access()` and `server_access()` are the single definition of
--     who may do what. The RLS policies call them and so does the API, so the
--     database and the application cannot drift into disagreeing.
--   * Shares grant use, never administration. A share can only ever be created
--     or revoked by someone with 'admin' on the resource itself, so there is no
--     re-sharing and no way to escalate by being shared with.
-- ===========================================================================

create type public.share_role as enum (
  'viewer',       -- watch it happen
  'collaborator'  -- drive it
);

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table public.server_shares (
  id         uuid primary key default gen_random_uuid(),
  server_id  uuid not null references public.servers (id) on delete cascade,
  -- Null until the invitee has an account. The email is what the grant is
  -- really made against; this column is the resolved subject once one exists.
  user_id    uuid references auth.users (id) on delete cascade,
  -- Kept even after user_id is filled in, because `authenticated` cannot read
  -- auth.users and the UI still has to be able to say who it is shared with.
  -- A later email change in GoTrue leaves this as the label it was granted to.
  email      text not null check (position('@' in email) > 1 and length(email) <= 320),
  role       public.share_role not null default 'collaborator',
  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now()
);

create unique index server_shares_uniq
  on public.server_shares (server_id, lower(email));
create index server_shares_user_idx on public.server_shares (user_id);

create table public.project_shares (
  id         uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects (id) on delete cascade,
  user_id    uuid references auth.users (id) on delete cascade,
  email      text not null check (position('@' in email) > 1 and length(email) <= 320),
  role       public.share_role not null default 'collaborator',
  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now()
);

create unique index project_shares_uniq
  on public.project_shares (project_id, lower(email));
create index project_shares_user_idx on public.project_shares (user_id);

-- ---------------------------------------------------------------------------
-- Claiming a share made before the invitee had an account
-- ---------------------------------------------------------------------------

create or replace function public.claim_pending_shares()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if new.email is null then
    return new;
  end if;
  update public.server_shares
     set user_id = new.id
   where user_id is null and lower(email) = lower(new.email);
  update public.project_shares
     set user_id = new.id
   where user_id is null and lower(email) = lower(new.email);
  return new;
end;
$$;

-- Separate from handle_new_user rather than folded into it: the personal-org
-- bootstrap must not be able to fail because of an unrelated share row.
create trigger on_auth_user_created_claim_shares
  after insert on auth.users
  for each row execute function public.claim_pending_shares();

-- ---------------------------------------------------------------------------
-- Share lookups
--
-- SECURITY DEFINER for the same reason the membership helpers are: policies on
-- the share tables call them, and they must not recurse into their own check.
-- ---------------------------------------------------------------------------

create or replace function public.server_share_role(p_server uuid)
returns public.share_role
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select s.role from public.server_shares s
  where s.server_id = p_server and s.user_id = auth.uid()
  limit 1;
$$;

create or replace function public.project_share_role(p_project uuid)
returns public.share_role
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select s.role from public.project_shares s
  where s.project_id = p_project and s.user_id = auth.uid()
  limit 1;
$$;

-- ---------------------------------------------------------------------------
-- Effective access
--
-- One of:
--   'admin'  everything, including deleting it and managing its shares
--   'write'  use it: start, stop, type into it, create projects on it
--   'read'   watch it; no input, no lifecycle
--   'host'   (projects only) you own the machine it runs on, but not the
--            project. You can see that it exists and reclaim the resources it
--            is using. You cannot read the conversation or type into it.
--   null     no access at all
--
-- Each comes in two forms. The `_for` variant takes the row's own columns and
-- is what the policies use; the one-argument variant looks the row up and is
-- what application code calls.
--
-- The split is not cosmetic. `INSERT ... RETURNING` re-applies the SELECT
-- policy to the new row, and a STABLE function reading `projects` during that
-- statement is looking at a snapshot from before the row existed — so a policy
-- written as `project_access(id) is not null` rejects every insert its owner
-- makes. Passing the columns in sidesteps the lookup entirely.
-- ---------------------------------------------------------------------------

create or replace function public.server_access_for(
  p_server uuid, p_org uuid, p_created_by uuid
)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select case
    when public.has_org_role(p_org, 'owner', 'admin')
         or p_created_by = auth.uid()                       then 'admin'
    when public.has_org_role(p_org, 'member')               then 'write'
    when public.server_share_role(p_server) = 'collaborator' then 'write'
    when public.server_share_role(p_server) = 'viewer'      then 'read'
    when public.is_org_member(p_org)                        then 'read'
    else null
  end;
$$;

create or replace function public.server_access(p_server uuid)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select public.server_access_for(s.id, s.org_id, s.created_by)
  from public.servers s where s.id = p_server;
$$;

create or replace function public.project_access_for(
  p_project uuid, p_org uuid, p_server uuid, p_created_by uuid
)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select case
    when public.has_org_role(p_org, 'owner', 'admin')
         or p_created_by = auth.uid()                        then 'admin'
    when public.has_org_role(p_org, 'member')                then 'write'
    when public.project_share_role(p_project) = 'collaborator' then 'write'
    when public.project_share_role(p_project) = 'viewer'     then 'read'
    when public.is_org_member(p_org)                         then 'read'
    -- Lending someone a machine should not mean losing sight of what runs on
    -- it. It should also not mean reading their agent's conversation, which is
    -- why this is its own level and not 'read'.
    when exists (
      select 1 from public.servers v
      where v.id = p_server and public.has_org_role(v.org_id, 'owner', 'admin')
    ) then 'host'
    else null
  end;
$$;

create or replace function public.project_access(p_project uuid)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select public.project_access_for(p.id, p.org_id, p.server_id, p.created_by)
  from public.projects p where p.id = p_project;
$$;

-- The machine's display name, for anyone who can see something running on it.
-- Deliberately just the name: a project share must not disclose the address,
-- the login, or the fingerprint of a server the recipient has no business with.
create or replace function public.server_label(p_server uuid)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select s.name from public.servers s
  where s.id = p_server
    and (public.is_org_member(s.org_id)
         or public.server_share_role(s.id) is not null
         or exists (
           select 1 from public.projects p
           where p.server_id = s.id
             and public.project_share_role(p.id) is not null
         ));
$$;

-- ---------------------------------------------------------------------------
-- Policy updates
-- ---------------------------------------------------------------------------

alter table public.server_shares  enable row level security;
alter table public.project_shares enable row level security;

-- Servers: a share makes one visible. It never grants management — those
-- policies stay exactly as they were.
drop policy servers_select on public.servers;
create policy servers_select on public.servers
  for select to authenticated
  using (public.is_org_member(org_id) or public.server_share_role(id) is not null);

-- Projects.
drop policy projects_select on public.projects;
create policy projects_select on public.projects
  for select to authenticated
  using (public.project_access_for(id, org_id, server_id, created_by) is not null);

-- Creating a project now has to prove access to the *server* as well as to the
-- organization. Previously it did not: foreign keys are not subject to RLS, so
-- a crafted request could attach a project to a server the caller could not
-- see, and the API would then connect to it with the owner's credentials. The
-- route already checked this; the database now does too.
drop policy projects_insert on public.projects;
create policy projects_insert on public.projects
  for insert to authenticated
  with check (
    created_by = auth.uid()
    and public.has_org_role(org_id, 'owner', 'admin', 'member')
    and public.server_access(server_id) in ('admin', 'write')
  );

drop policy projects_update on public.projects;
create policy projects_update on public.projects
  for update to authenticated
  using (public.project_access_for(id, org_id, server_id, created_by)
         in ('admin', 'write'))
  with check (public.project_access_for(id, org_id, server_id, created_by)
              in ('admin', 'write'));

-- 'host' can delete: it is their hardware, and reclaiming it should not require
-- an SSH session behind Moonphase's back.
drop policy projects_delete on public.projects;
create policy projects_delete on public.projects
  for delete to authenticated
  using (public.project_access_for(id, org_id, server_id, created_by)
         in ('admin', 'host'));

drop policy project_sessions_select on public.project_sessions;
create policy project_sessions_select on public.project_sessions
  for select to authenticated
  using (public.project_access(project_id) in ('admin', 'write', 'read'));

drop policy project_sessions_write on public.project_sessions;
create policy project_sessions_write on public.project_sessions
  for all to authenticated
  using (public.project_access(project_id) in ('admin', 'write'))
  with check (public.project_access(project_id) in ('admin', 'write'));

-- Shares themselves: only an admin of the resource sees the whole list. You
-- can always see, and give up, your own.
create policy server_shares_select on public.server_shares
  for select to authenticated
  using (public.server_access(server_id) = 'admin' or user_id = auth.uid());

create policy server_shares_insert on public.server_shares
  for insert to authenticated
  with check (public.server_access(server_id) = 'admin');

create policy server_shares_update on public.server_shares
  for update to authenticated
  using (public.server_access(server_id) = 'admin')
  with check (public.server_access(server_id) = 'admin');

create policy server_shares_delete on public.server_shares
  for delete to authenticated
  using (public.server_access(server_id) = 'admin' or user_id = auth.uid());

create policy project_shares_select on public.project_shares
  for select to authenticated
  using (public.project_access(project_id) = 'admin' or user_id = auth.uid());

create policy project_shares_insert on public.project_shares
  for insert to authenticated
  with check (public.project_access(project_id) = 'admin');

create policy project_shares_update on public.project_shares
  for update to authenticated
  using (public.project_access(project_id) = 'admin')
  with check (public.project_access(project_id) = 'admin');

create policy project_shares_delete on public.project_shares
  for delete to authenticated
  using (public.project_access(project_id) = 'admin' or user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

grant select, insert, update, delete
  on public.server_shares, public.project_shares
  to authenticated;

grant all on public.server_shares, public.project_shares to service_role;

grant execute on function public.server_share_role(uuid)  to authenticated, service_role;
grant execute on function public.project_share_role(uuid) to authenticated, service_role;
grant execute on function public.server_access(uuid)      to authenticated, service_role;
grant execute on function public.project_access(uuid)     to authenticated, service_role;
grant execute on function public.server_label(uuid)       to authenticated, service_role;
grant execute on function public.server_access_for(uuid, uuid, uuid)
  to authenticated, service_role;
grant execute on function public.project_access_for(uuid, uuid, uuid, uuid)
  to authenticated, service_role;
