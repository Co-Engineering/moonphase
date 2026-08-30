-- ===========================================================================
-- Sysbox runtime support, per server.
--
-- Sysbox (sysbox-runc) lets an *unprivileged* container run Docker safely,
-- via per-container user-namespace virtualization -- the alternative to
-- --privileged or mounting the host's docker.sock that a project asking for
-- Docker access needs. It is a host-level dependency: installed on the
-- managed server itself and registered as a Docker runtime in
-- /etc/docker/daemon.json, alongside Docker.
--
-- Off by default at both the server and project level. Nothing installs it
-- unless a server owner explicitly asks, mirroring how docker_version only
-- appears once Docker itself has been confirmed.
-- ===========================================================================

alter table public.servers
  add column if not exists sysbox_version text;

comment on column public.servers.sysbox_version is
  'sysbox-runc version once installed and confirmed registered as a Docker '
  'runtime. Null means not installed, not attempted, or not yet re-probed.';

alter table public.servers
  add column if not exists sysbox_status_detail text;

comment on column public.servers.sysbox_status_detail is
  'Outcome of the last Sysbox probe/install attempt when it is not simply '
  'installed -- an incompatibility reason or an install error. Kept '
  'separate from servers.status_detail, which carries the bootstrap/SSH '
  'narrative and would otherwise overwrite this on every re-bootstrap.';
