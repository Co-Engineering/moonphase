"""Which pages may open a socket, and — more to the point — which must.

The Origin check shipped comparing against `MOONPHASE_CORS_ORIGINS` alone.
Nothing sets that in a normal install: the bundled proxy serves the frontend
and the API from one origin, so CORS never applies and the variable keeps
whatever default compose gives it (`http://localhost:8471`). A browser on the
real domain therefore looked exactly like an attacker, and every socket —
terminal, feed, preview — was refused with a 403 at the handshake, which
reaches the user as a terminal that will not connect and no message at all.

Every test here sends headers a browser really sends. The suite that shipped
with the check built its websockets with none, so the branch was never run.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from moonphase import auth, tickets
from moonphase.config import get_settings


class _FakeWebSocket:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


PROJECT_ID = uuid.uuid4()


def _browser(origin: str, host: str = "moonphase.example") -> _FakeWebSocket:
    return _FakeWebSocket({"origin": origin, "host": host})


async def _connect(websocket: _FakeWebSocket) -> auth.Principal:
    ticket = tickets.issue({"sub": "user-1"}, scope=auth.ticket_scope(PROJECT_ID))
    return await auth.websocket_principal(websocket, project_id=PROJECT_ID, ticket=ticket)


async def test_the_page_this_deployment_serves_may_connect() -> None:
    """The regression: a reverse-proxied install on its own domain."""
    principal = await _connect(_browser("https://moonphase.example"))
    assert principal.user_id == "user-1"


async def test_a_deployment_reached_by_host_and_port_may_connect() -> None:
    principal = await _connect(_browser("http://192.168.1.10:8471", host="192.168.1.10:8471"))
    assert principal.user_id == "user-1"


async def test_the_desktop_app_may_connect() -> None:
    """It loads the frontend from a file:// page, whose origin is "null"."""
    principal = await _connect(_browser("null"))
    assert principal.user_id == "user-1"


async def test_a_configured_origin_may_connect() -> None:
    """A genuinely cross-origin frontend, like the dev server.

    Read out of the settings rather than written here: the allow-list is
    still honoured, and what it happens to hold varies by deployment.
    """
    configured = get_settings().cors_origins[0]
    principal = await _connect(_browser(configured, host="somewhere.else"))
    assert principal.user_id == "user-1"


async def test_a_client_that_sends_no_origin_may_connect() -> None:
    principal = await _connect(_FakeWebSocket({"host": "moonphase.example"}))
    assert principal.user_id == "user-1"


async def test_another_site_may_not_connect() -> None:
    with pytest.raises(HTTPException) as caught:
        await _connect(_browser("https://evil.example"))

    assert caught.value.status_code == 403


async def test_a_lookalike_hostname_may_not_connect() -> None:
    """Host comparison is exact, not a suffix or prefix match."""
    with pytest.raises(HTTPException) as caught:
        await _connect(_browser("https://moonphase.example.evil.test"))

    assert caught.value.status_code == 403


async def test_the_origin_check_runs_before_the_ticket_is_spent() -> None:
    """A refused origin must not burn the ticket.

    Tickets are single-use, so redeeming one inside a request that was going
    to be rejected anyway would turn a stray cross-origin probe into a way of
    breaking the real client's next connection.
    """
    ticket = tickets.issue({"sub": "user-1"}, scope=auth.ticket_scope(PROJECT_ID))

    with pytest.raises(HTTPException):
        await auth.websocket_principal(
            _browser("https://evil.example"), project_id=PROJECT_ID, ticket=ticket
        )

    principal = await auth.websocket_principal(
        _browser("https://moonphase.example"), project_id=PROJECT_ID, ticket=ticket
    )
    assert principal.user_id == "user-1"
