"""JWT verification — the single most security-critical gate in the API.

Covers the algorithm-confusion defense (an HS256 token is only ever checked
against the configured shared secret, never against a JWKS public key, and
vice versa), JWKS refresh-on-unknown-`kid`, the stale-cache fallback when the
auth service is unreachable, and the ordinary rejection cases (expired,
malformed, missing claims, unsupported algorithm).

No network calls: `httpx.AsyncClient` is redirected to an in-memory transport
that serves a fixed JWKS document (or fails, to exercise the fallback), and
JWTs are signed with keys generated locally for each test.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from jwt.algorithms import ECAlgorithm

from moonphase import auth
from moonphase.config import get_settings


def _keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, private_key.public_key()


def _jwk_for(public_key, kid: str) -> dict:
    jwk = ECAlgorithm(ECAlgorithm.SHA256).to_jwk(public_key, as_dict=True)
    jwk.update(kid=kid, alg="ES256", use="sig")
    return jwk


def _token(private_key, *, kid: str | None = "kid-1", alg: str = "ES256", **claims) -> str:
    payload = {
        "sub": "user-1",
        "email": "person@example.test",
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        **claims,
    }
    headers = {"kid": kid} if kid else {}
    return jwt.encode(payload, private_key, algorithm=alg, headers=headers)


class _FakeJwksTransport(httpx.AsyncBaseTransport):
    """Serves a fixed JWKS document instead of hitting the network."""

    def __init__(self, jwks: dict | None = None, *, fail: bool = False) -> None:
        self._jwks = jwks or {"keys": []}
        self._fail = fail
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self._fail:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=self._jwks)


def _serve(monkeypatch, jwks: dict | None = None, *, fail: bool = False) -> _FakeJwksTransport:
    transport = _FakeJwksTransport(jwks, fail=fail)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return transport


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _forge_hs256(secret: bytes, payload: dict, *, kid: str | None = None) -> str:
    """Hand-build an HS256 JWT with an arbitrary byte string as the HMAC
    secret. PyJWT's own `encode` now refuses a PEM-shaped key here, which
    would make the alg-confusion attack impossible to even construct — this
    bypasses that guard so `decode_token` is the thing actually tested."""
    header = {"alg": "HS256", "typ": "JWT"}
    if kid:
        header["kid"] = kid
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + b"."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + _b64url(signature)).decode()


def _set(monkeypatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Settings and the module-level JWKS cache are both process-wide state,
    and every test here changes one or the other."""
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "_jwks", auth._JwksCache())
    monkeypatch.setenv("SUPABASE_URL", "http://auth.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- asymmetric (JWKS) ---------------------------------------------------


async def test_a_valid_es256_token_verifies_against_the_jwks(monkeypatch) -> None:
    private_key, public_key = _keypair()
    _serve(monkeypatch, {"keys": [_jwk_for(public_key, "kid-1")]})

    claims = await auth.decode_token(_token(private_key, kid="kid-1"))

    assert claims["sub"] == "user-1"
    assert claims["email"] == "person@example.test"


async def test_an_unknown_kid_triggers_a_jwks_refresh(monkeypatch) -> None:
    private_key, public_key = _keypair()
    transport = _serve(monkeypatch, {"keys": [_jwk_for(public_key, "kid-2")]})
    # Seed the cache with an unrelated key and a fresh fetch time, so a hit on
    # kid-2 can only succeed through a refresh, never by luck.
    auth._jwks._keys = {"kid-old": object()}
    auth._jwks._fetched_at = time.monotonic()

    claims = await auth.decode_token(_token(private_key, kid="kid-2"))

    assert claims["sub"] == "user-1"
    assert transport.calls == 1


async def test_a_stale_cache_is_refreshed_once_reachable_again(monkeypatch) -> None:
    old_private, old_public = _keypair()
    auth._jwks._keys = {"kid-1": jwt.PyJWK(_jwk_for(old_public, "kid-1"))}
    auth._jwks._fetched_at = time.monotonic() - auth._JWKS_TTL_SECONDS - 1

    _, new_public = _keypair()
    transport = _serve(monkeypatch, {"keys": [_jwk_for(new_public, "kid-2")]})

    key = await auth._jwks.get("kid-2")

    assert key is not None
    assert transport.calls == 1
    assert "kid-1" not in auth._jwks._keys  # replaced, not merged


async def test_an_unreachable_jwks_falls_back_to_the_cached_key(monkeypatch) -> None:
    private_key, public_key = _keypair()
    auth._jwks._keys = {"kid-1": jwt.PyJWK(_jwk_for(public_key, "kid-1"))}
    # Stale, so a lookup would normally refresh -- but the endpoint is down.
    auth._jwks._fetched_at = time.monotonic() - auth._JWKS_TTL_SECONDS - 1
    _serve(monkeypatch, fail=True)

    claims = await auth.decode_token(_token(private_key, kid="kid-1"))

    assert claims["sub"] == "user-1"


async def test_an_unreachable_jwks_with_no_cached_key_is_a_503(monkeypatch) -> None:
    _serve(monkeypatch, fail=True)
    private_key, _ = _keypair()

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(_token(private_key, kid="missing-kid"))

    assert caught.value.status_code == 503


async def test_a_token_with_no_kid_is_rejected(monkeypatch) -> None:
    private_key, _ = _keypair()

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(_token(private_key, kid=None))

    assert caught.value.status_code == 401
    assert "key id" in caught.value.detail.lower()


async def test_a_token_for_a_key_not_in_the_jwks_is_rejected(monkeypatch) -> None:
    private_key, _ = _keypair()
    _serve(monkeypatch, {"keys": []})

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(_token(private_key, kid="nowhere"))

    assert caught.value.status_code == 401


# --- symmetric (shared secret) --------------------------------------------


async def test_an_hs256_token_verifies_against_the_configured_secret(monkeypatch) -> None:
    _set(monkeypatch, SUPABASE_JWT_SECRET="a-shared-secret-that-is-long-enough-1234")
    token = jwt.encode(
        {"sub": "user-1", "aud": "authenticated", "exp": int(time.time()) + 3600},
        "a-shared-secret-that-is-long-enough-1234",
        algorithm="HS256",
    )

    claims = await auth.decode_token(token)

    assert claims["sub"] == "user-1"


async def test_an_hs256_token_is_rejected_without_a_configured_secret(monkeypatch) -> None:
    """Without SUPABASE_JWT_SECRET, an HS256 token must be refused outright —
    never verified against anything else, in particular never against a
    JWKS public key."""
    _set(monkeypatch, SUPABASE_JWT_SECRET="")
    token = jwt.encode(
        {"sub": "attacker", "aud": "authenticated", "exp": int(time.time()) + 3600},
        "anything-at-all-the-secret-does-not-matter-here",
        algorithm="HS256",
    )

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(token)

    assert caught.value.status_code == 401
    assert "SUPABASE_JWT_SECRET" in caught.value.detail


async def test_an_hs256_token_signed_with_the_wrong_secret_is_rejected(monkeypatch) -> None:
    _set(monkeypatch, SUPABASE_JWT_SECRET="correct-secret-that-is-long-enough-5678")
    token = jwt.encode(
        {"sub": "user-1", "aud": "authenticated", "exp": int(time.time()) + 3600},
        "wrong-secret-that-is-also-long-enough-9012",
        algorithm="HS256",
    )

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(token)

    assert caught.value.status_code == 401


async def test_alg_confusion_a_public_key_forged_hs256_token_is_rejected(monkeypatch) -> None:
    """The classic JWT alg-confusion attack: sign with alg=HS256 using an
    ES256 public key's PEM bytes as the HMAC secret, hoping a verifier that
    reuses one "key" variable for both paths accepts it. This service never
    does that — the HS branch only ever uses SUPABASE_JWT_SECRET, and never
    sees the JWKS at all."""
    _set(monkeypatch, SUPABASE_JWT_SECRET="")
    _, public_key = _keypair()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    forged = _forge_hs256(
        public_pem, {"sub": "attacker", "aud": "authenticated", "exp": int(time.time()) + 3600}
    )

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(forged)

    assert caught.value.status_code == 401


# --- ordinary rejections ---------------------------------------------------


async def test_a_malformed_token_is_rejected(monkeypatch) -> None:
    with pytest.raises(HTTPException) as caught:
        await auth.decode_token("not-a-jwt")

    assert caught.value.status_code == 401


async def test_an_expired_token_is_rejected(monkeypatch) -> None:
    private_key, public_key = _keypair()
    _serve(monkeypatch, {"keys": [_jwk_for(public_key, "kid-1")]})
    token = _token(private_key, kid="kid-1", exp=int(time.time()) - 10)

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(token)

    assert caught.value.status_code == 401
    assert "expired" in caught.value.detail.lower()


async def test_a_not_yet_valid_token_is_rejected(monkeypatch) -> None:
    private_key, public_key = _keypair()
    _serve(monkeypatch, {"keys": [_jwk_for(public_key, "kid-1")]})
    token = _token(private_key, kid="kid-1", nbf=int(time.time()) + 3600)

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(token)

    assert caught.value.status_code == 401


async def test_a_token_missing_the_subject_claim_is_rejected(monkeypatch) -> None:
    private_key, public_key = _keypair()
    _serve(monkeypatch, {"keys": [_jwk_for(public_key, "kid-1")]})
    token = jwt.encode(
        {"aud": "authenticated", "exp": int(time.time()) + 3600},
        private_key,
        algorithm="ES256",
        headers={"kid": "kid-1"},
    )

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(token)

    assert caught.value.status_code == 401


async def test_a_token_missing_the_expiry_claim_is_rejected(monkeypatch) -> None:
    private_key, public_key = _keypair()
    _serve(monkeypatch, {"keys": [_jwk_for(public_key, "kid-1")]})
    token = jwt.encode(
        {"sub": "user-1", "aud": "authenticated"},
        private_key,
        algorithm="ES256",
        headers={"kid": "kid-1"},
    )

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(token)

    assert caught.value.status_code == 401


async def test_the_none_algorithm_is_rejected(monkeypatch) -> None:
    """The classic unsigned-JWT attack: an explicit `alg: none` header."""
    token = jwt.encode(
        {"sub": "attacker", "aud": "authenticated", "exp": int(time.time()) + 3600},
        key="",
        algorithm="none",
    )

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(token)

    assert caught.value.status_code == 401
    assert "algorithm" in caught.value.detail.lower()


async def test_a_wrong_audience_is_rejected(monkeypatch) -> None:
    private_key, public_key = _keypair()
    _serve(monkeypatch, {"keys": [_jwk_for(public_key, "kid-1")]})
    token = _token(private_key, kid="kid-1", aud="somewhere-else")

    with pytest.raises(HTTPException) as caught:
        await auth.decode_token(token)

    assert caught.value.status_code == 401
