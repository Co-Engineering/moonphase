-- ===========================================================================
-- When a session's activity was last confirmed
--
-- `activity_at` says when the state last *changed*, which is the right thing
-- to show ("idle for three hours") and the wrong thing to trust. A session the
-- monitor cannot reach keeps whatever state it had, and the interface presents
-- it with the same confidence as one checked a second ago — so a stopped agent
-- can sit there claiming to be working overnight.
--
-- This records when we last actually looked. A state older than a couple of
-- sweeps is not news, it is a guess, and the UI can say so.
-- ===========================================================================

alter table public.project_sessions
  add column checked_at timestamptz;

-- Nothing has been checked by the new code yet, and pretending otherwise would
-- make every existing row look fresh.
update public.project_sessions set checked_at = null;
