-- ===========================================================================
-- Settings that belong to the instance, not to the file it was installed from
--
-- .env should hold only what has to exist before the database does: the key
-- that encrypts credentials, the password that reaches Postgres. Everything a
-- person chooses — what address this answers on, whether anyone else may sign
-- up — is a decision made after installing, and editing a file on a server to
-- change one is not a reasonable thing to ask.
--
-- One row, enforced. There is exactly one instance.
-- ===========================================================================

create table public.instance_settings (
  id                  boolean primary key default true check (id),
  -- What people type to reach this. Used to decide which hostnames the proxy
  -- may obtain a certificate for, so it is the domain in a real sense rather
  -- than a display string.
  public_url          text,
  -- Closed once the first account exists. An open instance is not a breach —
  -- every signup lands in its own organization and can see nothing of yours —
  -- but it is other people's resources on your machine.
  signup_open         boolean not null default true,
  setup_completed_at  timestamptz,
  updated_at          timestamptz not null default now()
);

insert into public.instance_settings (id) values (true);

alter table public.instance_settings enable row level security;

-- Readable by anyone signed in; only an owner may change it. The unauthenticated
-- read the setup screen needs goes through the API's service role instead, so
-- that nothing about this table is exposed to an anonymous client beyond the
-- single question "has setup happened".
create policy instance_settings_read on public.instance_settings
  for select to authenticated using (true);

create policy instance_settings_write on public.instance_settings
  for update to authenticated
  using (
    exists (
      select 1 from public.org_members m
      where m.user_id = auth.uid() and m.role in ('owner', 'admin')
    )
  );

grant select on public.instance_settings to authenticated;
grant update on public.instance_settings to authenticated;
grant all on public.instance_settings to service_role;
