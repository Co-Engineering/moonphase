-- ===========================================================================
-- How people sign in
--
-- Four methods, chosen during setup and changeable afterwards. GoTrue reads
-- its configuration from the environment at startup, so the API renders these
-- into a file the auth container watches — turning on Google is a checkbox,
-- not an edit on the server.
--
-- Client secrets and the SMTP password live in `private`, which the
-- authenticated role cannot reach at all, and are encrypted with the same key
-- as every other credential. The API reads them only to write the generated
-- file, and they are never returned to a client.
-- ===========================================================================

create table public.auth_methods (
  id                boolean primary key default true check (id),

  -- Always available unless deliberately turned off, because it is the only
  -- one that needs nothing configured.
  password_enabled  boolean not null default true,

  -- A link in an email. Needs somewhere to send mail from, which is why the
  -- SMTP settings sit beside it rather than in their own screen.
  magic_link_enabled boolean not null default false,
  smtp_host         text,
  smtp_port         int,
  smtp_user         text,
  smtp_sender       text,

  google_enabled    boolean not null default false,
  google_client_id  text,

  microsoft_enabled boolean not null default false,
  microsoft_client_id text,
  -- An Azure tenant, or `common` for any Microsoft account.
  microsoft_tenant  text default 'common',

  updated_at        timestamptz not null default now()
);

insert into public.auth_methods (id) values (true);

alter table public.auth_methods enable row level security;

-- Everyone signed in may read which methods exist; only an owner or admin may
-- change them. What a *signed-out* client needs is served by the API, and is
-- deliberately only the list of enabled methods.
create policy auth_methods_read on public.auth_methods
  for select to authenticated using (true);

create policy auth_methods_write on public.auth_methods
  for update to authenticated
  using (
    exists (
      select 1 from public.org_members m
      where m.user_id = auth.uid() and m.role in ('owner', 'admin')
    )
  );

grant select, update on public.auth_methods to authenticated;
grant all on public.auth_methods to service_role;

-- The halves that must never reach a browser.
-- bytea, because that is what Fernet returns and what every other stored
-- credential in this schema uses.
create table private.auth_secrets (
  id                      boolean primary key default true check (id),
  google_client_secret    bytea,
  microsoft_client_secret bytea,
  smtp_password           bytea,
  updated_at              timestamptz not null default now()
);

insert into private.auth_secrets (id) values (true);

alter table private.auth_secrets enable row level security;
revoke all on private.auth_secrets from anon, authenticated;
grant all on private.auth_secrets to service_role;
