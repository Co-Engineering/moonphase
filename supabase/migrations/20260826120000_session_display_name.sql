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

alter table public.project_sessions
  add column display_name text;
