"""Push notification subscriptions.

The client half of "you can walk away": a browser registers here, and the
background monitor delivers to it when an agent stops working.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from .. import push, queries
from ..auth import Principal, current_principal
from ..db import user_session
from ..schemas import PushStatusOut, PushSubscriptionIn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=PushStatusOut)
async def push_status(
    principal: Principal = Depends(current_principal),
) -> PushStatusOut:
    """Whether this deployment can send, and whether this user has subscribed."""
    async with user_session(principal.claims) as conn:
        subscribed = await queries.has_push_subscription(conn)
    return PushStatusOut(
        configured=push.configured(),
        public_key=push.public_key() or None,
        subscribed=subscribed,
    )


@router.post("/subscribe", response_model=PushStatusOut)
async def subscribe(
    payload: PushSubscriptionIn, principal: Principal = Depends(current_principal)
) -> PushStatusOut:
    if not push.configured():
        raise HTTPException(
            status_code=409,
            detail=(
                "This deployment has no VAPID keypair, so it cannot send push "
                "notifications. Run scripts/gen_vapid.py and set the keys."
            ),
        )
    async with user_session(principal.claims) as conn:
        await queries.upsert_push_subscription(
            conn,
            user_id=principal.user_id,
            endpoint=payload.endpoint,
            p256dh=payload.p256dh,
            auth=payload.auth,
            user_agent=payload.user_agent,
        )
    return PushStatusOut(
        configured=True, public_key=push.public_key() or None, subscribed=True
    )


@router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(
    payload: PushSubscriptionIn, principal: Principal = Depends(current_principal)
) -> None:
    async with user_session(principal.claims) as conn:
        await queries.delete_push_subscription(conn, payload.endpoint)


@router.post("/test", status_code=status.HTTP_202_ACCEPTED)
async def send_test(
    principal: Principal = Depends(current_principal),
) -> dict[str, int]:
    """Send a notification to this user's devices.

    Worth having: push depends on service worker registration, browser
    permission and a reachable endpoint, and a silent failure in any of them is
    otherwise only discovered when a real notification does not arrive.
    """
    if not push.configured():
        raise HTTPException(status_code=409, detail="Push is not configured.")

    async with user_session(principal.claims) as conn:
        subs = await queries.list_own_push_subscriptions(conn)

    delivered = 0
    for sub in subs:
        alive = await push.send(
            push.Subscription(
                endpoint=sub["endpoint"], p256dh=sub["p256dh"], auth=sub["auth"]
            ),
            title="Moonphase",
            body="Notifications are working. You can close the app now.",
            tag="moonphase-test",
        )
        if alive:
            delivered += 1
    return {"delivered": delivered, "subscriptions": len(subs)}
