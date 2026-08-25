"""In-process rate limiting for endpoints where the *call* is the sensitive
part, not what it returns.

Share creation resolves an email address to an account and tells the caller
which it was — a deliberate, bounded trade-off for a single invite (see
docs/concepts/security.md) that becomes an account-enumeration primitive once
it can be called at whatever rate the API allows. This bounds that rate.

A plain in-memory sliding window is enough for the same reason login.py's
session dict is enough: the API runs as a single uvicorn process with no
`--workers` flag (docker/Dockerfile), so there is one counter, not one per
worker that would each let the full rate through.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding window: at most `max_calls` per key in any `window_seconds`."""

    def __init__(self, *, max_calls: int, window_seconds: float) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> float | None:
        """Record a call for `key` and allow it, or refuse it.

        Returns None if the call is allowed (and counted against the window).
        Otherwise returns the number of seconds until the oldest call in the
        window ages out and a new one would be allowed.
        """
        now = time.monotonic()
        calls = self._calls[key]
        while calls and now - calls[0] > self.window_seconds:
            calls.popleft()
        if len(calls) >= self.max_calls:
            return self.window_seconds - (now - calls[0])
        calls.append(now)
        return None
