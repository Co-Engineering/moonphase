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
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from . import activity, docker_remote, push, queries, runtime, sessions, ssh, usage
from . import harness as harness_registry
from .activity import ActivityState
from .config import get_settings
from .db import service_session
from .harness import SessionSpace
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

# How long to leave a container alone after an auto-resume attempt that could
# not bring everything back.
#
# The trigger for auto-resume — running container, session rows, no panes —
# stays true for exactly the sessions that failed to resume, so without this
# the monitor retries them every sweep, forever. A session whose owner
# revoked their harness credential is not going to start on the next attempt
# either, and 20-second retries turn one unresumable session into a few
# thousand SSH round-trips a day against a server that has nothing to gain
# from them.
RESUME_RETRY_INTERVAL_SECONDS = 600.0

# Disk does not move in twenty seconds. Once every five minutes per server is
# plenty to catch a fill-up coming, and it is a real round trip — docker
# system df and docker stats — against every managed server, not a cheap DB
# read like the activity sweep.
RESOURCE_INTERVAL_SECONDS = 300.0

# This is cleanup, not monitoring — nothing about a leaked volume needs to be
# noticed within minutes. Once an hour per server is enough to keep the disk
# from filling back up between resource-usage readings.
VOLUME_SWEEP_INTERVAL_SECONDS = 3600.0

# A volume Moonphase created for a project, matching the naming _container_name
# in routers/projects.py has always used: mp-<slug>-<8 hex chars>-workspace/-home.
# Matched by name rather than by the moonphase=1 label so volumes created by
# every earlier release — before that label existed — are still recognised.
_PROJECT_VOLUME_RE = re.compile(r"^mp-.+-[0-9a-f]{8}-(workspace|home)$")


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
        # When we last tried to bring a container's sessions back, so a
        # container that cannot resume is not retried every sweep.
        self._resume_attempted: dict[str, float] = {}
        # When each server's disk/CPU/memory was last read, and when it was
        # last swept for orphaned volumes — both keyed by server id.
        self._resources_checked: dict[str, float] = {}
        self._volumes_checked: dict[str, float] = {}

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
            try:
                await self.sweep_resources()
            except Exception as exc:  # noqa: BLE001
                log.warning("resource sweep failed: %s", exc)
            try:
                await self.sweep_volumes()
            except Exception as exc:  # noqa: BLE001
                log.warning("volume sweep failed: %s", exc)
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
        # are gone. Bring each *missing* session back with `--continue` the
        # same way the "Resume" button would, so a reboot is invisible rather
        # than an errand to run once per session.
        #
        # Judged per session rather than "every pane in the container is
        # gone": a plain terminal attach recreates a tmux pane for whichever
        # session someone happens to open first (see terminal.py), and that
        # one pane used to be enough to make the whole container look
        # already-resumed, silently cancelling auto-resume for every other
        # session in it until a person opened each one by hand.
        missing = [row for row in group if panes.get(str(row["tmux_session"])) is None]
        if missing and self._resume_due(container):
            resumed, failed = await self._auto_resume(conn_ssh, container, missing)
            # Only a failure starts the clock. Clearing it on a clean resume
            # keeps the *next* reboot immediate rather than making it serve
            # out the backoff earned by an unrelated earlier one.
            if failed:
                self._resume_attempted[container] = time.monotonic()
            else:
                self._resume_attempted.pop(container, None)
            if resumed:
                # What got resumed is now actually running; re-read rather than
                # let the per-session loop below judge against the pre-resume
                # (empty) snapshot and settle everything as stopped.
                panes = await sessions.capture_all_panes(conn_ssh, container)
            if failed and resumed:
                detail = (
                    f"The container restarted; {resumed} of {resumed + failed} "
                    "sessions resumed automatically. The rest need a manual Resume."
                )
            elif failed:
                detail = (
                    "The container restarted, so the agents in it are not running. "
                    "Resume a session to pick it back up."
                )
            else:
                detail = None
            await self._reconcile_project(group[0], status="running", detail=detail)
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

    def _resume_due(self, container: str) -> bool:
        """Whether enough time has passed to try resuming this container again.

        A first sight of an empty container always tries immediately — a
        reboot should be invisible, not delayed ten minutes. It is only the
        retry after a failure that waits.
        """
        last = self._resume_attempted.get(container)
        return last is None or time.monotonic() - last >= RESUME_RETRY_INTERVAL_SECONDS

    async def _auto_resume(
        self, conn_ssh: Any, container: str, missing: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Bring each of the given, currently paneless sessions back on its own.

        `missing` is whichever sessions in the container have no tmux pane
        right now — not necessarily all of them, since one session can already
        be back (someone opened it, or an earlier attempt resumed it) while
        others in the same container are still gone.

        Best effort, one session at a time: one person's revoked credential or
        deleted project must not stop their neighbours' sessions in the same
        container from resuming, so a failure is counted and logged rather
        than raised. Returns (resumed, failed).
        """
        resumed = 0
        failed = 0
        for row in missing:
            user_id = row.get("user_id")
            session_name = str(row["tmux_session"])
            if user_id is None:
                # A session from before sessions had owners has no account to
                # resume on — left for a person to do by hand, as always.
                failed += 1
                continue
            try:
                async with service_session() as conn:
                    org_id = await queries.personal_org_id_for_user_privileged(
                        conn, str(user_id)
                    )
                    project = await queries.get_project(conn, row["id"])
                if org_id is None:
                    raise SSHError("the session's owner has no personal organization")
                if project is None:
                    raise SSHError("the project no longer exists")

                profile = await runtime.load_session_profile_privileged(
                    org_id, project, str(row["harness"]), session_name
                )
                if not profile.has_harness_auth:
                    raise SSHError("no harness credential to resume with")

                space = SessionSpace(
                    home=str(row["home_dir"]), workdir=str(row["workdir"])
                )
                await sessions.ensure_session(
                    conn_ssh,
                    container,
                    harness_kind=str(row["harness"]),
                    workspace_profile=profile,
                    session=session_name,
                    space=space,
                    resume=True,
                )
                resumed += 1
            except Exception as exc:  # noqa: BLE001 — one session must not sink the rest
                log.info(
                    "monitor: could not auto-resume %s in %s: %s",
                    session_name, container, exc,
                )
                failed += 1
        return resumed, failed

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

    async def sweep_resources(self) -> int:
        """Disk/CPU/memory per server, and how it splits across projects.

        Every managed server, not just ones with a running project — a
        server can be filling up with kept-on-delete volumes while nothing
        on it is currently running at all.
        """
        now = time.monotonic()
        async with service_session() as conn:
            servers = await queries.list_servers(conn)
            projects = await queries.list_projects(conn)

        by_server: dict[str, list[dict[str, Any]]] = {}
        for project in projects:
            by_server.setdefault(str(project["server_id"]), []).append(project)

        checked = 0
        for server in servers:
            server_id = str(server["id"])
            if server.get("status") != "online":
                continue
            if now - self._resources_checked.get(server_id, 0.0) < RESOURCE_INTERVAL_SECONDS:
                continue
            self._resources_checked[server_id] = now
            try:
                await self._read_server_resources(server, by_server.get(server_id, []))
                checked += 1
            except SSHError as exc:
                log.debug("resource sweep: could not reach %s: %s", server["name"], exc)
        return checked

    async def _read_server_resources(
        self, server: dict[str, Any], projects: list[dict[str, Any]]
    ) -> None:
        conn_ssh = await ssh.pool.get(await self._target_for({"server_id": server["id"]}))
        disk = await docker_remote.disk_usage(conn_ssh)
        if disk is None:
            return
        volumes_by_name = {v.name: v for v in await docker_remote.volume_usage(conn_ssh)}
        containers = [p["container_name"] for p in projects if p.get("container_name")]
        stats = await docker_remote.container_stats(conn_ssh, containers)

        by_project: list[dict[str, Any]] = []
        for project in projects:
            workspace = volumes_by_name.get(project.get("workspace_volume") or "")
            home = volumes_by_name.get(project.get("home_volume") or "")
            stat = stats.get(project.get("container_name") or "")
            by_project.append(
                {
                    "project_id": str(project["id"]),
                    "name": project["name"],
                    "workspace_bytes": workspace.size_bytes if workspace else 0,
                    "home_bytes": home.size_bytes if home else 0,
                    "cpu_percent": stat.cpu_percent if stat else None,
                    "mem_bytes": stat.mem_bytes if stat else None,
                }
            )
        by_project.sort(key=lambda p: p["workspace_bytes"] + p["home_bytes"], reverse=True)

        async with service_session() as conn:
            await queries.upsert_resource_snapshot(
                conn,
                server_id=server["id"],
                disk_total_bytes=disk.total_bytes,
                disk_used_bytes=disk.used_bytes,
                by_project=by_project,
            )

    async def sweep_volumes(self) -> int:
        """Find volumes no project claims, and remove ones past their grace period.

        Two independent halves. Discovery is per-server and throttled the
        same way sweep_resources is, since it needs an SSH round trip.
        Reaping is a plain read of what is already due — no SSH needed to
        find the work, only to carry it out — so it is not throttled at all;
        it simply does nothing when nothing is due.
        """
        now = time.monotonic()
        async with service_session() as conn:
            servers = await queries.list_servers(conn)
            projects = await queries.list_projects(conn)

        live_by_server: dict[str, set[str]] = {}
        for project in projects:
            names = live_by_server.setdefault(str(project["server_id"]), set())
            for volume in (project.get("workspace_volume"), project.get("home_volume")):
                if volume:
                    names.add(volume)

        swept = 0
        for server in servers:
            server_id = str(server["id"])
            if server.get("status") != "online":
                continue
            if now - self._volumes_checked.get(server_id, 0.0) < VOLUME_SWEEP_INTERVAL_SECONDS:
                continue
            self._volumes_checked[server_id] = now
            try:
                await self._discover_orphans(server, live_by_server.get(server_id, set()))
            except SSHError as exc:
                log.debug("volume sweep: could not reach %s: %s", server["name"], exc)

        swept += await self._reap_due_volumes()
        return swept

    async def _discover_orphans(
        self, server: dict[str, Any], live_volume_names: set[str]
    ) -> None:
        conn_ssh = await ssh.pool.get(await self._target_for({"server_id": server["id"]}))
        volumes = await docker_remote.volume_usage(conn_ssh)
        retention = timedelta(days=get_settings().moonphase_orphan_volume_retention_days)

        async with service_session() as conn:
            existing = await queries.list_orphaned_volumes(conn, server["id"])
            tracked_names = {row["volume_name"] for row in existing}
            for volume in volumes:
                if not _PROJECT_VOLUME_RE.match(volume.name):
                    continue  # not one of ours to begin with
                if volume.name in live_volume_names or volume.name in tracked_names:
                    continue
                # Ours, claimed by no live project, and not already on
                # record — a project delete from before this feature
                # existed, or a container-creation crash that never made it
                # as far as the projects row. Either way it gets the same
                # grace period as a deliberately kept one, not an instant
                # delete on the sweep's first look at it.
                await queries.track_orphaned_volume(
                    conn,
                    server_id=server["id"],
                    volume_name=volume.name,
                    project_name=volume.project_label,
                    reason="discovered",
                    delete_after=datetime.now(UTC) + retention,
                )

    async def _reap_due_volumes(self) -> int:
        async with service_session() as conn:
            due = await queries.list_orphaned_volumes_due(conn, datetime.now(UTC))
        if not due:
            return 0

        by_server: dict[str, list[dict[str, Any]]] = {}
        for row in due:
            by_server.setdefault(str(row["server_id"]), []).append(row)

        removed = 0
        for server_id_str, rows in by_server.items():
            try:
                conn_ssh = await ssh.pool.get(
                    await self._target_for({"server_id": UUID(server_id_str)})
                )
            except SSHError as exc:
                log.debug("volume reap: could not reach server %s: %s", server_id_str, exc)
                continue
            for row in rows:
                removed_ok = await docker_remote.volume_remove(conn_ssh, row["volume_name"])
                if not removed_ok:
                    # Still in use, most likely — a container that was
                    # supposed to be gone but is not. Leave it tracked; the
                    # next sweep tries again rather than losing track of it.
                    log.warning(
                        "could not remove orphaned volume %s; will retry", row["volume_name"]
                    )
                    continue
                async with service_session() as conn:
                    await queries.delete_orphaned_volume_row(conn, row["id"])
                removed += 1
                log.info(
                    "removed orphaned volume %s (%s, kept since %s)",
                    row["volume_name"],
                    row["reason"],
                    row["discovered_at"],
                )
        return removed

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
            result = await push.send(
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
            if not result.alive:
                dead.append(sub["endpoint"])
            elif not result.delivered:
                log.warning(
                    "push to a live subscription failed for project %s: %s",
                    row["id"],
                    result.error,
                )

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
                   s.home_dir, s.workdir,
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
