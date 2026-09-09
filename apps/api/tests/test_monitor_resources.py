"""Resource sweeps: reading disk/CPU/memory, and the orphaned-volume lifecycle.

The volume-discovery logic is the part actually worth pinning down: it has to
recognise a volume Moonphase made without relying on the moonphase=1 label,
since volumes created before that label existed have none — see the
_PROJECT_VOLUME_RE comment in monitor.py.
"""

from __future__ import annotations

from typing import Any

from moonphase import docker_remote
from moonphase.monitor import SessionMonitor, _PROJECT_VOLUME_RE


class _NullSession:
    async def __aenter__(self):
        class _Conn:
            async def execute(self, *args: Any, **kwargs: Any) -> None:
                return None

        return _Conn()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _server(server_id: str, *, status: str = "online") -> dict[str, Any]:
    return {"id": server_id, "name": server_id, "status": status}


async def fake_pool_get(_target: Any) -> Any:
    return object()


def _project(
    project_id: str,
    server_id: str,
    *,
    workspace: str,
    home: str,
    container: str | None = None,
) -> dict[str, Any]:
    return {
        "id": project_id,
        "server_id": server_id,
        "name": project_id,
        "workspace_volume": workspace,
        "home_volume": home,
        "container_name": container,
    }


# --- naming convention ---------------------------------------------------------


def test_project_volume_pattern_matches_the_real_naming_scheme() -> None:
    assert _PROJECT_VOLUME_RE.match("mp-demo-abcd1234-workspace")
    assert _PROJECT_VOLUME_RE.match("mp-my-project-slug-deadbeef-home")


def test_project_volume_pattern_rejects_unrelated_volumes() -> None:
    assert not _PROJECT_VOLUME_RE.match("moonphase_db-data")
    assert not _PROJECT_VOLUME_RE.match("some-other-volume")
    assert not _PROJECT_VOLUME_RE.match("mp-demo-abcd1234-logs")  # wrong suffix


# --- discovery -------------------------------------------------------------------


async def test_discovery_tracks_only_unclaimed_moonphase_volumes(monkeypatch) -> None:
    """A live project's volumes and non-Moonphase volumes must never be tracked.

    Only 'mp-orphan-deadbeef-workspace' matches the naming scheme, is not on
    any live project, and is not already tracked — that is the one that
    should start a grace period.
    """
    monitor = SessionMonitor()
    tracked: list[dict[str, Any]] = []

    async def fake_volume_usage(_conn):
        return [
            docker_remote.VolumeUsage(
                name="mp-live-cafe1234-workspace", project_label=None, size_bytes=100
            ),
            docker_remote.VolumeUsage(
                name="mp-orphan-deadbeef-workspace", project_label=None, size_bytes=200
            ),
            docker_remote.VolumeUsage(
                name="moonphase_db-data", project_label=None, size_bytes=300
            ),
        ]

    async def fake_target(_row):
        return object()

    async def fake_list_orphaned(_conn, _server_id):
        return []

    async def fake_track(_conn, **kwargs):
        tracked.append(kwargs)

    monkeypatch.setattr(monitor, "_target_for", fake_target)
    monkeypatch.setattr("moonphase.ssh.pool.get", fake_pool_get)
    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    monkeypatch.setattr(docker_remote, "volume_usage", fake_volume_usage)
    monkeypatch.setattr("moonphase.monitor.queries.list_orphaned_volumes", fake_list_orphaned)
    monkeypatch.setattr("moonphase.monitor.queries.track_orphaned_volume", fake_track)

    await monitor._discover_orphans(
        _server("srv-1"), live_volume_names={"mp-live-cafe1234-workspace"}
    )

    assert len(tracked) == 1
    assert tracked[0]["volume_name"] == "mp-orphan-deadbeef-workspace"
    assert tracked[0]["reason"] == "discovered"


async def test_discovery_does_not_retrack_a_volume_already_on_record(monkeypatch) -> None:
    """Retracking would reset the grace-period clock every sweep, forever."""
    monitor = SessionMonitor()
    tracked: list[dict[str, Any]] = []

    async def fake_volume_usage(_conn):
        return [
            docker_remote.VolumeUsage(
                name="mp-orphan-deadbeef-workspace", project_label=None, size_bytes=200
            )
        ]

    async def fake_list_orphaned(_conn, _server_id):
        return [{"volume_name": "mp-orphan-deadbeef-workspace"}]

    async def fake_track(_conn, **kwargs):
        tracked.append(kwargs)

    async def fake_target(_row):
        return object()

    monkeypatch.setattr(monitor, "_target_for", fake_target)
    monkeypatch.setattr("moonphase.ssh.pool.get", fake_pool_get)
    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    monkeypatch.setattr(docker_remote, "volume_usage", fake_volume_usage)
    monkeypatch.setattr("moonphase.monitor.queries.list_orphaned_volumes", fake_list_orphaned)
    monkeypatch.setattr("moonphase.monitor.queries.track_orphaned_volume", fake_track)

    await monitor._discover_orphans(_server("srv-1"), live_volume_names=set())

    assert tracked == []


# --- reaping ---------------------------------------------------------------------


async def test_reap_removes_and_untracks_on_success(monkeypatch) -> None:
    monitor = SessionMonitor()
    removed_rows: list[str] = []

    async def fake_due(_conn, _now):
        return [
            {
                "id": "row-1",
                "server_id": "11111111-1111-1111-1111-111111111111",
                "volume_name": "mp-orphan-deadbeef-workspace",
                "reason": "discovered",
                "discovered_at": "2026-01-01",
            }
        ]

    async def fake_target(_row):
        return object()

    async def fake_remove(_conn, name):
        return True

    async def fake_delete_row(_conn, row_id):
        removed_rows.append(row_id)

    monkeypatch.setattr(monitor, "_target_for", fake_target)
    monkeypatch.setattr("moonphase.ssh.pool.get", fake_pool_get)
    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    monkeypatch.setattr("moonphase.monitor.queries.list_orphaned_volumes_due", fake_due)
    monkeypatch.setattr(docker_remote, "volume_remove", fake_remove)
    monkeypatch.setattr("moonphase.monitor.queries.delete_orphaned_volume_row", fake_delete_row)

    removed = await monitor._reap_due_volumes()

    assert removed == 1
    assert removed_rows == ["row-1"]


async def test_reap_keeps_tracking_a_volume_that_failed_to_remove(monkeypatch) -> None:
    """Still in use, most likely — must not be forgotten, or it leaks forever."""
    monitor = SessionMonitor()
    removed_rows: list[str] = []

    async def fake_due(_conn, _now):
        return [
            {
                "id": "row-1",
                "server_id": "11111111-1111-1111-1111-111111111111",
                "volume_name": "mp-stuck-deadbeef-workspace",
                "reason": "discovered",
                "discovered_at": "2026-01-01",
            }
        ]

    async def fake_target(_row):
        return object()

    async def fake_remove(_conn, name):
        return False

    async def fake_delete_row(_conn, row_id):
        removed_rows.append(row_id)

    monkeypatch.setattr(monitor, "_target_for", fake_target)
    monkeypatch.setattr("moonphase.ssh.pool.get", fake_pool_get)
    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    monkeypatch.setattr("moonphase.monitor.queries.list_orphaned_volumes_due", fake_due)
    monkeypatch.setattr(docker_remote, "volume_remove", fake_remove)
    monkeypatch.setattr("moonphase.monitor.queries.delete_orphaned_volume_row", fake_delete_row)

    removed = await monitor._reap_due_volumes()

    assert removed == 0
    assert removed_rows == []


# --- resource reading, per-project attribution -------------------------------------


async def test_resource_reading_matches_volumes_and_stats_to_their_project(
    monkeypatch,
) -> None:
    monitor = SessionMonitor()
    upserted: dict[str, Any] = {}

    projects = [
        _project(
            "proj-a", "srv-1", workspace="mp-a-cafe1234-workspace",
            home="mp-a-cafe1234-home", container="mp-a-cafe1234",
        ),
        _project(
            "proj-b", "srv-1", workspace="mp-b-beef1234-workspace",
            home="mp-b-beef1234-home", container=None,
        ),
    ]

    async def fake_target(_row):
        return object()

    async def fake_disk_usage(_conn):
        return docker_remote.DiskUsage(total_bytes=1000, used_bytes=400)

    async def fake_volume_usage(_conn):
        return [
            docker_remote.VolumeUsage(
                name="mp-a-cafe1234-workspace", project_label=None, size_bytes=100
            ),
            docker_remote.VolumeUsage(
                name="mp-a-cafe1234-home", project_label=None, size_bytes=50
            ),
            docker_remote.VolumeUsage(
                name="mp-b-beef1234-workspace", project_label=None, size_bytes=10
            ),
        ]

    async def fake_container_stats(_conn, names):
        assert names == ["mp-a-cafe1234"]
        return {"mp-a-cafe1234": docker_remote.ContainerStat(
            name="mp-a-cafe1234", cpu_percent=1.5, mem_bytes=1024
        )}

    async def fake_upsert(_conn, **kwargs):
        upserted.update(kwargs)

    monkeypatch.setattr(monitor, "_target_for", fake_target)
    monkeypatch.setattr("moonphase.ssh.pool.get", fake_pool_get)
    monkeypatch.setattr("moonphase.monitor.service_session", lambda: _NullSession())
    monkeypatch.setattr(docker_remote, "disk_usage", fake_disk_usage)
    monkeypatch.setattr(docker_remote, "volume_usage", fake_volume_usage)
    monkeypatch.setattr(docker_remote, "container_stats", fake_container_stats)
    monkeypatch.setattr("moonphase.monitor.queries.upsert_resource_snapshot", fake_upsert)

    await monitor._read_server_resources(_server("srv-1"), projects)

    assert upserted["disk_total_bytes"] == 1000
    assert upserted["disk_used_bytes"] == 400
    by_project = {p["project_id"]: p for p in upserted["by_project"]}
    assert by_project["proj-a"]["workspace_bytes"] == 100
    assert by_project["proj-a"]["home_bytes"] == 50
    assert by_project["proj-a"]["cpu_percent"] == 1.5
    assert by_project["proj-a"]["mem_bytes"] == 1024
    assert by_project["proj-b"]["workspace_bytes"] == 10
    assert by_project["proj-b"]["cpu_percent"] is None, "no container, no live stat"
    # Largest total first.
    assert upserted["by_project"][0]["project_id"] == "proj-a"
