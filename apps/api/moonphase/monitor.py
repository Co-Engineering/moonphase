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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from . import activity, docker_remote, push, queries, sessions, ssh, usage
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

# Usage is read far less often than activity. A token count two minutes old is
# perfectly useful; re-reading transcripts every sweep would turn the cheapest
# question here into the most expensive one.
USAGE_INTERVAL_SECONDS = 120.0


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
        # When each container's transcripts were last read for usage.
        self._usage_checked: dict[str, float] = {}

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
            try:
                await self.check_budgets()
            except Exception as exc:  # noqa: BLE001
                log.warning("budget check failed: %s", exc)
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
            # The project says it is running and it is not. Nothing else looks
            # at every project regularly, so if this does not correct the
            # record nothing will, and the interface goes on offering a
            # terminal for a container that no longer exists.
            await self._reconcile_project(
                group[0],
                status="stopped",
                detail=(
                    "The container is gone from the server."
                    if info is None
                    else f"The container is {info.state}."
                ),
            )
            for row in group:
                await self._settle(row, activity.Snapshot(
                    state=ActivityState.STOPPED, digest=""
                ))
            return len(group)

        panes = await sessions.capture_all_panes(conn_ssh, container)
        await self._collect_usage(conn_ssh, container, group)

        # A host reboot brings the container back — that is what the restart
        # policy is for — but everything inside it started fresh, so the agents
        # are gone. The project is genuinely running and every session in it is
        # not, which is worth recording as exactly that rather than as either
        # one alone.
        if not panes and group:
            await self._reconcile_project(
                group[0],
                status="running",
                detail=(
                    "The container restarted, so the agents in it are not running. "
                    "Resume a session to pick it back up."
                ),
            )
        elif group:
            await self._reconcile_project(group[0], status="running", detail=None)

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

    async def _collect_usage(
        self, conn_ssh: Any, container: str, group: list[dict[str, Any]]
    ) -> None:
        """Read what each session has spent since we last looked.

        Runs less often than the activity sweep: a token count that is two
        minutes stale is fine, and reading transcripts every twenty seconds
        would make the cheapest question Moonphase asks into the most
        expensive one.
        """
        now = time.monotonic()
        if now - self._usage_checked.get(container, 0.0) < USAGE_INTERVAL_SECONDS:
            return
        self._usage_checked[container] = now

        for row in group:
            directory = row.get("transcript_path")
            if not directory or not row.get("user_id"):
                continue
            try:
                collected = await usage.collect_session(
                    conn_ssh,
                    container,
                    str(directory),
                    known=dict(row.get("usage_cursors") or {}),
                )
            except SSHError as exc:
                log.debug("usage: could not read %s: %s", directory, exc)
                continue
            if collected is None:
                continue

            async with service_session() as conn:
                if collected.events:
                    await queries.record_usage_privileged(
                        conn,
                        user_id=str(row["user_id"]),
                        project_id=row["id"],
                        project_name=str(row["name"]),
                        session_id=row["session_id"],
                        events=collected.events,
                    )
                await queries.set_usage_cursors_privileged(
                    conn,
                    session_id=row["session_id"],
                    cursors=collected.cursors,
                )

    async def check_budgets(self) -> int:
        """Warn people before a limit stops them, not after.

        A limit you discover by hitting it is the worst kind: the session stops
        mid-task on a machine you are not sitting at. Everything needed to see
        it coming is already collected, so the only new thing is a threshold
        and a note of which window has already been announced.

        Fired per window rather than per check. A threshold crossed at 60%
        stays crossed, and without the anchor this would send the same
        notification every two minutes for the rest of the window.
        """
        now = datetime.now(UTC)
        sent = 0
        async with service_session() as conn:
            rows = await queries.limits_to_check_privileged(conn)

            for row in rows:
                threshold = int(row["alert_percent"])
                for column, length, limit_key, label in (
                    ("alerted_window", usage.SESSION_WINDOW, "session_tokens", "5-hour"),
                    ("alerted_week", usage.WEEK_WINDOW, "weekly_tokens", "weekly"),
                ):
                    allowance = row.get(limit_key)
                    if not allowance:
                        continue
                    times = await queries.usage_times_for_privileged(
                        conn, row["user_id"], now - length
                    )
                    window = usage.current_window(times, length, now)
                    if window is None:
                        continue
                    # Already announced for this window. Comparing anchors
                    # rather than storing a flag means a new window rearms it
                    # by itself.
                    if row.get(column) == window.started_at:
                        continue

                    used = await queries.usage_total_between_privileged(
                        conn, row["user_id"], window.started_at, window.resets_at
                    )
                    percent = used / int(allowance) * 100
                    if percent < threshold:
                        continue

                    await queries.mark_alerted_privileged(
                        conn,
                        user_id=row["user_id"],
                        column=column,
                        anchor=window.started_at,
                    )
                    await self._push_budget(
                        conn, row["user_id"], percent, label, window.resets_at
                    )
                    sent += 1
        return sent

    async def _push_budget(
        self, conn: Any, user_id: Any, percent: float, label: str, resets_at: Any
    ) -> None:
        """Tell the one person whose allowance it is."""
        if not push.configured():
            log.debug("push not configured; would have warned about %s limit", label)
            return

        subscriptions = await _subscriptions_for_session(conn, user_id)
        when = resets_at.strftime("%H:%M") if hasattr(resets_at, "strftime") else ""
        for sub in subscriptions:
            await push.send(
                push.Subscription(
                    endpoint=sub["endpoint"], p256dh=sub["p256dh"], auth=sub["auth"]
                ),
                title=f"{percent:.0f}% of your {label} limit used",
                body=f"It resets at {when}." if when else "",
                # Not a question, so it should not sit on screen demanding an
                # answer the way a waiting session does.
                kind="budget",
                url="/",
                tag=f"moonphase-budget-{label}",
            )

    async def _reconcile_project(
        self, row: dict[str, Any], *, status: str, detail: str | None
    ) -> None:
        """Make the record match the machine, and only when it does not."""
        if row.get("project_status") == status and row.get("status_detail") == detail:
            return
        async with service_session() as conn:
            await conn.execute(
                text(
                    "update projects set status = cast(:s as project_status), "
                    "status_detail = :d where id = :id"
                ),
                {"s": status, "d": detail, "id": row["id"]},
            )
        log.info("monitor: %s is %s (%s)", row["name"], status, detail or "as recorded")

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
                # Deep link to the session, not the project: the whole point
                # of the notification is that something specific is waiting,
                # and `/projects/<id>` was a path the client had no route for,
                # so tapping one landed on an empty app.
                url=(
                    f"/?project={row['id']}&session={row['tmux_session']}"
                ),
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
                   p.status::text as project_status, p.status_detail,
                   s.id as session_id, s.tmux_session, s.user_id,
                   s.transcript_path, s.usage_cursors,
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
