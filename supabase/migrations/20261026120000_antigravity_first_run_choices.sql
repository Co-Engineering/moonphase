-- ===========================================================================
-- Antigravity's first-run questions, answered once per person
--
-- Every session has a HOME of its own, so `agy` treats each one as a fresh
-- install: a colour scheme, Google's Terms of Service & Data Use, and whether
-- to trust the folder, every time. Both of the last two are the user's to
-- answer, so Moonphase only answers them for someone who said it may — asked
-- when they add their first Google account, and changeable afterwards.
--
-- On the personal profile, next to the rest of what a person sets once and
-- expects everywhere. Off by default: a row from before this column, or a
-- person who never said, gets the questions as before.
-- ===========================================================================

alter table public.workspace_profiles
  add column antigravity_accept_terms  boolean not null default false,
  add column antigravity_trust_folders boolean not null default false;
