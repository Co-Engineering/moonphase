-- ===========================================================================
-- A third harness
--
-- `harness_kind` has carried 'opencode' since the first migration, because the
-- seam was drawn for a second agent before there was one. Pydantic AI is the
-- third, and unlike the other two it is a value the type has never seen.
--
-- Adding a value to an enum is its own statement and cannot be used in the
-- same transaction that adds it — which is fine here, because nothing below
-- uses it. The rows that will hold it are written by the application later.
-- ===========================================================================

alter type public.harness_kind add value if not exists 'pydantic_ai';

comment on type public.harness_kind is
  'Which coding agent a project runs. Each is a Harness subclass in '
  'apps/api/moonphase/harness; adding one is a subclass, a value here, and '
  'an install line in the runtime image recipe.';
