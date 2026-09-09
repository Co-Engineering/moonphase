"""`_track_kept_volumes` — starting the grace period for a volume a delete
did not remove.

Factored out of delete_project specifically so the retention math and the
service-role write are testable without a database or HTTP client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from moonphase.routers.projects import _track_kept_volumes


class _NullSession:
    async def __aenter__(self):
        class _Conn:
            async def execute(self, *args: Any, **kwargs: Any) -> None:
                return None

        return _Conn()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


async def test_tracks_every_volume_with_the_given_reason(monkeypatch) -> None:
    tracked: list[dict[str, Any]] = []

    async def fake_track(_conn, **kwargs):
        tracked.append(kwargs)

    monkeypatch.setattr(
        "moonphase.routers.projects.service_session", lambda: _NullSession()
    )
    monkeypatch.setattr(
        "moonphase.routers.projects.queries.track_orphaned_volume", fake_track
    )

    project = {"server_id": "srv-1", "name": "demo"}
    await _track_kept_volumes(
        project, ["mp-demo-abcd1234-workspace", "mp-demo-abcd1234-home"],
        reason="project_deleted",
    )

    assert [t["volume_name"] for t in tracked] == [
        "mp-demo-abcd1234-workspace",
        "mp-demo-abcd1234-home",
    ]
    assert all(t["reason"] == "project_deleted" for t in tracked)
    assert all(t["project_name"] == "demo" for t in tracked)
    assert all(t["server_id"] == "srv-1" for t in tracked)


async def test_delete_after_honours_the_configured_retention(monkeypatch) -> None:
    tracked: list[dict[str, Any]] = []

    async def fake_track(_conn, **kwargs):
        tracked.append(kwargs)

    class _Settings:
        moonphase_orphan_volume_retention_days = 3

    monkeypatch.setattr(
        "moonphase.routers.projects.service_session", lambda: _NullSession()
    )
    monkeypatch.setattr(
        "moonphase.routers.projects.queries.track_orphaned_volume", fake_track
    )
    monkeypatch.setattr(
        "moonphase.routers.projects.get_settings", lambda: _Settings()
    )

    before = datetime.now(UTC)
    await _track_kept_volumes(
        {"server_id": "srv-1", "name": "demo"}, ["mp-demo-abcd1234-workspace"],
        reason="cleanup_failed",
    )
    after = datetime.now(UTC)

    delete_after = tracked[0]["delete_after"]
    assert before + timedelta(days=3) <= delete_after <= after + timedelta(days=3)
