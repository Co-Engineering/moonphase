-- ===========================================================================
-- The parts of Supabase that Moonphase actually depends on
--
-- A full Supabase deployment is eight services. Moonphase uses two of them:
-- Postgres, and GoTrue for sign-in. It talks to Postgres directly with asyncpg
-- and never goes through PostgREST, Realtime or Storage, so shipping those
-- would be several hundred megabytes of container to run nothing.
--
-- What the migrations *do* assume is the surface Supabase puts on top of a
-- plain Postgres: three roles, and the `auth.uid()` helper that every RLS
-- policy is written against. That is all created here.
--
-- Mounted twice on purpose: once into the database's init directory, so it runs
-- when the cluster is created and before GoTrue first connects, and once into
-- the migration step, so an existing deployment picks up changes here on
-- upgrade. Everything below is written to be safe to run repeatedly.
--
-- The ordering matters. GoTrue is pointed at `search_path=auth` and will not
-- create that schema itself; without it existing first, its own migrations
-- fail with "no schema has been selected to create in".
-- ===========================================================================

-- --- roles -----------------------------------------------------------------
-- `authenticated` is what a request-scoped connection becomes via SET LOCAL
-- ROLE; `service_role` is the explicit opt-out used for background work and
-- for reading the `private` schema. `anon` exists because the schema revokes
-- from it by name.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end
$$;

-- The API connects as one login role and switches. It therefore has to be a
-- member of each of them.
grant anon, authenticated, service_role to current_user;

grant usage on schema public to anon, authenticated, service_role;

-- --- auth schema and helpers -----------------------------------------------
-- GoTrue owns the tables inside it; this only has to exist first.
create schema if not exists auth;
grant usage on schema auth to anon, authenticated, service_role;

-- Reads the claims the API pushes in with SET LOCAL request.jwt.claims. Every
-- policy in the schema is written against this, so it has to behave exactly as
-- Supabase's does — including returning null rather than raising when there
-- are no claims, which is what makes an unauthenticated query return nothing
-- instead of failing.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select nullif(
    coalesce(
      nullif(current_setting('request.jwt.claim.sub', true), ''),
      (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
    ),
    ''
  )::uuid
$$;

create or replace function auth.role()
returns text
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  )
$$;

create or replace function auth.email()
returns text
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.email', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email')
  )
$$;

grant execute on function auth.uid, auth.role, auth.email
  to anon, authenticated, service_role;
