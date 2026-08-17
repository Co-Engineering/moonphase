-- ===========================================================================
-- One cursor per transcript, not one per session
--
-- Claude Code opens a new transcript file per conversation. A single
-- (file, offset) pair means that the moment someone starts a second
-- conversation, the collector follows the new file and abandons whatever the
-- previous one wrote since the last pass — up to a couple of minutes of
-- messages, silently, exactly at the moment a session gets busy.
--
-- A map of file to offset reads every transcript that has grown, so switching
-- conversations costs nothing.
-- ===========================================================================

alter table public.project_sessions
  add column usage_cursors jsonb not null default '{}'::jsonb;

-- Carry the existing single cursor over so nothing is re-read on upgrade.
update public.project_sessions
   set usage_cursors = jsonb_build_object(usage_file, usage_offset)
 where usage_file is not null;

alter table public.project_sessions
  drop column usage_file,
  drop column usage_offset;
