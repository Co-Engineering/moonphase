-- ===========================================================================
-- MCP server OAuth credentials
--
-- Claude Code's own OAuth for an MCP server is a redirect-based PKCE flow
-- hardcoded to `http://localhost:PORT/callback` — fine on a laptop, useless
-- inside a remote container nobody's browser can reach. `claude mcp login
-- <name> --no-browser` is the documented escape hatch: it prints the
-- authorization URL and also accepts the resulting redirect URL pasted back
-- at an interactive prompt, so completing it never actually requires the
-- callback listener to be reachable — only that the code+state it prints
-- get typed back in. Moonphase drives that relay the same way it already
-- drives `claude setup-token` for the account itself (see login.py).
--
-- What comes out the other end is a single entry Claude Code writes into
-- ~/.claude/.credentials.json under a top-level "mcpOAuth" key, keyed by
-- "<server-name>|<hash>". That's captured verbatim (key and all — the hash
-- is Claude Code's own and not ours to recompute) and replayed into every
-- session's own credentials.json on start, org-wide, the same way the
-- Anthropic account credential already is.
-- ===========================================================================

create table private.mcp_oauth_credentials (
  id              uuid primary key default gen_random_uuid(),
  org_id          uuid not null references public.organizations (id) on delete cascade,
  server_name     text not null,
  -- The raw {"<name>|<hash>": {...}} pair from Claude Code's own
  -- credentials.json, round-tripped exactly as captured.
  credential_json text not null,
  created_by      uuid references auth.users (id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (org_id, server_name)
);

create trigger mcp_oauth_credentials_touch before update on private.mcp_oauth_credentials
  for each row execute function public.touch_updated_at();

-- Same trust model as harness_credentials and vcs_credentials: a live OAuth
-- token, private-schema, no policy for `authenticated` at all — service_role
-- only, reached exclusively through the API's privileged connection.
alter table private.mcp_oauth_credentials enable row level security;

grant all on private.mcp_oauth_credentials to service_role;
