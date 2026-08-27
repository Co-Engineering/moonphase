-- ===========================================================================
-- auth_methods_write passed for every signed-in user
--
-- Same bug class `20260820120000_instance_admins.sql` already fixed once, in
-- the sibling `instance_settings_write` policy — and said so explicitly in
-- its own comment. This policy was never ported over and still has the
-- original check: 'owner'/'admin' of *any* organization, which every account
-- satisfies for its own personal org from the moment it signs up. Any
-- authenticated user could rewrite the instance's SMTP relay, OAuth client
-- secrets, or disable password auth entirely.
-- ===========================================================================

drop policy if exists auth_methods_write on public.auth_methods;

create policy auth_methods_write on public.auth_methods
  for update to authenticated
  using (
    exists (
      select 1 from public.instance_admins a where a.user_id = auth.uid()
    )
  );
