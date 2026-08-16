-- ===========================================================================
-- Sessions belong to a person
--
-- Sharing a server or a project shares the *resource*. It must not share the
-- coding subscription behind it: two people working in one project each bring
-- their own Claude account, and neither one's work may run on the other's.
--
-- Until now a container had one credential file, one env file and one
-- `git config --global`, so every session in it authenticated as — and
-- committed as — whoever created the project. That is the thing this migration
-- exists to make impossible.
--
-- A session now records who owns it and where its private state lives:
--
--   home_dir   /home/dev/sessions/<name>   credentials, harness config,
--                                          history, transcripts, .gitconfig
--   workdir    /workspace-<name>           a git worktree on its own branch
--   branch     the branch that worktree is on
--
-- `/workspace` stays the shared repository. Collaborating means merging, which
-- is what git is for, rather than two agents writing the same file at once.
--
-- A user may have several sessions in one project; the rule is not one each,
-- it is that every session has exactly one owner.
-- ===========================================================================

alter table public.project_sessions
  add column user_id  uuid references auth.users (id) on delete set null,
  add column workdir  text not null default '/workspace',
  add column home_dir text not null default '/home/dev',
  add column branch   text;

-- Sessions that predate this all live in the shared home and checkout, which
-- the defaults above already describe. They belong to whoever made the project.
update public.project_sessions s
   set user_id = p.created_by
  from public.projects p
 where p.id = s.project_id
   and s.user_id is null;

create index project_sessions_user_idx on public.project_sessions (user_id);

-- ---------------------------------------------------------------------------
-- Who may do what to a session
--
--   watch   anyone who can observe the project
--   drive   the session's owner, and only them — otherwise the work would run
--           on their subscription while someone else typed
--   create  a collaborator on the project, for themselves only
--   remove  the owner, or an admin of the project reclaiming a stale one
-- ---------------------------------------------------------------------------

drop policy project_sessions_select on public.project_sessions;
create policy project_sessions_select on public.project_sessions
  for select to authenticated
  using (public.project_access(project_id) in ('admin', 'write', 'read'));

drop policy project_sessions_write on public.project_sessions;

create policy project_sessions_insert on public.project_sessions
  for insert to authenticated
  with check (
    public.project_access(project_id) in ('admin', 'write')
    and user_id = auth.uid()
  );

create policy project_sessions_update on public.project_sessions
  for update to authenticated
  using (
    public.project_access(project_id) in ('admin', 'write')
    and user_id = auth.uid()
  )
  with check (
    public.project_access(project_id) in ('admin', 'write')
    and user_id = auth.uid()
  );

create policy project_sessions_delete on public.project_sessions
  for delete to authenticated
  using (
    user_id = auth.uid()
    or public.project_access(project_id) = 'admin'
  );
