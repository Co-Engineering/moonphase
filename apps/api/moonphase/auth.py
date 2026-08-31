"""GoTrue JWT verification.

Supabase signs access tokens one of two ways, and a self-hostable product has
to cope with both:

  * **Asymmetric (ES256/RS256), the current default.** The public keys are
    published at `/auth/v1/.well-known/jwks.json` and selected per token by
    `kid`. Nothing secret is needed to verify, which is the point.
  * **HS256 with a shared secret**, used by older projects and by anyone who
    pinned `SUPABASE_JWT_SECRET`.

The algorithm is taken from the token header but never trusted blindly: an
`HS*` token is only accepted against the configured secret, and an asymmetric
one only against a key actually published in the JWKS. That closes the
algorithm-confusion attack where a token claiming `HS256` is verified using a
public key as the HMAC secret.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import tickets
from .config import get_settings

_bearer = HTTPBearer(auto_error=False)

_ASYMMETRIC_ALGS = {"ES256", "ES384", "ES512", "RS256", "RS384", "RS512"}
_SYMMETRIC_ALGS = {"HS256", "HS384", "HS512"}

# Keys rotate rarely; a short TTL plus refetch-on-unknown-kid means a rotation
# is picked up on the first token signed by the new key, not minutes later.
_JWKS_TTL_SECONDS = 600


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    claims: dict[str, Any]


class _JwksCache:
    def __init__(self) -> None:
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float = 0.0

    @property
    def _url(self) -> str:
        return f"{get_settings().supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(self._url)
            response.raise_for_status()
            payload = response.json()

        keys: dict[str, jwt.PyJWK] = {}
        for entry in payload.get("keys", []):
            try:
                key = jwt.PyJWK(entry)
            except Exception:  # noqa: BLE001 — one bad key must not kill the set
                continue
            if key.key_id:
                keys[key.key_id] = key

        self._keys = keys
        self._fetched_at = time.monotonic()

    async def get(self, kid: str) -> jwt.PyJWK | None:
        stale = time.monotonic() - self._fetched_at > _JWKS_TTL_SECONDS
        if kid not in self._keys or stale:
            try:
                await self._refresh()
            except Exception as exc:  # noqa: BLE001
                if kid in self._keys:
                    # Serve the cached key rather than locking everyone out
                    # because the auth service blipped.
                    return self._keys[kid]
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Could not fetch signing keys from Supabase: {exc}",
                ) from exc
        return self._keys.get(kid)


_jwks = _JwksCache()


async def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()

    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token."
        ) from exc

    alg = header.get("alg", "")
    options = {"require": ["exp", "sub"]}

    if alg in _SYMMETRIC_ALGS:
        if not settings.supabase_jwt_secret:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Token is HS-signed but no SUPABASE_JWT_SECRET is configured. "
                    "Set it, or use a project that publishes a JWKS."
                ),
            )
        key: Any = settings.supabase_jwt_secret
        algorithms = [alg]

    elif alg in _ASYMMETRIC_ALGS:
        kid = header.get("kid")
        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has no key id.",
            )
        jwk = await _jwks.get(kid)
        if jwk is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token was signed with an unknown key.",
            )
        key = jwk.key
        # Pin to the JWKS entry's own algorithm, not the token's claim.
        algorithms = [jwk.algorithm_name or alg]

    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unsupported token algorithm {alg!r}.",
        )

    try:
        return jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience="authenticated",
            options=options,
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired."
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token."
        ) from exc


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has no subject."
        )
    return Principal(user_id=str(sub), email=claims.get("email"), claims=claims)


async def _principal_from_token(token: str) -> Principal:
    claims = await decode_token(token)
    return _principal_from_claims(claims)


async def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    del request
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _principal_from_token(credentials.credentials)


def ticket_scope(project_id: UUID | None) -> str:
    """The scope a project's sockets mint and redeem tickets under.

    One scope per project rather than per socket kind (terminal, feed,
    preview): all three take the same `{project_id}` path parameter, and a
    ticket is only ever proof of identity — the socket still runs its own
    access check against that identity afterward, same as it always did with
    a bearer token. Narrowing further would not remove a check, only add
    ticket-minting endpoints nothing yet asks for.
    """
    return f"ws:{project_id}"


def _origin_allowed(websocket: WebSocket) -> bool:
    """Whether the page that opened this WebSocket is one of ours.

    Only a browser ever sends `Origin` on a WebSocket handshake — it is the
    thing enforcing same-origin policy in the first place — so its absence
    means a non-browser client, which was never a participant in that model
    and has no origin to compare. A browser that does send one, naming a page
    this deployment does not serve, is the CSRF-shaped case this exists to
    catch.

    Note what this is layered on top of: a socket is authenticated by a
    ticket minted over a request carrying a bearer token in a header, or by
    that token itself. Neither is ambient the way a cookie is — the token
    lives in the frontend's own origin-scoped storage — so a cross-site page
    has nothing to send. This check is a second lock on that door, which is
    why the cases below can be answered generously without opening one.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    if origin in get_settings().cors_origins:
        return True

    # Served by this very deployment. Everything behind the bundled proxy is
    # one origin — that is the point of it, and why the phone client needs no
    # CORS at all (docker/Caddyfile) — so the frontend's origin is whatever
    # address the browser used to reach us. No configured list can know that
    # in advance: it is the domain the operator chose, and until this check
    # existed nothing ever had to be told it. Comparing against `Host` asks
    # the question directly instead. A cross-site page cannot forge its way
    # through: the browser sets both headers, `Origin` to the attacking page
    # and `Host` to whoever is being attacked, and it is precisely when they
    # differ that this is worth refusing.
    host = websocket.headers.get("host")
    if host and urlsplit(origin).netloc == host:
        return True

    # The desktop app loads the same frontend off a file:// page, and an
    # opaque origin serialises to the string "null". Verified against
    # Chromium rather than assumed, since getting it wrong locks that client
    # out exactly as thoroughly as a wrong hostname locks out the web one.
    return origin == "null"


async def websocket_principal(
    websocket: WebSocket,
    project_id: UUID | None = None,
    ticket: str | None = Query(default=None),
) -> Principal:
    """Authenticate a WebSocket.

    Browsers cannot set headers on a WebSocket handshake, so proof of
    identity has to arrive as a query parameter — which is exactly why it
    must be a ticket and never a real access token: a query string lands in
    every reverse proxy's and uvicorn's own access log for as long as it
    stays in the URL, and a ticket is short-lived, single-use, minted over
    an authenticated HTTP request specifically so a logged copy is worthless
    the moment it's read back. This used to also accept a raw bearer token,
    via `?token=` or an `Authorization` header, as a fallback for a client
    that had not yet switched over — both shipped clients (web, desktop)
    have, so that fallback is gone rather than left live as a way to undo
    the whole point of tickets.
    """
    if not _origin_allowed(websocket):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Origin not allowed."
        )

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing ticket."
        )
    claims = tickets.redeem(ticket, scope=ticket_scope(project_id))
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This ticket is invalid, expired, or already used.",
        )
    return _principal_from_claims(claims)
