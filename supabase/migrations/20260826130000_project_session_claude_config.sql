-- ===========================================================================
-- Claude Code config, scoped to a project or a single session
--
-- The workspace profile is org-wide: one CLAUDE.md, one settings.json, one
-- set of MCP servers, applied to every project. That is right for "sign in
-- once" but wrong for "this project needs a database MCP server the others
-- don't" or "I personally want a stricter permission rule while I'm testing
-- something risky in this one session" — today there is no layer beneath the
-- org for either of those.
--
-- Same four fields as workspace_profiles, at two more scopes:
--   projects           applies to every session in that project
--   project_sessions   applies to that one session only (private to its owner,
--                       same as the row itself already is)
--
-- Materialisation composes all three layers (org, project, session) into one
-- effective config under the session's own $HOME rather than writing into the
-- project's git checkout — see moonphase/harness/claude_code.py. That keeps
-- this out of `git status` and out of the way of a team's own committed
-- `.claude/` files, the same way org-level config already avoids it.
-- ===========================================================================

alter table public.projects
  add column claude_settings_json text,
  add column claude_md            text,
  add column mcp_json             text,
  add column skills_json          jsonb not null default '{}'::jsonb;

alter table public.project_sessions
  add column claude_settings_json text,
  add column claude_md            text,
  add column mcp_json             text,
  add column skills_json          jsonb not null default '{}'::jsonb;

-- The org profile predates skills; every other scope gets the column at birth.
alter table public.workspace_profiles
  add column skills_json jsonb not null default '{}'::jsonb;
