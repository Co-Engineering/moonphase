-- ===========================================================================
-- More than one Claude account per person
--
-- People commonly hold several subscriptions — their own, their employer's, a
-- client's — and which one a piece of work runs on matters: it is whose
-- allowance it spends and, for a client, whose terms it is done under. Until
-- now an organization could hold exactly one org-wide credential per harness
-- (harness_credentials_org_uniq), so switching meant signing out and back in.
--
-- Now an org holds any number of named accounts, exactly one of which is the
-- default. A project can name the account its owner's sessions run on, and a
-- session can name its own; the session's choice wins, then the project's,
-- then the default. Both are plain references that fall back to the default
-- when the account is removed.
-- ===========================================================================

drop index if exists private.harness_credentials_org_uniq;

alter table private.harness_credentials
  add column if not exists is_default boolean not null default false;

-- Whatever each org was already using becomes its default. The old index
-- guaranteed there was at most one org-wide row per harness to choose from.
update private.harness_credentials
set is_default = true
where project_id is null;

create unique index if not exists harness_credentials_org_default_uniq
  on private.harness_credentials (org_id, harness)
  where project_id is null and is_default;

alter table public.projects
  add column if not exists harness_credential_id uuid
    references private.harness_credentials (id) on delete set null;

alter table public.project_sessions
  add column if not exists harness_credential_id uuid
    references private.harness_credentials (id) on delete set null;
