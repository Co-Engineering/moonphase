-- ===========================================================================
-- Per-project opt-in to Docker access inside the container.
--
-- Off by default. Enabling this only means the project's container is
-- started under the sysbox-runc runtime (see servers.sysbox_version) -- it
-- does not install Docker itself inside the guest. The agent runs its own
-- `sudo apt-get install -y docker.io` once it is running under sysbox-runc,
-- which is what makes that safe. See docs/guides/docker-access.md.
-- ===========================================================================

alter table public.projects
  add column if not exists docker_access boolean not null default false;

comment on column public.projects.docker_access is
  'When true, the project container is started with --runtime=sysbox-runc so '
  'it can run its own Docker daemon. Requires the project server to have '
  'Sysbox installed (servers.sysbox_version not null); enforced in '
  'routers/projects.py at creation time, not by a DB constraint, since it '
  'needs a cross-table lookup.';
