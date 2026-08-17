#!/usr/bin/env sh
#
# Apply the schema, once, in order.
#
# The Supabase CLI does this in development. It is a Node application with its
# own Docker orchestration, which is a great deal to install on a server whose
# only job is to run one `psql` per file — so this does the same work with the
# tools that are already in a Postgres image.
#
# Idempotent by record: each file's name is written to a table once it has been
# applied, and a file already in that table is skipped. Re-running the stack is
# therefore free, and a half-finished migration is impossible because each one
# runs in a single transaction.
set -eu

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"
BOOTSTRAP="${BOOTSTRAP:-/bootstrap.sql}"

export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
PSQL="psql --host=${POSTGRES_HOST:-db} --port=${POSTGRES_PORT:-5432} \
  --username=${POSTGRES_USER:-postgres} --dbname=${POSTGRES_DB:-postgres} \
  --no-psqlrc --quiet --set=ON_ERROR_STOP=1"

say() { printf '\033[34m%s\033[0m\n' "$*"; }

# --- wait for the database -------------------------------------------------
say "==> waiting for postgres"
attempt=0
until $PSQL --command 'select 1' >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -gt 60 ]; then
    echo "postgres did not become ready in time" >&2
    exit 1
  fi
  sleep 2
done

# --- wait for GoTrue to have created auth.users -----------------------------
# The first migration puts a trigger on `auth.users`, so the table has to exist
# before it runs. GoTrue creates it on its own first start; racing it produces
# a confusing "relation auth.users does not exist" on a fresh install.
say "==> waiting for the auth schema"
attempt=0
until $PSQL --tuples-only --command \
    "select to_regclass('auth.users') is not null" 2>/dev/null | grep -q t; do
  attempt=$((attempt + 1))
  if [ "$attempt" -gt 90 ]; then
    echo "auth.users never appeared — is the auth service healthy?" >&2
    exit 1
  fi
  sleep 2
done

# --- roles and auth helpers -------------------------------------------------
say "==> bootstrap"
$PSQL --file "$BOOTSTRAP"

# --- schema -----------------------------------------------------------------
$PSQL --command "
  create table if not exists public.schema_migrations (
    version    text primary key,
    applied_at timestamptz not null default now()
  );
"

say "==> migrations"
applied=0
for file in "$MIGRATIONS_DIR"/*.sql; do
  [ -f "$file" ] || continue
  version=$(basename "$file")

  if $PSQL --tuples-only --command \
      "select 1 from public.schema_migrations where version = '$version'" \
      | grep -q 1; then
    continue
  fi

  printf '    %s\n' "$version"
  # One transaction per file: a migration that fails leaves nothing behind and
  # is not recorded, so the next run retries it from the top.
  $PSQL --single-transaction \
    --file "$file" \
    --command "insert into public.schema_migrations (version) values ('$version')"
  applied=$((applied + 1))
done

if [ "$applied" -eq 0 ]; then
  say "==> already up to date"
else
  say "==> applied $applied migration(s)"
fi
