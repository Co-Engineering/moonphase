-- ===========================================================================
-- Each Claude account's plan limits, kept
--
-- The dashboard used to read an account's five-hour and weekly windows live:
-- from the status-line file a running session of it wrote, else by asking
-- Anthropic. Nothing was kept but a per-process cache, so an account whose
-- sessions had all been closed — or an API restart — left the dashboard with
-- nothing to say about how much of the window was gone. Now the background
-- monitor writes every reading here as it finds it, and the dashboard reads
-- only this.
--
-- One row per account: the newest reading, and the last attempt to get one
-- if that failed. Private, like the accounts themselves; the API reads it
-- for the accounts it has already resolved as the caller's own.
-- ===========================================================================

create table private.harness_account_usage (
  account_id uuid primary key
    references private.harness_credentials (id) on delete cascade,
  -- [{key, label, percent, resets_at}], as `/usage` shows them. A window whose
  -- reset has since passed is shown as empty again when read.
  windows    jsonb not null default '[]'::jsonb,
  -- When the reading was true: when the session's status line wrote it, or
  -- when Anthropic answered. Null until there has been one.
  read_at    timestamptz,
  -- Where it came from: 'statusline', or the token Anthropic was asked with
  -- ('stored', 'session').
  source     text,
  -- The last attempt to ask Anthropic, when it failed: 'expired',
  -- 'unsupported', 'rate_limited', 'unreachable' or 'error'. Null after a
  -- good reading.
  problem    text,
  detail     text,
  -- After a refusal: not to be asked again before this.
  retry_at   timestamptz,
  -- Refusals in a row, for the back-off.
  refusals   integer not null default 0,
  updated_at timestamptz not null default now()
);

-- Like every private table: the API's service role only, no policy for
-- `authenticated` at all.
grant all on private.harness_account_usage to service_role;
