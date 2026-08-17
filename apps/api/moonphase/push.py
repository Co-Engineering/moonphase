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

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

from .config import get_settings

log = logging.getLogger(__name__)


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
) -> bool:
    """Deliver one notification. False means the subscription is dead.

    A dead subscription is a normal outcome — browsers expire them and users
    clear site data — so it is reported rather than raised, and the caller
    prunes it.
    """
    settings = get_settings()
    if not configured():
        raise PushNotConfigured(
            "MOONPHASE_VAPID_PUBLIC_KEY and MOONPHASE_VAPID_PRIVATE_KEY are not set."
        )

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
        return True
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            log.info("push subscription gone (%s), will prune", status)
            return False
        log.warning("push failed (%s): %s", status, exc)
        return True
    except Exception as exc:  # noqa: BLE001 — a push must never break a caller
        log.warning("push error: %s", exc)
        return True
