-- ===========================================================================
-- Antigravity's own settings, set once per person
--
-- `agy` keeps its settings in a settings.json of its own — the agent mode
-- (Auto, Accept edits, Plan), what it may do without asking, and so on — in
-- each session's HOME, so a choice made in one session's `/config` is gone in
-- the next. This is the person's standing answer, merged into every session's
-- file as it starts, the way their Claude Code settings are.
--
-- A JSON object of agy's own keys, kept as text like claude_settings_json.
-- Null until saved, which Moonphase reads as its defaults (Auto mode).
-- ===========================================================================

alter table public.workspace_profiles
  add column antigravity_settings_json text;
