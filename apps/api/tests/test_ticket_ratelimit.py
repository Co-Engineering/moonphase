"""Ticket minting is rate limited per caller, end to end through the router.

Unlike test_ratelimit.py (the limiter in isolation), this exercises the real
`issue_terminal_ticket` handler in routers/terminal.py — the endpoint checks
no project access at all (the socket does, against the identity the ticket
carries), which is exactly why this is the one thing bounding how fast a
single account can grow the shared in-memory ticket store.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from moonphase.auth import Principal
from moonphase.ratelimit import RateLimiter
from moonphase.routers import terminal

PROJECT_ID = uuid.uuid4()


def _principal(user_id: str) -> Principal:
    claims = {"sub": user_id, "email": f"{user_id}@example.test"}
    return Principal(user_id=user_id, email=claims["email"], claims=claims)


async def test_a_burst_of_ticket_requests_is_refused_past_the_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        terminal, "_TICKET_RATE_LIMITER", RateLimiter(max_calls=2, window_seconds=60)
    )
    principal = _principal("user-1")

    await terminal.issue_terminal_ticket(PROJECT_ID, principal=principal)
    await terminal.issue_terminal_ticket(PROJECT_ID, principal=principal)

    with pytest.raises(HTTPException) as caught:
        await terminal.issue_terminal_ticket(PROJECT_ID, principal=principal)

    assert caught.value.status_code == 429
    assert "Retry-After" in caught.value.headers


async def test_callers_do_not_share_a_ticket_budget(monkeypatch) -> None:
    """One account scripting a flood must not cost another account its own
    ability to open a terminal at all."""
    monkeypatch.setattr(
        terminal, "_TICKET_RATE_LIMITER", RateLimiter(max_calls=1, window_seconds=60)
    )

    await terminal.issue_terminal_ticket(PROJECT_ID, principal=_principal("flooder"))
    with pytest.raises(HTTPException):
        await terminal.issue_terminal_ticket(PROJECT_ID, principal=_principal("flooder"))

    # A different account's budget is untouched.
    out = await terminal.issue_terminal_ticket(PROJECT_ID, principal=_principal("someone-else"))
    assert "ticket" in out
