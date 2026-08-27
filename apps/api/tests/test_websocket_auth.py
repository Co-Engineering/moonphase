"""`websocket_principal`'s two ways in: a ticket, or the bearer token itself.

The ticket path is what closes the leak a raw token in the query string
caused (see tickets.py); the token path stays as a fallback, so both need to
keep working the same way they did before.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from moonphase import auth, tickets
from moonphase.auth import Principal
from moonphase.routers import terminal


class _FakeWebSocket:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


PROJECT_ID = uuid.uuid4()


async def test_a_valid_ticket_authenticates_the_connection() -> None:
    claims = {"sub": "user-1", "email": "person@example.test"}
    ticket = tickets.issue(claims, scope=auth.ticket_scope(PROJECT_ID))

    principal = await auth.websocket_principal(
        _FakeWebSocket(), project_id=PROJECT_ID, ticket=ticket
    )

    assert principal.user_id == "user-1"
    assert principal.email == "person@example.test"


async def test_a_ticket_cannot_be_replayed() -> None:
    ticket = tickets.issue({"sub": "user-1"}, scope=auth.ticket_scope(PROJECT_ID))
    await auth.websocket_principal(_FakeWebSocket(), project_id=PROJECT_ID, ticket=ticket)

    with pytest.raises(HTTPException) as caught:
        await auth.websocket_principal(_FakeWebSocket(), project_id=PROJECT_ID, ticket=ticket)

    assert caught.value.status_code == 401


async def test_a_ticket_minted_for_a_different_project_is_refused() -> None:
    ticket = tickets.issue({"sub": "user-1"}, scope=auth.ticket_scope(PROJECT_ID))
    other_project = uuid.uuid4()

    with pytest.raises(HTTPException) as caught:
        await auth.websocket_principal(_FakeWebSocket(), project_id=other_project, ticket=ticket)

    assert caught.value.status_code == 401


async def test_a_ticket_with_no_subject_claim_is_refused() -> None:
    ticket = tickets.issue({"email": "no-sub@example.test"}, scope=auth.ticket_scope(PROJECT_ID))

    with pytest.raises(HTTPException) as caught:
        await auth.websocket_principal(_FakeWebSocket(), project_id=PROJECT_ID, ticket=ticket)

    assert caught.value.status_code == 401


async def test_falls_back_to_a_bearer_header_when_no_ticket_is_given() -> None:
    """A client that has not switched to tickets yet still works.

    `token` and `ticket` are explicitly None here: calling the dependency
    function directly (rather than through FastAPI's injection) skips the
    resolution that would otherwise turn their `Query(default=None)` param
    markers into a real `None` — left unset, they are truthy sentinel
    objects, not absent.
    """
    websocket = _FakeWebSocket({"authorization": "Bearer not-a-real-token"})

    with pytest.raises(HTTPException) as caught:
        await auth.websocket_principal(websocket, project_id=PROJECT_ID, token=None, ticket=None)

    # Reaches real token decoding (and fails there) rather than the "missing
    # token" error — proving the header fallback path was taken.
    assert caught.value.detail != "Missing token."


async def test_no_ticket_and_no_token_is_refused() -> None:
    with pytest.raises(HTTPException) as caught:
        await auth.websocket_principal(
            _FakeWebSocket(), project_id=PROJECT_ID, token=None, ticket=None
        )

    assert caught.value.status_code == 401
    assert caught.value.detail == "Missing token."


# --- Origin --------------------------------------------------------------------


async def test_a_page_from_an_unlisted_origin_is_refused_even_with_a_good_ticket() -> None:
    """A browser is the only client that ever sends `Origin` on a WebSocket
    handshake — it is what enforces same-origin policy — so one present at
    all and not ours is the CSRF-shaped case this exists to catch, whatever
    else the request carries."""
    ticket = tickets.issue({"sub": "user-1"}, scope=auth.ticket_scope(PROJECT_ID))

    with pytest.raises(HTTPException) as caught:
        await auth.websocket_principal(
            _FakeWebSocket({"origin": "https://evil.example"}),
            project_id=PROJECT_ID,
            ticket=ticket,
        )

    assert caught.value.status_code == 403


async def test_a_page_from_a_configured_origin_is_allowed() -> None:
    from moonphase.config import get_settings

    allowed = get_settings().cors_origins[0]
    ticket = tickets.issue({"sub": "user-1"}, scope=auth.ticket_scope(PROJECT_ID))

    principal = await auth.websocket_principal(
        _FakeWebSocket({"origin": allowed}), project_id=PROJECT_ID, ticket=ticket
    )

    assert principal.user_id == "user-1"


async def test_a_non_browser_client_sends_no_origin_and_is_not_penalised_for_it() -> None:
    """The desktop app's preview relay is a plain `ws` client in Node, not a
    browser — it never sends `Origin` at all, and was never a same-origin
    participant to begin with. Requiring the header would just break it."""
    ticket = tickets.issue({"sub": "user-1"}, scope=auth.ticket_scope(PROJECT_ID))

    principal = await auth.websocket_principal(
        _FakeWebSocket(), project_id=PROJECT_ID, ticket=ticket
    )

    assert principal.user_id == "user-1"


# --- issuing a ticket, end to end ------------------------------------------


async def test_a_minted_ticket_redeems_to_the_minting_principal() -> None:
    """The full round trip a client actually does: mint over HTTP, then
    authenticate the socket with what came back — no DB involved, since
    minting a ticket does not itself check project access (the socket still
    does, against the identity the ticket carries)."""
    principal = Principal(user_id="user-1", email="person@example.test", claims={"sub": "user-1"})

    out = await terminal.issue_terminal_ticket(PROJECT_ID, principal=principal)

    redeemed = await auth.websocket_principal(
        _FakeWebSocket(), project_id=PROJECT_ID, ticket=out["ticket"]
    )
    assert redeemed.user_id == "user-1"


async def test_a_minted_ticket_does_not_redeem_for_another_project() -> None:
    principal = Principal(user_id="user-1", email=None, claims={"sub": "user-1"})
    out = await terminal.issue_terminal_ticket(PROJECT_ID, principal=principal)

    with pytest.raises(HTTPException) as caught:
        await auth.websocket_principal(
            _FakeWebSocket(), project_id=uuid.uuid4(), ticket=out["ticket"]
        )
    assert caught.value.status_code == 401
