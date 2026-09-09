-- ===========================================================================
-- A server's creator could see it, but not what was running on it
--
-- server_access_for grants 'admin' on a server two ways: org role, or being
-- the row's own created_by — the second exists so a server survives its
-- creator later leaving the org that owns it, or never having joined one in
-- the first place (created before an org existed to join, a since-cleaned-up
-- membership, and so on).
--
-- project_access_for's 'host' branch — "you own the machine this runs on,
-- but not the project" — only replicated the org-role half of that grant. A
-- user who reached 'admin' on the server purely through created_by got a
-- server page that looked fully theirs (including, since #129, a resource
-- breakdown naming every project and how much space each uses) while
-- project_count and every project list came back empty, because the RLS
-- policy backing them never recognised them as the server's creator at all.
-- Adding the same OR here is the fix: same two paths as server_access_for,
-- so a server admin's two surfaces — the machine, and what is running on it —
-- agree on who that is.
-- ===========================================================================

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
      where v.id = p_server
        and (
          public.has_org_role(v.org_id, 'owner', 'admin')
          or v.created_by = auth.uid()
        )
    ) then 'host'
    else null
  end;
$$;
