-- ===========================================================================
-- Naming the person a session belongs to
--
-- `authenticated` cannot read auth.users, and it should not be able to: that
-- table is an enumeration of everyone with an account here. But a shared
-- project's session list is unreadable without it — "someone else's session"
-- tells you nothing about whether to interrupt them.
--
-- So: a definer function that will name a user only to someone who already
-- shares a project with them. It cannot be used to probe for accounts, because
-- an id you have no project in common with returns null however you found it.
-- ===========================================================================

create or replace function public.session_owner_label(p_user uuid)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select u.email
  from auth.users u
  where u.id = p_user
    and exists (
      select 1
      from public.project_sessions s
      where s.user_id = p_user
        and public.project_access(s.project_id) is not null
    );
$$;

grant execute on function public.session_owner_label(uuid)
  to authenticated, service_role;
