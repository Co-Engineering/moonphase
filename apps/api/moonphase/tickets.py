"""Short-lived, single-use tickets for authenticating a WebSocket handshake.

Browsers cannot set headers when opening a WebSocket, so proof of identity has
to travel in the URL somehow. A raw bearer token there lands in every reverse
proxy's and load balancer's access log for as long as it stays valid — a
self-hosted instance behind nginx/Caddy/Traefik with default logging leaks a
live GoTrue token on every socket connection.

A ticket fixes that: it is minted over an authenticated HTTP request (a
bearer token in the `Authorization` header, where it belongs), and is
worthless the moment it appears in a log, since it is single-use and expires
in seconds. What lands in proxy logs is the ticket, not the credential it was
minted from.

A plain in-memory store is enough for the same reason login.py's session dict
is: the API runs as a single uvicorn process with no `--workers` flag
(docker/Dockerfile), so there is one store, not one per worker that a ticket
minted on one process could fail to redeem against another.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

# Long enough to cover minting the ticket, returning it, and opening the
# socket — a client does all three in one motion — and nothing more.
TICKET_TTL_SECONDS = 15.0

# Defense in depth beyond the per-caller rate limit on the minting endpoint
# (routers/terminal.py): a bound on the store itself, so many distinct
# accounts each staying under their own limit still cannot grow this past a
# fixed size.
MAX_TICKETS = 10_000


@dataclass
class _Ticket:
    claims: dict[str, Any]
    scope: str
    expires_at: float


_tickets: dict[str, _Ticket] = {}


def issue(claims: dict[str, Any], *, scope: str) -> str:
    """Mint a ticket carrying `claims`, redeemable only for `scope`."""
    _prune()
    if len(_tickets) >= MAX_TICKETS:
        # Oldest first in insertion order, and every ticket shares the same
        # TTL, so the oldest is also the one soonest to expire anyway.
        _tickets.pop(next(iter(_tickets)))
    ticket = secrets.token_urlsafe(32)
    _tickets[ticket] = _Ticket(
        claims=claims, scope=scope, expires_at=time.monotonic() + TICKET_TTL_SECONDS
    )
    return ticket


def redeem(ticket: str, *, scope: str) -> dict[str, Any] | None:
    """Consume a ticket and return the claims it carries, or None.

    Single-use either way: a ticket is gone after this call whether it was
    valid or not, so a leaked or guessed value cannot be replayed by trying
    again. `scope` must match what it was issued for — a ticket minted for
    one project's socket cannot be redeemed against another's.
    """
    found = _tickets.pop(ticket, None)
    if found is None:
        return None
    if found.expires_at < time.monotonic():
        return None
    if found.scope != scope:
        return None
    return found.claims


def _prune() -> None:
    now = time.monotonic()
    for key in [k for k, t in _tickets.items() if t.expires_at < now]:
        _tickets.pop(key, None)
