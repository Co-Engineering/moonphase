-- ===========================================================================
-- What your plan actually allows
--
-- Anthropic does not publish a token allowance per plan, and Moonphase cannot
-- ask the API for one without the session's own OAuth token. So the allowance
-- is something you tell it once, and everything else — how much of the window
-- has gone, when it resets — is derived from data already collected.
--
-- Per user, not per organization: a subscription belongs to a person, and
-- sessions are individual by design.
-- ===========================================================================

create table public.usage_limits (
  user_id        uuid primary key references auth.users (id) on delete cascade,
  -- Nulls mean "not told", which renders as no percentage rather than as zero.
  session_tokens bigint check (session_tokens is null or session_tokens > 0),
  weekly_tokens  bigint check (weekly_tokens is null or weekly_tokens > 0),
  updated_at     timestamptz not null default now()
);

alter table public.usage_limits enable row level security;

create policy usage_limits_own on public.usage_limits
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

grant select, insert, update, delete on public.usage_limits to authenticated;
grant all on public.usage_limits to service_role;
