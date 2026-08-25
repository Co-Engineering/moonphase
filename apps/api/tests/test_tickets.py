"""The WebSocket ticket store, in isolation.

A ticket exists to keep a real bearer token out of proxy access logs (see
tickets.py), so the properties that matter are exactly the ones that make a
leaked ticket worthless: single-use, short-lived, and scoped to what it was
minted for.
"""

from __future__ import annotations

import time

from moonphase import tickets


def test_a_fresh_ticket_redeems_to_the_claims_it_carries() -> None:
    claims = {"sub": "user-1", "email": "person@example.test"}
    ticket = tickets.issue(claims, scope="ws:project-1")

    assert tickets.redeem(ticket, scope="ws:project-1") == claims


def test_a_ticket_is_single_use() -> None:
    claims = {"sub": "user-1"}
    ticket = tickets.issue(claims, scope="ws:project-1")

    assert tickets.redeem(ticket, scope="ws:project-1") == claims
    assert tickets.redeem(ticket, scope="ws:project-1") is None


def test_a_ticket_only_redeems_for_the_scope_it_was_minted_for() -> None:
    """A ticket minted for one project's socket must not authenticate a
    connection to a different one, even with the right ticket value."""
    ticket = tickets.issue({"sub": "user-1"}, scope="ws:project-1")

    assert tickets.redeem(ticket, scope="ws:project-2") is None


def test_redeeming_against_the_wrong_scope_still_consumes_the_ticket() -> None:
    """Otherwise a ticket could be probed against every scope until one hit —
    a wrong guess must burn it exactly like a right one would."""
    ticket = tickets.issue({"sub": "user-1"}, scope="ws:project-1")

    tickets.redeem(ticket, scope="ws:wrong")

    assert tickets.redeem(ticket, scope="ws:project-1") is None


def test_an_unknown_ticket_is_refused() -> None:
    assert tickets.redeem("not-a-real-ticket", scope="ws:project-1") is None


def test_an_expired_ticket_is_refused(monkeypatch) -> None:
    ticket = tickets.issue({"sub": "user-1"}, scope="ws:project-1")

    real_monotonic = time.monotonic
    monkeypatch.setattr(
        time, "monotonic", lambda: real_monotonic() + tickets.TICKET_TTL_SECONDS + 1
    )

    assert tickets.redeem(ticket, scope="ws:project-1") is None
