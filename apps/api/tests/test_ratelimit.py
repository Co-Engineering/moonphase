"""The sliding-window limiter, in isolation from anything it protects."""

from __future__ import annotations

import time

from moonphase.ratelimit import RateLimiter


def test_calls_within_the_limit_are_all_allowed() -> None:
    limiter = RateLimiter(max_calls=3, window_seconds=60)
    assert limiter.check("alice") is None
    assert limiter.check("alice") is None
    assert limiter.check("alice") is None


def test_the_call_over_the_limit_is_refused_with_a_wait_time() -> None:
    limiter = RateLimiter(max_calls=2, window_seconds=60)
    limiter.check("alice")
    limiter.check("alice")
    retry_after = limiter.check("alice")
    assert retry_after is not None
    assert 0 < retry_after <= 60


def test_keys_do_not_share_a_budget() -> None:
    """One admin batch-enumerating should not cost an unrelated caller theirs."""
    limiter = RateLimiter(max_calls=1, window_seconds=60)
    assert limiter.check("alice") is None
    assert limiter.check("bob") is None


def test_the_window_slides_rather_than_resetting_all_at_once() -> None:
    limiter = RateLimiter(max_calls=1, window_seconds=0.05)
    assert limiter.check("alice") is None
    assert limiter.check("alice") is not None
    time.sleep(0.06)
    assert limiter.check("alice") is None
