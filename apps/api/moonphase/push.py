"""Web push notifications.

The one message worth interrupting someone for: the agent stopped needing to be
left alone. Everything else can wait until they open the app.

Web Push rather than a native mobile app because the client is already a web
page, and VAPID needs no third-party service — a self-hosted deployment sends
directly to the browser vendor's endpoint with its own keypair.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

from .config import get_settings

log = logging.getLogger(__name__)

# A browser only ever hands out an endpoint on one of its vendor's own push
# services — never an arbitrary URL of the page's choosing. `endpoint` still
# arrives here as a string a caller wrote by hand, and `send()` below is a
# server-side POST to whatever it says: an unchecked value turns this into an
# SSRF primitive any signed-in user can point at the internal network, cloud
# metadata endpoints, or anything else this container can reach.
#
# A domain allowlist beats a private-IP blocklist here: this is a closed set
# (every push service in real use), and unlike a blocklist it is not a race
# against redirects, decimal/octal IP encodings, or DNS rebinding — there is
# no "safe" IP address to rebind an allowed hostname's own DNS to.
ALLOWED_PUSH_ENDPOINT_SUFFIXES = (
    "fcm.googleapis.com",  # Chrome, Edge, Opera, Firefox on Android
    "updates.push.services.mozilla.com",  # Firefox desktop
    "web.push.apple.com",  # Safari
    "notify.windows.com",  # legacy Edge / WNS
)


class InvalidPushEndpoint(ValueError):
    """`endpoint` is not a real push service, or not shaped like a URL at all."""


def validate_endpoint(endpoint: str) -> None:
    """Raise unless `endpoint` is a plausible browser push-service URL.

    Called both when a subscription is stored and again immediately before
    `send()` posts to it, so a row that reached the table some other way
    (a direct DB write, an older client) cannot use this as a delivery path.
    """
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    if parsed.scheme != "https" or not host:
        raise InvalidPushEndpoint("Push endpoint must be an https:// URL.")
    if not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in ALLOWED_PUSH_ENDPOINT_SUFFIXES
    ):
        raise InvalidPushEndpoint(
            f"{host!r} is not a recognized push service endpoint."
        )


@dataclass
class Subscription:
    endpoint: str
    p256dh: str
    auth: str

    def to_info(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


class PushNotConfigured(RuntimeError):
    """No VAPID keypair, so pushes cannot be signed."""


@dataclass
class SendResult:
    """What actually happened, not just whether to keep the subscription.

    `delivered` is the only field a human waiting on "Send a test" should be
    told about. `alive` is what the monitor's silent background sends care
    about: a subscription the push service has confirmed is gone (404/410)
    should be pruned; one that merely failed this time (a timeout, a bad
    VAPID key, a payload the service rejected) should not be — that is a
    reason to say so, not a reason to forget the device.
    """

    delivered: bool
    alive: bool
    error: str | None = None


def configured() -> bool:
    settings = get_settings()
    return bool(
        settings.moonphase_vapid_public_key and settings.moonphase_vapid_private_key
    )


def public_key() -> str:
    """The application server key a browser needs to subscribe."""
    return get_settings().moonphase_vapid_public_key


def generate_keypair() -> tuple[str, str]:
    """A VAPID keypair as (applicationServerKey, private PEM).

    The public half must be the raw uncompressed EC point, base64url without
    padding — that is the only form `pushManager.subscribe` accepts. py_vapid
    exposes PEM, so derive the point directly.
    """
    vapid = Vapid01()
    vapid.generate_keys()

    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    public = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    private = vapid.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode().strip()
    return public, private


async def send(
    subscription: Subscription,
    *,
    title: str,
    body: str,
    url: str | None = None,
    tag: str | None = None,
    kind: str | None = None,
) -> SendResult:
    """Deliver one notification.

    Never raises for a delivery failure — a push must never break a caller
    that is, say, notifying twenty subscriptions in a loop — but it also
    never claims success it cannot back up. `alive=False` (the push service
    itself confirmed the subscription is gone) is the only case a caller
    should prune on; every other failure is `delivered=False` with `error`
    set to why, and the subscription is left alone since the same device
    may well work again next time.
    """
    settings = get_settings()
    if not configured():
        raise PushNotConfigured(
            "MOONPHASE_VAPID_PUBLIC_KEY and MOONPHASE_VAPID_PRIVATE_KEY are not set."
        )
    try:
        validate_endpoint(subscription.endpoint)
    except InvalidPushEndpoint as exc:
        # Should not be reachable — subscribe() already validates — but a row
        # from before this check existed, or written some other way, must be
        # pruned rather than used to make a request on the caller's behalf.
        log.warning("refusing to deliver to a bad push endpoint: %s", exc)
        return False

    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "url": url,
            "tag": tag or "moonphase",
            # Lets the worker treat a question differently from an
            # announcement: one needs answering and should stay on screen
            # until it is, the other can fade.
            "kind": kind,
        }
    )

    def _send() -> None:
        webpush(
            subscription_info=subscription.to_info(),
            data=payload,
            vapid_private_key=settings.moonphase_vapid_private_key,
            vapid_claims={"sub": settings.moonphase_vapid_subject},
            timeout=15,
        )

    try:
        # pywebpush is synchronous; keep it off the event loop, which is also
        # serving live terminals.
        await asyncio.to_thread(_send)
        return SendResult(delivered=True, alive=True)
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            log.info("push subscription gone (%s), will prune", status)
            return SendResult(delivered=False, alive=False, error=f"subscription gone ({status})")
        detail = _response_detail(exc)
        log.warning("push failed (%s): %s", status, detail)
        return SendResult(
            delivered=False, alive=True, error=f"push service answered {status}: {detail}"
        )
    except Exception as exc:  # noqa: BLE001 — a push must never break a caller
        log.warning("push error: %s", exc)
        return SendResult(delivered=False, alive=True, error=str(exc))


def _response_detail(exc: WebPushException) -> str:
    """Whatever the push service said about why, not just its status code.

    A VAPID key mismatch, an expired subscription the service has not yet
    fully forgotten, a payload it refused — the status code alone reads the
    same ("push failed (400)") for all of them, and that is not enough to
    act on. The body usually says which.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        text = response.text
    except Exception:  # noqa: BLE001 — best-effort diagnostics only
        text = None
    return (text or str(exc))[:300]
