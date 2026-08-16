-- ===========================================================================
-- Session activity and push notifications.
--
-- The product's premise is that you can walk away. That only works if
-- something tells you when the agent stops needing to be left alone — either
-- because it finished, or because it is blocked on a question only you can
-- answer. Without it you are back to opening the app every few minutes, which
-- is the waiting the app exists to remove.
--
-- Activity is tracked per session so the state survives a client reload, and
-- so a notification fires once per transition rather than once per observer.
-- ===========================================================================

-- working        : the pane changed since the last probe
-- awaiting_input : static, and showing a question the harness is blocked on
-- idle           : static for a while, nothing asked
-- stopped        : no session or no container
create type public.activity_state as enum (
  'unknown', 'working', 'awaiting_input', 'idle', 'stopped'
);

alter table public.project_sessions
  add column if not exists activity        public.activity_state not null default 'unknown',
  add column if not exists activity_at     timestamptz,
  -- Hash of the last observed pane. Change detection is the primary signal,
  -- because it needs no knowledge of a harness's UI strings.
  add column if not exists pane_digest     text,
  -- What the user was last told, so a transition notifies exactly once.
  add column if not exists notified_state  public.activity_state,
  -- A short excerpt of the question, for the notification body.
  add column if not exists activity_detail text;

create index project_sessions_activity_idx
  on public.project_sessions (activity)
  where activity in ('working', 'awaiting_input');

-- ---------------------------------------------------------------------------
-- Web push subscriptions
-- ---------------------------------------------------------------------------

create table public.push_subscriptions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users (id) on delete cascade,
  -- The endpoint uniquely identifies a browser install; re-subscribing from the
  -- same one must update rather than accumulate.
  endpoint   text not null unique,
  p256dh     text not null,
  auth       text not null,
  user_agent text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index push_subscriptions_user_idx on public.push_subscriptions (user_id);

create trigger push_subscriptions_touch before update on public.push_subscriptions
  for each row execute function public.touch_updated_at();

alter table public.push_subscriptions enable row level security;

-- A subscription is personal: even an org owner has no business reading
-- another member's browser endpoints.
create policy push_subscriptions_own on public.push_subscriptions
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

grant select, insert, update, delete on public.push_subscriptions to authenticated;
grant all on all tables in schema public to service_role;
