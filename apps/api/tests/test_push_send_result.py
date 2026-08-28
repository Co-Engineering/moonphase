"""push.send()'s result has to tell the truth about what happened.

It used to return a bare bool where True meant "do not prune the
subscription" — which a caller waiting on "Send a test" read as "delivered",
even when the push service had rejected the request outright. A VAPID key
mismatch, a timeout reaching the push service, a payload it refused: all of
these used to come back indistinguishable from success.
"""

from __future__ import annotations

import base64

import pytest
from pywebpush import WebPushException

from moonphase import push
from moonphase.config import get_settings


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    get_settings.cache_clear()
    key = base64.urlsafe_b64encode(b"x" * 32).decode()
    monkeypatch.setenv("MOONPHASE_SECRET_KEY", key)
    monkeypatch.setenv("MOONPHASE_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("MOONPHASE_VAPID_PRIVATE_KEY", "private-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _subscription() -> push.Subscription:
    return push.Subscription(endpoint="https://fcm.googleapis.com/x", p256dh="p", auth="a")


async def test_a_successful_send_is_delivered_and_alive(monkeypatch) -> None:
    monkeypatch.setattr(push, "webpush", lambda **kwargs: None)

    result = await push.send(_subscription(), title="t", body="b")

    assert result.delivered is True
    assert result.alive is True
    assert result.error is None


async def test_a_gone_subscription_is_not_delivered_and_not_alive(monkeypatch) -> None:
    def _raise(**kwargs: object) -> None:
        raise WebPushException("gone", response=_FakeResponse(410))

    monkeypatch.setattr(push, "webpush", _raise)

    result = await push.send(_subscription(), title="t", body="b")

    assert result.delivered is False
    assert result.alive is False


async def test_a_404_is_treated_the_same_as_410(monkeypatch) -> None:
    def _raise(**kwargs: object) -> None:
        raise WebPushException("gone", response=_FakeResponse(404))

    monkeypatch.setattr(push, "webpush", _raise)

    result = await push.send(_subscription(), title="t", body="b")

    assert result.alive is False


async def test_a_rejected_push_is_not_delivered_but_the_subscription_stays(
    monkeypatch,
) -> None:
    """The bug this closes: this used to come back indistinguishable from
    success, because 'not 404/410' meant 'return True'."""

    def _raise(**kwargs: object) -> None:
        raise WebPushException("bad request", response=_FakeResponse(400, "invalid vapid key"))

    monkeypatch.setattr(push, "webpush", _raise)

    result = await push.send(_subscription(), title="t", body="b")

    assert result.delivered is False
    assert result.alive is True, "a rejected push must not prune a device that might work later"
    assert result.error is not None
    assert "400" in result.error
    assert "invalid vapid key" in result.error


async def test_a_network_failure_is_not_delivered_but_the_subscription_stays(
    monkeypatch,
) -> None:
    def _raise(**kwargs: object) -> None:
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(push, "webpush", _raise)

    result = await push.send(_subscription(), title="t", body="b")

    assert result.delivered is False
    assert result.alive is True
    assert "timed out" in (result.error or "")
