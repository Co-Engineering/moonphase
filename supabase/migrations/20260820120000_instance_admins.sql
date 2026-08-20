-- ===========================================================================
-- Who administers this instance
--
-- There was no answer to that question, and something was already asking it.
-- `instance_settings_write` allowed anyone who was 'owner' or 'admin' of any
-- organization — and every account is the owner of its own personal org, made
-- for it by a trigger the moment it signs up. So the check passed for every
-- signed-in user, and any of them could change the instance's domain or
-- reopen registration.
--
-- It went unnoticed because until now nothing else depended on it. Managing
-- accounts does, and hanging "delete anyone's account" off a check that
-- everyone passes would be considerably worse than the setting it currently
-- guards.
--
-- So administration of the *instance* becomes its own thing, separate from any
-- role inside an organization. Being the owner of your own work says nothing
-- about whether you may close the door on everybody else's.
-- ===========================================================================

create table if not exists public.instance_admins (
  user_id  uuid primary key references auth.users (id) on delete cascade,
  added_by uuid references auth.users (id) on delete set null,
  added_at timestamptz not null default now()
);

comment on table public.instance_admins is
  'Accounts that may change instance settings and manage other accounts. '
  'Distinct from org roles: owning your own organization is not a claim on '
  'anyone else''s.';

-- The account that set this instance up, which is the earliest one there is.
--
-- Existing installs have to keep working, and there is exactly one defensible
-- answer for them: whoever was here first is the person who ran the installer.
-- On a fresh install this finds nothing, and the setup screen inserts the
-- account that completes it.
insert into public.instance_admins (user_id)
select id from auth.users order by created_at asc limit 1
on conflict (user_id) do nothing;

alter table public.instance_admins enable row level security;

-- Readable by anyone signed in: the client needs to know whether to draw the
-- administration screens at all, and who runs the box is not a secret from the
-- people using it.
create policy instance_admins_read on public.instance_admins
  for select to authenticated using (true);

-- Deliberately no insert or delete policy for `authenticated`. Granting and
-- revoking goes through the API as service_role, which refuses to remove the
-- last administrator — a rule with a count in it, which is clearer in one place
-- than spread across policies.
grant select on public.instance_admins to authenticated;
grant all on public.instance_admins to service_role;

-- Replace the check that everybody passed.
drop policy if exists instance_settings_write on public.instance_settings;

create policy instance_settings_write on public.instance_settings
  for update to authenticated
  using (
    exists (
      select 1 from public.instance_admins a where a.user_id = auth.uid()
    )
  );
