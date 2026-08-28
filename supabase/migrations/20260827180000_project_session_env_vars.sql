-- ===========================================================================
-- Environment variables, scoped to a project or a single session
--
-- The workspace profile already carried env_vars org-wide — shared API keys
-- every project needs. This is the layer beneath it, the same one
-- claude_settings_json/claude_md/mcp_json/skills_json already got in an
-- earlier migration: a project might need a database URL only it uses, and a
-- session might want its own value while testing something without touching
-- what anyone else's session sees.
--
-- Composed the same way those already are — org, then project, then session,
-- most specific wins a key collision — in moonphase.runtime.load_session_profile,
-- and materialised into the session's own env file exactly where the org
-- layer already was, so nothing about how a session picks these up changes.
-- ===========================================================================

alter table public.projects
  add column env_vars jsonb not null default '{}'::jsonb;

alter table public.project_sessions
  add column env_vars jsonb not null default '{}'::jsonb;
