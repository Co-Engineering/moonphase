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
    means a non-browser client, like the desktop app's preview relay, which
    was never a participant in that model and has no origin to compare. A
    browser that does send one, sending something other than a page this
    deployment actually serves, is the CSRF-shaped case this exists to catch.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    return origin in get_settings().cors_origins


async def websocket_principal(
    websocket: WebSocket,
    project_id: UUID | None = None,
    token: str | None = Query(default=None),
    ticket: str | None = Query(default=None),
) -> Principal:
    """Authenticate a WebSocket.

    Browsers cannot set headers on a WebSocket handshake, so proof of
    identity has to arrive as a query parameter either way. A raw bearer
    token there lands in every reverse proxy's access log for as long as it
    is valid, so `ticket` — short-lived, single-use, minted over an
    authenticated HTTP request — is preferred. `token` remains as a fallback
    for a client that has not switched over.
    """
    if not _origin_allowed(websocket):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Origin not allowed."
        )

    if ticket:
        claims = tickets.redeem(ticket, scope=ticket_scope(project_id))
        if claims is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This ticket is invalid, expired, or already used.",
            )
        return _principal_from_claims(claims)

    if not token:
        header = websocket.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token."
        )
    return await _principal_from_token(token)
