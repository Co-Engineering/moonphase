"""Background session monitor.

Watches every running project and notices when its agent stops working. This
has to happen server-side: the whole point is that nobody has a client open.

Deliberately a polling loop rather than anything cleverer. Detecting "the
terminal stopped changing" is inherently a sampling problem, the interval is
tens of seconds, and one `tmux capture-pane` per project per tick over an
already-open SSH connection is cheap. A push-based design would need the
harness to cooperate, which is exactly the coupling the activity module avoids.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from sqlalchemy import text

from . import activity, push, queries, ssh
from . import harness as harness_registry
from .activity import ActivityState
from .config import get_settings
from .db import service_session
from .ssh import SSHError

log = logging.getLogger(__name__)


class SessionMonitor:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # When each project's pane was last seen to change, so "still for long
        # enough" can be judged without another round trip.
        self._still_since: dict[str, float] = {}

    def start(self) -> None:
        settings = get_settings()
        if settings.moonphase_monitor_interval <= 0:
            log.info("session monitor disabled")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        log.info(
            "session monitor started (every %ss)", settings.moonphase_monitor_interval
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        interval = get_settings().moonphase_monitor_interval
        # Let the API finish starting before the first sweep.
        await asyncio.sleep(5)
        while not self._stop.is_set():
            try:
                await self.sweep()
            except Exception as exc:  # noqa: BLE001 — the loop must outlive a bad tick
                log.warning("monitor sweep failed: %s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def sweep(self) -> int:
        """One pass over every running project. Returns how many were checked."""
        async with service_session() as conn:
            rows = await _running_projects(conn)

        checked = 0
        for row in rows:
            try:
                await self._check(row)
                checked += 1
            except SSHError as exc:
                # An unreachable server is ordinary; do not let it stop the
                # sweep for everything else.
                log.debug("monitor: %s unreachable: %s", row["name"], exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("monitor: %s failed: %s", row["name"], exc)
        return checked

    async def _check(self, row: dict[str, Any]) -> None:
        project_id = str(row["id"])

        async with service_session() as conn:
            target = await queries.load_ssh_target_privileged(conn, row["server_id"])
        if target is None:
            return

        conn_ssh = await ssh.pool.get(target)
        harness = harness_registry.get(str(row["harness"]))

        previous = ActivityState(row["activity"] or "unknown")
        still_since = self._still_since.get(project_id, time.monotonic())

        snapshot = await activity.probe(
            conn_ssh,
            str(row["container_name"]),
            harness,
            previous_digest=row.get("pane_digest"),
            still_for_seconds=time.monotonic() - still_since,
        )

        if snapshot.digest and snapshot.digest != row.get("pane_digest"):
            self._still_since[project_id] = time.monotonic()

        if snapshot.state == previous and snapshot.digest == row.get("pane_digest"):
            return  # nothing to write

        async with service_session() as conn:
            await _record_activity(
                conn,
                session_id=row["session_id"],
                state=str(snapshot.state),
                digest=snapshot.digest,
                detail=snapshot.detail,
            )

        message = activity.notification_for(
            previous, snapshot.state, snapshot.detail, str(row["name"])
        )
        if message is None:
            return

        # One notification per transition, not per sweep.
        if row.get("notified_state") == str(snapshot.state):
            return

        title, body = message
        await self._notify(row, title, body)

        async with service_session() as conn:
            await conn.execute(
                text(
                    "update project_sessions set notified_state = "
                    "cast(:s as activity_state) where id = :id"
                ),
                {"s": str(snapshot.state), "id": row["session_id"]},
            )

    async def _notify(self, row: dict[str, Any], title: str, body: str) -> None:
        if not push.configured():
            log.debug("push not configured; would have sent: %s", title)
            return

        async with service_session() as conn:
            subscriptions = await _subscriptions_for_org(conn, row["org_id"])

        dead: list[str] = []
        for sub in subscriptions:
            alive = await push.send(
                push.Subscription(
                    endpoint=sub["endpoint"], p256dh=sub["p256dh"], auth=sub["auth"]
                ),
                title=title,
                body=body,
                url=f"/projects/{row['id']}",
                # Collapse repeats for the same project rather than stacking.
                tag=f"moonphase-{row['id']}",
            )
            if not alive:
                dead.append(sub["endpoint"])

        if dead:
            async with service_session() as conn:
                await conn.execute(
                    text("delete from push_subscriptions where endpoint = any(:e)"),
                    {"e": dead},
                )
            log.info("pruned %d dead push subscriptions", len(dead))


# ---------------------------------------------------------------------------
# Queries used only by the monitor, which has no caller and so no RLS context.
# ---------------------------------------------------------------------------


async def _running_projects(conn: Any) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            select p.id, p.org_id, p.name, p.server_id, p.harness, p.container_name,
                   s.id as session_id, s.activity, s.pane_digest, s.notified_state
            from projects p
            join project_sessions s on s.project_id = p.id
            join servers v on v.id = p.server_id
            where p.status = 'running'
              and v.status = 'online'
              and p.container_name is not null
              and s.state = 'running'
            """
        )
    )
    return [dict(r._mapping) for r in result]


async def _record_activity(
    conn: Any, *, session_id: Any, state: str, digest: str, detail: str | None
) -> None:
    await conn.execute(
        text(
            """
            update project_sessions
            set activity = cast(:state as activity_state),
                activity_at = now(),
                pane_digest = :digest,
                activity_detail = :detail
            where id = :id
            """
        ),
        {"state": state, "digest": digest or None, "detail": detail, "id": session_id},
    )


async def _subscriptions_for_org(conn: Any, org_id: Any) -> list[dict[str, Any]]:
    """Everyone in the org who has enabled notifications on some device."""
    result = await conn.execute(
        text(
            """
            select ps.endpoint, ps.p256dh, ps.auth
            from push_subscriptions ps
            join org_members m on m.user_id = ps.user_id
            where m.org_id = :org_id
            """
        ),
        {"org_id": org_id},
    )
    return [dict(r._mapping) for r in result]


monitor = SessionMonitor()
