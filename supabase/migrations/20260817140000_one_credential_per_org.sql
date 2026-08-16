-- ===========================================================================
-- One org-wide harness credential per harness
--
-- Signing in was an insert, not an upsert. The per-project branch deleted the
-- row it was replacing; the org-wide branch — the one the Settings sign-in
-- uses — did not. So every sign-in appended another row, and resolution
-- ordered only by `project_id nulls last`, which does not distinguish between
-- several org-wide rows. Postgres was free to hand back whichever it liked.
--
-- The symptom is the worst kind: you sign in, Moonphase says you are connected,
-- and the container still comes up unauthenticated because it was handed a
-- stale credential. Signing in again makes it more likely, not less.
--
-- A partial unique index makes the duplicate impossible rather than merely
-- avoided by convention.
-- ===========================================================================

-- Any duplicates already accumulated: keep the newest.
delete from private.harness_credentials a
using private.harness_credentials b
where a.project_id is null
  and b.project_id is null
  and a.org_id = b.org_id
  and a.harness = b.harness
  and (a.updated_at, a.id) < (b.updated_at, b.id);

create unique index if not exists harness_credentials_org_uniq
  on private.harness_credentials (org_id, harness)
  where project_id is null;
