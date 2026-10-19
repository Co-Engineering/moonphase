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

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Required by the provider for OAuth-authenticated calls.
OAUTH_BETA = "oauth-2025-04-20"
CACHE_SECONDS = 60.0

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

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


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

    if response.status_code == 401:
        raise UsageUnavailable(
            "expired",
            "The sign-in has expired. Open a session on this account, or sign it in again.",
        )
    if response.status_code == 403:
        raise UsageUnavailable(
            "unsupported", "This kind of sign-in cannot report plan usage."
        )
    if response.status_code == 429:
        raise UsageUnavailable("rate_limited", "Anthropic asked us to slow down.")
    if response.status_code >= 400:
        raise UsageUnavailable(
            "error", f"Anthropic answered {response.status_code}."
        )
    try:
        return parse_windows(response.json())
    except ValueError as exc:
        raise UsageUnavailable("error", "Anthropic's answer was not readable.") from exc


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
    return reading
