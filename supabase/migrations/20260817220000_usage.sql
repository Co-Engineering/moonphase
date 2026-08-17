-- ===========================================================================
-- What each agent actually consumed
--
-- Claude Code records its own usage in the transcript it already writes: model,
-- input and output tokens, cache reads, and cache writes split by lifetime.
-- That is the authoritative number — it is what the provider counted — and it
-- is sitting in a file Moonphase already reads for the feed.
--
-- Stored per message rather than as a running total. A total cannot answer
-- "which project is eating my week", cannot be recomputed when a price changes,
-- and cannot be trusted after a crash. Rows can, and deduplicate on the
-- message id the provider assigned, so re-reading a transcript is harmless.
--
-- Usage is personal. Sessions belong to one person and run on their account, so
-- these are their numbers and nobody else's.
-- ===========================================================================

create table public.usage_events (
  id                    uuid primary key default gen_random_uuid(),
  user_id               uuid not null references auth.users (id) on delete cascade,
  project_id            uuid references public.projects (id) on delete set null,
  session_id            uuid references public.project_sessions (id) on delete set null,
  -- Kept as text so a deleted project's history survives it: what you spent
  -- last week did not stop having happened.
  project_name          text,
  model                 text not null,
  -- The provider's own id for the message, which is what makes re-reading a
  -- transcript idempotent.
  message_id            text not null,
  at                    timestamptz not null,
  input_tokens          bigint not null default 0,
  output_tokens         bigint not null default 0,
  cache_read_tokens     bigint not null default 0,
  -- Cache writes are priced by how long they live, so they cannot be one
  -- number.
  cache_write_5m_tokens bigint not null default 0,
  cache_write_1h_tokens bigint not null default 0,
  thinking_tokens       bigint not null default 0,
  created_at            timestamptz not null default now()
);

create unique index usage_events_message_uniq
  on public.usage_events (user_id, message_id);
create index usage_events_user_at_idx on public.usage_events (user_id, at desc);
create index usage_events_project_idx on public.usage_events (project_id);

-- Where the collector had read up to, per session. A transcript grows all day
-- and re-reading it from the top every few minutes would be the most expensive
-- thing Moonphase does.
alter table public.project_sessions
  add column usage_file   text,
  add column usage_offset bigint not null default 0;

alter table public.usage_events enable row level security;

create policy usage_events_select on public.usage_events
  for select to authenticated
  using (user_id = auth.uid());

-- Written only by the collector, which runs as service_role and has no caller.
grant select on public.usage_events to authenticated;
grant all on public.usage_events to service_role;
