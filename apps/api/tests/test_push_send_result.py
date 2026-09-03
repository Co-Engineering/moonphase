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
from py_vapid import Vapid01
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
    # A real keypair, not a placeholder string: send() parses this PEM itself
    # before ever reaching the mocked webpush() below, so a fixture that
    # doesn't actually parse is a fixture that can't catch send() and
    # generate_keypair() disagreeing about the key's format — which is
    # exactly the bug this file's tests all used to fly straight past.
    public, private = push.generate_keypair()
    monkeypatch.setenv("MOONPHASE_VAPID_PUBLIC_KEY", public)
    monkeypatch.setenv("MOONPHASE_VAPID_PRIVATE_KEY", private)
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


async def test_send_hands_webpush_a_parsed_key_not_the_raw_pem_string(monkeypatch) -> None:
    """The actual bug: generate_keypair() emits PEM, but pywebpush only
    treats `vapid_private_key` as PEM when it is already a `Vapid01`
    instance -- any other string (a file path or not) goes through
    `Vapid.from_string()`, which expects a bare base64/DER key with no PEM
    armor and fails deep inside `cryptography` with an opaque "ASN.1 parsing
    error: invalid length", for every key, valid or not. Every other test in
    this file would pass whether send() got this right or not, since none of
    them inspect what was actually passed to webpush() -- this one does."""
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(push, "webpush", _capture)

    result = await push.send(_subscription(), title="t", body="b")

    assert result.delivered is True, result.error
    assert isinstance(captured["vapid_private_key"], Vapid01)


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
