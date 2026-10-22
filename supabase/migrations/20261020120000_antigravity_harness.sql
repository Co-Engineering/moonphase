-- ===========================================================================
-- A fourth harness: Google's Antigravity CLI
--
-- `agy`, the terminal agent that replaced Gemini CLI. Like Pydantic AI before
-- it, this is a value `harness_kind` has never seen, so it needs adding before
-- a project, a session or an account can name it.
--
-- Adding a value to an enum is its own statement and cannot be used in the
-- same transaction that adds it — which is fine here, because nothing below
-- uses it. The rows that will hold it are written by the application later.
-- ===========================================================================

alter type public.harness_kind add value if not exists 'antigravity';
