"""Each Claude account's plan limits, written down as they are found.

The dashboard shows every account's five-hour and weekly windows. It used to
find them when it was opened: in the status-line file a running session on the
account had written, else by asking Anthropic. Nothing was kept, so once an
account's sessions were closed — or the API restarted — there was nothing to
say about how much of its window was gone, though the window itself was still
counting down.

Now the background monitor collects them and this writes them to the database
(`private.harness_account_usage`), and the dashboard reads only that:

* A Claude Code session's status line saves what Claude last told it
  (ClaudeCode.statusline), and the monitor reads those files as it reads the
  transcripts for token usage. The newest one per account wins.
* An account whose sessions have not reported for a while is asked of
  Anthropic, with a token borrowed from one of its sessions (see plan_usage
  for why never a refreshed one), or the stored sign-in while it is valid.
  A refusal is written down too, with when to try again.
* A session about to be deleted is read one last time, so closing the last
  session on an account does not lose what it said.
"""

from __future__ import annotations

import json
import logging
import shlex
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncssh

from . import docker_remote, plan_usage, profile, queries
from .db import service_session
from .harness import HarnessKind, SessionSpace
from .harness import get as get_harness
from .harness.claude_code import USAGE_FILE

log = logging.getLogger(__name__)

# How old an account's newest reading may get before Anthropic is asked. The
# status lines report on every reply, so an account in use never gets here.
STALE_SECONDS = 600.0

# Every given HOME's status-line file, one per line: home, mtime, contents.
# One exec for the whole container, however many sessions are in it.
_READ_STATUSLINES = (
    'for h in "$@"; do '
    f'f="$h/{USAGE_FILE}"; [ -f "$f" ] || continue; '
    'printf "%s\\t%s\\t" "$h" "$(stat -c %Y "$f")"; tr -d "\\n" < "$f"; echo; '
    "done"
)


def to_json(windows: list[plan_usage.Window]) -> list[dict[str, Any]]:
    return [
        {
            "key": w.key,
            "label": w.label,
            "percent": w.percent,
            "resets_at": w.resets_at.timestamp() if w.resets_at else None,
        }
        for w in windows
    ]


def from_json(raw: Any, now: float) -> list[plan_usage.Window]:
    """Stored windows, as they stand now: one whose reset has passed since it
    was written down is empty again."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    windows: list[plan_usage.Window] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("percent"), int | float):
            continue
        resets = entry.get("resets_at")
        resets_at = (
            datetime.fromtimestamp(resets, tz=UTC) if isinstance(resets, int | float) else None
        )
        percent = float(entry["percent"])
        if resets_at is not None and resets_at.timestamp() <= now:
            percent, resets_at = 0.0, None
        windows.append(
            plan_usage.Window(
                key=str(entry.get("key")),
                label=str(entry.get("label") or entry.get("key")),
                percent=percent,
                resets_at=resets_at,
            )
        )
    return windows


async def read_statuslines(
    conn: asyncssh.SSHClientConnection, container: str, homes: list[str], now: float
) -> dict[str, tuple[list[plan_usage.Window], float]]:
    """{home: (windows, when written)} for every session that has a reading."""
    if not homes:
        return {}
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", _READ_STATUSLINES, "sh", *homes], timeout=30
    )
    found: dict[str, tuple[list[plan_usage.Window], float]] = {}
    for line in result.stdout.splitlines():
        home, _, rest = line.partition("\t")
        mtime, _, body = rest.partition("\t")
        try:
            at = float(mtime)
            windows = plan_usage.parse_statusline(json.loads(body), now)
        except ValueError:
            continue
        if windows:
            found[home] = (windows, at)
    return found


async def account_for_session(conn: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    """The Claude account a session runs on: its own choice, its project's
    when the project is its owner's, else the owner's default — as the
    session's own start resolved it (runtime.load_session_profile).

    `row` needs `user_id`, `project_id`, `project_org_id` and, if the
    session picked one, `session_account_id`.
    """
    user_id = row.get("user_id")
    if user_id is None:
        return None
    org_id = await queries.personal_org_id_for_user_privileged(conn, str(user_id))
    if org_id is None:
        return None
    own_project = str(row.get("project_org_id")) == str(org_id)
    return await queries.resolve_harness_credential_privileged(
        conn,
        org_id=org_id,
        project_id=row["project_id"] if own_project else org_id,
        harness=HarnessKind.CLAUDE_CODE,
        account_id=row.get("session_account_id"),
    )


async def save_reading(
    account_id: UUID, windows: list[plan_usage.Window], at: float, source: str
) -> None:
    async with service_session() as conn:
        await queries.save_account_reading_privileged(
            conn,
            account_id=account_id,
            windows=to_json(windows),
            read_at=datetime.fromtimestamp(at, tz=UTC),
            source=source,
        )


async def ask_anthropic(
    account_id: UUID,
    candidates: list[tuple[str, plan_usage.AccountToken]],
    now: float,
    refusals: int = 0,
) -> bool:
    """Ask with each token in turn, and write down the answer or why not.

    A token Anthropic will not take (expired, or a kind not allowed to read
    usage) moves on to the next; anything else is the account's answer.
    """
    last: plan_usage.UsageUnavailable | None = None
    for source, token in candidates:
        try:
            windows = await plan_usage.fetch(token.token)
        except plan_usage.UsageUnavailable as exc:
            last = exc
            if exc.reason in ("expired", "unsupported"):
                continue
            break
        await save_reading(account_id, windows, now, source)
        return True
    if last is None:
        return False
    retry_at = None
    streak = 0
    if last.reason == "rate_limited":
        # Asking again soon only keeps the account pinned at its limit.
        streak = refusals + 1
        wait = (
            last.retry_after
            if last.retry_after is not None
            else min(
                plan_usage.BACKOFF_CAP_SECONDS,
                plan_usage.BACKOFF_SECONDS * 2 ** (streak - 1),
            )
        )
        retry_at = datetime.fromtimestamp(now + wait, tz=UTC)
    async with service_session() as conn:
        await queries.save_account_problem_privileged(
            conn,
            account_id=account_id,
            problem=last.reason,
            detail=last.detail,
            retry_at=retry_at,
            refusals=streak,
        )
    return False


def due(stored: dict[str, Any] | None, now: float) -> bool:
    """Whether Anthropic should be asked about an account now."""
    stored = stored or {}
    retry_at = stored.get("retry_at")
    if retry_at is not None and retry_at.timestamp() > now:
        return False
    read_at = stored.get("read_at")
    return read_at is None or now - read_at.timestamp() >= STALE_SECONDS


async def borrow_token(
    conn: asyncssh.SSHClientConnection, container: str, homes: list[str], now: float
) -> plan_usage.AccountToken | None:
    """A current token from one of these sessions' own credentials files.

    Read, never refreshed: the session refreshes its own, and doing it here
    would spend the refresh token it relies on.
    """
    harness = get_harness(HarnessKind.CLAUDE_CODE)
    for home in homes:
        path = harness.credentials_merge_target(SessionSpace(home=home))
        if path is None:
            return None
        token = plan_usage.token_from_credentials_file(
            await profile.read_file(conn, container, path)
        )
        if token is not None and token.valid(now):
            return token
    return None


async def collect(
    conn: asyncssh.SSHClientConnection,
    container: str,
    rows: list[dict[str, Any]],
    now: float,
    *,
    ask: bool = True,
) -> None:
    """Write down what every Claude Code session in one container last saw.

    `rows` are its sessions, as `account_for_session` takes them, with their
    `home_dir`. With `ask`, an account whose newest reading is stale is also
    asked of Anthropic with a token borrowed from one of its sessions here.
    """
    rows = [
        r for r in rows
        if str(r.get("harness") or HarnessKind.CLAUDE_CODE) == HarnessKind.CLAUDE_CODE
        and r.get("home_dir")
    ]
    if not rows:
        return
    readings = await read_statuslines(conn, container, [str(r["home_dir"]) for r in rows], now)

    homes: dict[UUID, list[str]] = {}
    async with service_session() as db:
        for row in rows:
            account = await account_for_session(db, row)
            # An API key has no plan limits to read.
            if account is None or account.get("auth_mode") != "oauth":
                continue
            homes.setdefault(account["id"], []).append(str(row["home_dir"]))
        stored = await queries.get_account_usage_privileged(db, list(homes))

    for account_id, account_homes in homes.items():
        found = [readings[h] for h in account_homes if h in readings]
        newest = max(found, key=lambda r: r[1], default=None)
        if newest is not None:
            await save_reading(account_id, newest[0], newest[1], "statusline")
        current = dict(stored.get(account_id) or {})
        if newest is not None and (
            current.get("read_at") is None or newest[1] > current["read_at"].timestamp()
        ):
            current["read_at"] = datetime.fromtimestamp(newest[1], tz=UTC)
        if not ask or not due(current, now):
            continue
        token = await borrow_token(conn, container, account_homes, now)
        if token is not None:
            await ask_anthropic(
                account_id, [("session", token)], now, int(current.get("refusals") or 0)
            )


async def refresh_from_stored(now: float) -> int:
    """Ask Anthropic about every account whose reading is stale, while the
    sign-in stored for it is still valid — which covers an account with no
    running session to borrow a token from. Returns how many were asked."""
    async with service_session() as conn:
        accounts = await queries.claude_accounts_for_usage_privileged(conn)
    asked = 0
    for account in accounts:
        if not due(account, now):
            continue
        token = plan_usage.token_from_credentials_file(account.get("oauth_blob"))
        if token is None or not token.valid(now):
            continue
        await ask_anthropic(
            account["id"], [("stored", token)], now, int(account.get("refusals") or 0)
        )
        asked += 1
    return asked


async def capture_before_removal(
    conn: asyncssh.SSHClientConnection,
    container: str,
    row: dict[str, Any],
    now: float,
) -> None:
    """Read a session's status line one last time, before its HOME goes."""
    try:
        await collect(conn, container, [row], now, ask=False)
    except Exception as exc:  # noqa: BLE001 — never in the way of deleting a session
        log.info(
            "usage: no last reading from %s: %s",
            shlex.quote(str(row.get("home_dir"))),
            exc,
        )
