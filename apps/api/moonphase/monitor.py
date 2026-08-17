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

from . import activity, docker_remote, push, queries, sessions, ssh
from . import harness as harness_registry
from .activity import ActivityState
from .config import get_settings
from .db import service_session
from .ssh import SSHError

log = logging.getLogger(__name__)

# A failing server is usually failing for a reason that will not fix itself in
# twenty seconds — a rotated key, a stopped box. Back off, but keep checking
# often enough that a server coming back is noticed within a minute or two.
BASE_BACKOFF_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 600.0


class SessionMonitor:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # When each project's pane was last seen to change, so "still for long
        # enough" can be judged without another round trip.
        self._still_since: dict[str, float] = {}
        # Consecutive failures per server, and when to try it again. A server
        # whose key no longer works fails identically every sweep; retrying it
        # on each one wastes a connection attempt per project on it and buries
        # real problems in the log.
        self._failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}

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
        """One pass over every running project. Returns how many were checked.

        Grouped by container, because that is the unit the questions are about.
        Asking per session meant re-inspecting the same container once per
        agent in it and re-listing the same tmux server, so a project with four
        sessions cost twelve round trips a sweep. Two answer the same thing.
        """
        async with service_session() as conn:
            rows = await _running_projects(conn)

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (str(row["server_id"]), str(row["container_name"]))
            groups.setdefault(key, []).append(row)

        now = time.monotonic()
        checked = 0
        for (server, container), group in groups.items():
            if now < self._retry_after.get(server, 0.0):
                continue

            try:
                target = await self._target_for(group[0])
                if target is None:
                    continue
                conn_ssh = await ssh.pool.get(target)
            except SSHError as exc:
                # The machine is unreachable, which is a fact about the server
                # and not about any one project on it.
                self._back_off(server, str(group[0]["name"]), exc)
                continue

            try:
                checked += await self._check_container(conn_ssh, container, group)
                self._failures.pop(server, None)
                self._retry_after.pop(server, None)
            except SSHError as exc:
                # A failure talking to one container says nothing about the
                # others on the same machine, so it must not silence them —
                # which is how three sessions came to sit frozen for hours
                # while a fourth updated normally.
                log.info("monitor: %s is not answering (%s)", container, exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("monitor: %s failed: %s", container, exc)
        return checked

    async def _target_for(self, row: dict[str, Any]) -> Any:
        async with service_session() as conn:
            return await queries.load_ssh_target_privileged(conn, row["server_id"])

    async def _check_container(
        self, conn_ssh: Any, container: str, group: list[dict[str, Any]]
    ) -> int:
        """Two round trips for however many sessions the container holds."""
        info = await docker_remote.inspect(conn_ssh, container)
        if info is None or info.state != "running":
            for row in group:
                await self._settle(row, activity.Snapshot(
                    state=ActivityState.STOPPED, digest=""
                ))
            return len(group)

        panes = await sessions.capture_all_panes(conn_ssh, container)

        for row in group:
            name = str(row["tmux_session"])
            pane = panes.get(name)
            if pane is None:
                # Listed as running in the database, absent from tmux.
                await self._settle(row, activity.Snapshot(
                    state=ActivityState.STOPPED, digest=""
                ))
                continue

            harness = harness_registry.get(str(row["harness"]))
            session_key = str(row["session_id"])
            # `setdefault`, not `get`. The clock that measures "how long has
            # this pane been still" used to be started only when the pane
            # changed, so a session that stopped changing never accumulated any
            # stillness at all and its state froze at whatever it last was.
            still_since = self._still_since.setdefault(session_key, time.monotonic())

            snapshot = activity.classify(
                pane,
                signals=harness.activity_signals(),
                previous_digest=row.get("pane_digest"),
                still_for_seconds=time.monotonic() - still_since,
            )
            if snapshot.digest and snapshot.digest != row.get("pane_digest"):
                self._still_since[session_key] = time.monotonic()
            await self._settle(row, snapshot)

        return len(group)

    async def _settle(self, row: dict[str, Any], snapshot: Any) -> None:
        """Write what we saw, and notify if it is worth waking someone for."""
        previous = ActivityState(row["activity"] or "unknown")

        if snapshot.state == previous and snapshot.digest == row.get("pane_digest"):
            # Nothing changed, but we did look — and "when was this last
            # confirmed" is the difference between a state and a guess.
            async with service_session() as conn:
                await _touch_checked(conn, row["session_id"])
            return

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
        await self._notify(row, title, body, kind=str(snapshot.state))

        async with service_session() as conn:
            await conn.execute(
                text(
                    "update project_sessions set notified_state = "
                    "cast(:s as activity_state) where id = :id"
                ),
                {"s": str(snapshot.state), "id": row["session_id"]},
            )

    def _back_off(self, server: str, name: str, exc: Exception) -> None:
        """Wait longer after each consecutive failure, up to a ceiling."""
        count = self._failures.get(server, 0) + 1
        self._failures[server] = count
        delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (count - 1)))
        self._retry_after[server] = time.monotonic() + delay
        # Only the first failure is worth a line; after that it is the same
        # message repeating.
        if count == 1:
            log.info("monitor: %s unreachable (%s); backing off", name, exc)
        else:
            log.debug("monitor: %s still unreachable after %d tries", name, count)

    async def _notify(
        self, row: dict[str, Any], title: str, body: str, *, kind: str | None = None
    ) -> None:
        if not push.configured():
            log.debug("push not configured; would have sent: %s", title)
            return

        async with service_session() as conn:
            subscriptions = await _subscriptions_for_session(conn, row.get("user_id"))

        dead: list[str] = []
        for sub in subscriptions:
            alive = await push.send(
                push.Subscription(
                    endpoint=sub["endpoint"], p256dh=sub["p256dh"], auth=sub["auth"]
                ),
                title=title,
                body=body,
                kind=kind,
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
                   s.id as session_id, s.tmux_session, s.user_id,
                   s.activity, s.pane_digest, s.notified_state
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
                checked_at = now(),
                pane_digest = :digest,
                activity_detail = :detail
            where id = :id
            """
        ),
        {"state": state, "digest": digest or None, "detail": detail, "id": session_id},
    )


async def _touch_checked(conn: Any, session_id: Any) -> None:
    """Record that we looked, without claiming anything changed.

    `activity_at` answers "since when", which is what a person wants to read.
    This answers "is that still true", which is what the interface needs before
    showing it as fact.
    """
    await conn.execute(
        text("update project_sessions set checked_at = now() where id = :id"),
        {"id": session_id},
    )


async def _subscriptions_for_session(conn: Any, user_id: Any) -> list[dict[str, Any]]:
    """The devices of the one person who can answer.

    A session runs on its owner's account and only its owner can type into it,
    so "Claude is waiting for you" is addressed to exactly one person. Telling
    the rest of the project would be a notification they can do nothing about,
    and the useful signal drowns quickly.
    """
    if user_id is None:
        return []
    result = await conn.execute(
        text(
            "select endpoint, p256dh, auth from push_subscriptions "
            "where user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    return [dict(r._mapping) for r in result]


monitor = SessionMonitor()
