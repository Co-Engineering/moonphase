-- ===========================================================================
-- Seeing the disk before it fills up
--
-- A project's volumes are the actual bytes on a server — the container
-- itself is nearly nothing. Deleting a project keeps its volumes by default,
-- on purpose: an accidental delete should not also delete the work. But
-- nothing ever showed how much was accumulating, and nothing ever came back
-- to clean a volume up once its project was gone. Together that is how a
-- disk fills silently: nobody removes anything, and nobody would have
-- noticed if they had wanted to.
--
-- These two tables cover both halves. `server_resource_snapshots` holds the
-- latest disk/CPU/memory reading per server, replaced wholesale each sweep —
-- this is "what does it look like right now", not a history, so one row per
-- server is enough and nothing needs pruning. `orphaned_volumes` is a volume
-- a project delete (or a cleanup that could not reach the server at the
-- time) left behind, with a grace period before the background sweep in
-- monitor.py actually removes it.
-- ===========================================================================

create table public.server_resource_snapshots (
  server_id         uuid primary key references public.servers (id) on delete cascade,
  disk_total_bytes  bigint not null check (disk_total_bytes >= 0),
  disk_used_bytes   bigint not null check (disk_used_bytes >= 0),
  -- One entry per project with a volume on this server:
  -- {project_id, name, workspace_bytes, home_bytes, cpu_percent, mem_bytes}.
  -- jsonb rather than a child table because nothing ever reads a single
  -- project's row on its own — always "this server's whole breakdown" — and
  -- a sweep replaces the set wholesale anyway.
  by_project        jsonb not null default '[]'::jsonb,
  sampled_at        timestamptz not null default now()
);

alter table public.server_resource_snapshots enable row level security;

create policy server_resource_snapshots_select on public.server_resource_snapshots
  for select to authenticated
  using (public.server_access(server_id) is not null);

grant select on public.server_resource_snapshots to authenticated;
grant all on public.server_resource_snapshots to service_role;

create table public.orphaned_volumes (
  id            uuid primary key default gen_random_uuid(),
  server_id     uuid not null references public.servers (id) on delete cascade,
  volume_name   text not null,
  -- The project row is already gone by the time this exists; kept only so
  -- the list means something to whoever is looking at it.
  project_name  text,
  reason        text not null check (reason in ('project_deleted', 'cleanup_failed', 'discovered')),
  discovered_at timestamptz not null default now(),
  delete_after  timestamptz not null,
  unique (server_id, volume_name)
);

create index orphaned_volumes_delete_after_idx on public.orphaned_volumes (delete_after);

alter table public.orphaned_volumes enable row level security;

-- Bookkeeping for the sweep, not a feature anyone administers by hand — same
-- visibility as who may already manage the server.
create policy orphaned_volumes_select on public.orphaned_volumes
  for select to authenticated
  using (public.server_access(server_id) = 'admin');

grant select on public.orphaned_volumes to authenticated;
grant all on public.orphaned_volumes to service_role;
