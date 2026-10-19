"""How full each Claude subscription's limits are, as the provider reports it.

The same numbers `/usage` shows inside Claude Code: the five-hour session
window, the weekly windows, and when each resets. Moonphase cannot derive
these — the allowance is not published, and its own token counts only cover
sessions it ran — so they are read from the endpoint Claude Code itself asks,
with the account's own sign-in.

The token is the delicate part. A running Claude refreshes its access token
on its own and writes the new one to `~/.claude/.credentials.json`; the copy
stored at sign-in goes stale within hours. Refreshing it here would spend the
refresh token the running sessions depend on and could sign them out, so this
never refreshes anything. It uses the stored token while that is valid, and
otherwise borrows the current one from a running session on that account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Required by the provider for OAuth-authenticated calls.
OAUTH_BETA = "oauth-2025-04-20"
# The numbers move slowly, and the endpoint is rate-limited per account — a
# limit the account's own running Claude sessions draw on too.
CACHE_SECONDS = 120.0
# After a 429 with no Retry-After, wait this long, doubling on each refusal in
# a row up to the cap. Asking again a minute later just keeps the account at
# its limit.
BACKOFF_SECONDS = 300.0
BACKOFF_CAP_SECONDS = 1800.0

# Known windows, in display order. Anything else the provider adds is shown
# after these under its own key rather than dropped.
WINDOW_LABELS = {
    "five_hour": "Current session",
    "seven_day": "Current week (all models)",
    "seven_day_opus": "Current week (Opus)",
    "seven_day_sonnet": "Current week (Sonnet)",
}


@dataclass
class Window:
    key: str
    label: str
    percent: float
    resets_at: datetime | None


@dataclass
class AccountToken:
    token: str
    # Milliseconds since the epoch, as Claude Code stores it. None when unknown.
    expires_at_ms: int | None

    def valid(self, now: float, margin_seconds: float = 60.0) -> bool:
        if self.expires_at_ms is None:
            return True
        return self.expires_at_ms / 1000 > now + margin_seconds


@dataclass
class Reading:
    windows: list[Window] = field(default_factory=list)
    fetched_at: float = 0.0


class UsageUnavailable(Exception):
    """The provider would not say. `reason` is a short machine-readable kind."""

    def __init__(self, reason: str, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        # Seconds the provider asked us to wait, when it said.
        self.retry_after = retry_after


def retry_after_seconds(value: str | None, now: float) -> float | None:
    """`Retry-After` as seconds from now — it may be a number or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - now)


def token_from_credentials_file(contents: str | None) -> AccountToken | None:
    """The access token in a `.credentials.json`, or None if there is none."""
    if not contents:
        return None
    try:
        doc = json.loads(contents)
    except json.JSONDecodeError:
        return None
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    expires = oauth.get("expiresAt")
    return AccountToken(
        token=token, expires_at_ms=int(expires) if isinstance(expires, int | float) else None
    )


def freshest(tokens: list[AccountToken | None], now: float) -> AccountToken | None:
    """The valid token that lasts longest. None when every one has expired."""
    valid = [t for t in tokens if t is not None and t.valid(now)]
    if not valid:
        return None
    return max(valid, key=lambda t: t.expires_at_ms or 0)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_windows(doc: Any) -> list[Window]:
    """The provider's answer, as windows. Unknown or empty entries are skipped."""
    if not isinstance(doc, dict):
        return []
    keys = [k for k in WINDOW_LABELS if k in doc] + sorted(
        k for k in doc if k not in WINDOW_LABELS
    )
    windows: list[Window] = []
    for key in keys:
        entry = doc.get(key)
        if not isinstance(entry, dict):
            continue
        percent = entry.get("utilization")
        if not isinstance(percent, int | float):
            continue
        windows.append(
            Window(
                key=key,
                label=WINDOW_LABELS.get(key, key.replace("_", " ").capitalize()),
                percent=max(0.0, min(100.0, float(percent))),
                resets_at=_parse_time(entry.get("resets_at")),
            )
        )
    return windows


def _anthropic_message(response: httpx.Response) -> str:
    """Anthropic's own words for a refusal, short enough to show."""
    try:
        doc = response.json()
    except ValueError:
        return response.text.strip()[:200]
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:200]
    return ""


def _said(message: str) -> str:
    return f" Anthropic said: \u201c{message}\u201d" if message else ""


async def fetch(token: str, client: httpx.AsyncClient | None = None) -> list[Window]:
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=10) as own:
                response = await own.get(USAGE_URL, headers=headers)
        else:
            response = await client.get(USAGE_URL, headers=headers)
    except httpx.HTTPError as exc:
        raise UsageUnavailable("unreachable", f"Could not reach Anthropic: {exc}") from exc

    if response.status_code < 400:
        try:
            return parse_windows(response.json())
        except ValueError as exc:
            raise UsageUnavailable("error", "Anthropic's answer was not readable.") from exc

    message = _anthropic_message(response)
    log.info("usage endpoint answered %s: %s", response.status_code, message)
    if response.status_code == 401:
        raise UsageUnavailable("expired", "This sign-in has expired." + _said(message))
    if response.status_code == 403:
        raise UsageUnavailable(
            "unsupported", "This sign-in is not allowed to read plan usage." + _said(message)
        )
    if response.status_code == 429:
        raise UsageUnavailable(
            "rate_limited",
            # Seen both when the account has used up a window and when usage is
            # read too often; the status alone does not say which.
            "Anthropic refused to report usage right now — usually because this "
            "account has hit a limit." + _said(message),
            retry_after=retry_after_seconds(response.headers.get("retry-after"), time.time()),
        )
    raise UsageUnavailable(
        "error", f"Anthropic answered {response.status_code}." + _said(message)
    )


# account id -> last good reading. Per process, which is all it needs to be:
# it only exists so that a dashboard left open does not ask once a minute per
# viewer.
_cache: dict[str, Reading] = {}


def cached(account_id: str, now: float) -> Reading | None:
    reading = _cache.get(account_id)
    if reading is None or now - reading.fetched_at > CACHE_SECONDS:
        return None
    return reading


def last_known(account_id: str) -> Reading | None:
    return _cache.get(account_id)


def remember(account_id: str, windows: list[Window]) -> Reading:
    reading = Reading(windows=windows, fetched_at=time.time())
    _cache[account_id] = reading
    _backoff.pop(account_id, None)
    return reading


# account id -> (do not ask before this time, refusals in a row, what was said).
_backoff: dict[str, tuple[float, int, str | None]] = {}


def backing_off(account_id: str, now: float) -> tuple[float, str | None] | None:
    """When it is worth asking again, and what the refusal said — or None."""
    entry = _backoff.get(account_id)
    if entry is None or entry[0] <= now:
        return None
    return entry[0], entry[2]


def refused(
    account_id: str, now: float, retry_after: float | None, detail: str | None = None
) -> float:
    """Record a 429. Returns when it is worth asking again."""
    streak = _backoff.get(account_id, (0.0, 0, None))[1] + 1
    wait = (
        retry_after
        if retry_after is not None
        else min(BACKOFF_CAP_SECONDS, BACKOFF_SECONDS * 2 ** (streak - 1))
    )
    until = now + wait
    _backoff[account_id] = (until, streak, detail)
    return until


# One request per account at a time, so two people opening the dashboard
# together — or the page and its own poll — do not both spend the allowance.
_locks: dict[str, asyncio.Lock] = {}


def lock_for(account_id: str) -> asyncio.Lock:
    return _locks.setdefault(account_id, asyncio.Lock())
