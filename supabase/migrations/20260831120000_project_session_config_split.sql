-- ===========================================================================
-- Session-scope Claude config moves to its own table
--
-- claude_settings_json/claude_md/mcp_json/skills_json/env_vars landed
-- directly on project_sessions in two earlier migrations. project_sessions'
-- own SELECT policy predates both and was never tightened for them: it
-- still lets any project viewer/collaborator/admin read the full row
-- (project_access(project_id) in ('admin','write','read')), including
-- these columns -- while the UPDATE policy correctly requires
-- user_id = auth.uid(). env_vars exists specifically to hold things like a
-- session-only database URL, unencrypted, in that same row.
--
-- Nothing reaches this today: every current caller (get_session_config,
-- terminal.py, projects.py, mcp_oauth.py) already gates on is_mine or
-- CAN_CONTROL before reading a session that is not its own, and there is no
-- PostgREST/REST surface exposed in this deployment. This closes the gap at
-- the database layer anyway, so a future caller that forgets that check --
-- or a REST surface turned on later -- inherits the restriction rather than
-- the hole.
--
-- A blanket restriction on project_sessions itself would break the
-- legitimate case collaborators rely on: watching someone else's session
-- (its state, activity, transcript) is meant to work for anyone who can
-- observe the project. Only the *config* columns need narrowing, so they
-- move to their own table instead of tightening the row everyone already
-- sees.
-- ===========================================================================

create table public.project_session_config (
  session_id           uuid primary key references public.project_sessions (id) on delete cascade,
  claude_settings_json text,
  claude_md            text,
  mcp_json             text,
  skills_json          jsonb not null default '{}'::jsonb,
  env_vars             jsonb not null default '{}'::jsonb
);

-- One row per existing session, carrying over whatever it already had (a
-- session with nothing set gets the table's own defaults). This, plus the
-- trigger below for every session created from now on, means every session
-- always has exactly one config row -- so update_session_config (below)
-- stays a plain UPDATE, never an upsert, and the RLS policy on this table
-- never needs an INSERT case for application code to satisfy.
insert into public.project_session_config
  (session_id, claude_settings_json, claude_md, mcp_json, skills_json, env_vars)
select id, claude_settings_json, claude_md, mcp_json, skills_json, env_vars
from public.project_sessions;

alter table public.project_sessions
  drop column claude_settings_json,
  drop column claude_md,
  drop column mcp_json,
  drop column skills_json,
  drop column env_vars;

-- Keeps every future session in the same state the backfill above just put
-- every existing one in. security definer so this runs as the table owner
-- rather than whoever's insert into project_sessions fired it -- the point
-- is to create a blank row unconditionally, not to ask the RLS question
-- this whole migration exists to narrow.
create function public.create_project_session_config()
returns trigger language plpgsql security definer as $$
begin
  insert into public.project_session_config (session_id) values (new.id);
  return new;
end;
$$;

create trigger project_sessions_create_config
  after insert on public.project_sessions
  for each row execute function public.create_project_session_config();

-- select/update only, not insert/delete: every row is created by the
-- trigger and destroyed only via project_sessions' own cascade, so there is
-- no client-facing case for either verb -- and there is no policy that
-- would let one through regardless, once RLS is enabled below.
grant select, update on public.project_session_config to authenticated;
grant all on all tables in schema public to service_role;

alter table public.project_session_config enable row level security;

-- Same rule the split is for: the session's own owner, or a project admin
-- looking in (the "watch someone else's session" case, at the level admins
-- already get everywhere else) -- not a plain collaborator with only
-- observe/write access to the project.
create policy project_session_config_select on public.project_session_config
  for select to authenticated
  using (
    exists (
      select 1 from public.project_sessions ps
      where ps.id = project_session_config.session_id
        and (ps.user_id = auth.uid() or public.project_access(ps.project_id) = 'admin')
    )
  );

-- The same bar project_sessions' own row already requires to change
-- anything about a session -- its owner, with at least write access to the
-- project it is in. No INSERT policy: every row is created by the trigger
-- above, never by application code, so there is no client-facing case to
-- allow one for.
create policy project_session_config_update on public.project_session_config
  for update to authenticated
  using (
    exists (
      select 1 from public.project_sessions ps
      where ps.id = project_session_config.session_id
        and ps.user_id = auth.uid()
        and public.project_access(ps.project_id) in ('admin', 'write')
    )
  )
  with check (
    exists (
      select 1 from public.project_sessions ps
      where ps.id = session_id
        and ps.user_id = auth.uid()
        and public.project_access(ps.project_id) in ('admin', 'write')
    )
  );
