"""A push subscription's `endpoint` is a URL the server will later POST to.

Unchecked, that is an SSRF primitive any signed-in user can point at the
internal network, cloud metadata endpoints, or anything else this container
can reach — `push.send()` calls `pywebpush.webpush()` against whatever the
subscriber wrote down. Real browsers only ever hand out an endpoint on one of
a small, fixed set of vendor push services, so that set is the allowlist.
"""

from __future__ import annotations

import pytest

from moonphase import push
from moonphase.push import InvalidPushEndpoint
from moonphase.schemas import PushSubscriptionIn

# --- push.validate_endpoint ---------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc123",
        "https://updates.push.services.mozilla.com/wpush/v2/xyz",
        "https://web.push.apple.com/some-id",
        "https://sn1p.notify.windows.com/w/?token=abc",
        # A vendor-side subdomain is still theirs.
        "https://abc.fcm.googleapis.com/fcm/send/def",
    ],
)
def test_real_push_service_urls_are_accepted(endpoint: str) -> None:
    push.validate_endpoint(endpoint)  # must not raise


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc123",  # not https
        "https://127.0.0.1:8080/",
        "https://localhost/",
        "https://169.254.169.254/latest/meta-data/",
        "https://internal.corp.example/webhook",
        "https://evil.com/fcm.googleapis.com",
        "https://fcm.googleapis.com.evil.com/",  # suffix lookalike
        "not-a-url-at-all",
        "",
    ],
)
def test_anything_else_is_refused(endpoint: str) -> None:
    with pytest.raises(InvalidPushEndpoint):
        push.validate_endpoint(endpoint)


# --- the schema, which is what actually gates the API -------------------------


def test_the_subscribe_schema_accepts_a_real_endpoint() -> None:
    sub = PushSubscriptionIn(
        endpoint="https://fcm.googleapis.com/fcm/send/abc123",
        p256dh="x" * 8,
        auth="y" * 8,
    )
    assert sub.endpoint.startswith("https://fcm.googleapis.com")


def test_the_subscribe_schema_refuses_an_arbitrary_url() -> None:
    with pytest.raises(ValueError, match="recognized push service"):
        PushSubscriptionIn(
            endpoint="https://169.254.169.254/latest/meta-data/",
            p256dh="x" * 8,
            auth="y" * 8,
        )


# --- send() re-checks, in case a bad row got in some other way ---------------


async def test_send_prunes_rather_than_delivers_to_a_bad_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(push, "configured", lambda: True)

    def _must_not_be_called(**kwargs: object) -> None:
        raise AssertionError("webpush() must not be called for a bad endpoint")

    monkeypatch.setattr(push, "webpush", _must_not_be_called)

    subscription = push.Subscription(
        endpoint="https://internal.example/steal", p256dh="p", auth="a"
    )
    delivered = await push.send(subscription, title="t", body="b")

    assert delivered is False
