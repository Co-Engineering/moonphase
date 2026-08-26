-- ===========================================================================
-- A session's label, separate from what it is
--
-- `tmux_session` is not a label — it names the tmux session, and derives the
-- session's home directory, its git worktree and its branch (see
-- 20260817160000_individual_sessions.sql). Changing it would mean moving all
-- three inside a running container, which is a great deal of risk for a
-- nicer word, so it never has.
--
-- That made renaming a session look unsupported, when what people actually
-- want is the same thing project and server renaming already give: a display
-- name, decoupled from the identifier the resource was created with.
-- ===========================================================================

-- Bounded like every other name in this schema. The API refuses anything
-- longer with the number in the message, and the constraint is what makes
-- that refusal a fact rather than a convention some future caller can skip.
alter table public.project_sessions
  add column display_name text
  check (display_name is null or length(trim(display_name)) between 1 and 64);
