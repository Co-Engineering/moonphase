-- ===========================================================================
-- Per-project base environment.
--
-- Which distribution the project's container runs on. Stored as text rather
-- than an enum so adding one to the catalogue does not need a migration, and
-- unknown values fall back to the default in application code rather than
-- making a row unreadable.
-- ===========================================================================

alter table public.projects
  add column if not exists environment text not null default 'debian';

comment on column public.projects.environment is
  'Key into the environment catalogue in moonphase/environments.py.';
