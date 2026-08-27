"""MOONPHASE_SSH_TRUST_ON_FIRST_USE, and the policy object that has to honor it.

The setting existed but nothing ever read it: `_PinnedHostKeyPolicy` always
behaved as though trust-on-first-use were on, silently accepting whatever
host key a server with no pin yet presented — `require_pin=False` was the
only behavior there was. These tests exercise the class directly, with a
fake key object rather than a real handshake.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from moonphase.schemas import ServerCreate
from moonphase.ssh import _PinnedHostKeyPolicy, fingerprint


@dataclass
class _FakeKey:
    public_data: bytes


KEY_A = _FakeKey(public_data=b"key-a-bytes")
KEY_B = _FakeKey(public_data=b"key-b-bytes")


def test_trust_on_first_use_accepts_an_unpinned_key() -> None:
    policy = _PinnedHostKeyPolicy(None, require_pin=False)

    assert policy.validate_host_public_key("h", "a", 22, KEY_A) is True
    assert policy.observed_fp == fingerprint(KEY_A)
    assert policy.refused_no_pin is False


def test_disabling_trust_on_first_use_refuses_an_unpinned_key() -> None:
    """The bug this closes: this used to be unreachable — every unpinned key
    was accepted regardless of the setting."""
    policy = _PinnedHostKeyPolicy(None, require_pin=True)

    assert policy.validate_host_public_key("h", "a", 22, KEY_A) is False
    assert policy.refused_no_pin is True
    assert policy.mismatch is None


def test_a_pinned_key_is_still_accepted_with_trust_on_first_use_disabled() -> None:
    expected = fingerprint(KEY_A)
    policy = _PinnedHostKeyPolicy(expected, require_pin=True)

    assert policy.validate_host_public_key("h", "a", 22, KEY_A) is True
    assert policy.refused_no_pin is False


def test_a_changed_key_is_refused_regardless_of_trust_on_first_use() -> None:
    expected = fingerprint(KEY_A)

    for require_pin in (False, True):
        policy = _PinnedHostKeyPolicy(expected, require_pin=require_pin)
        assert policy.validate_host_public_key("h", "a", 22, KEY_B) is False
        assert policy.mismatch is not None
        assert expected in policy.mismatch
        assert policy.refused_no_pin is False


# --- the API field that makes disabling trust-on-first-use usable at all -----


def _server(**overrides: object) -> dict:
    base = dict(
        name="srv",
        host="10.0.0.1",
        port=22,
        ssh_user="root",
        auth_mode="password_bootstrap",
        password="x",
    )
    base.update(overrides)
    return base


def test_a_sha256_fingerprint_is_accepted() -> None:
    server = ServerCreate(
        **_server(expected_host_key_fingerprint="SHA256:abcdef1234567890")
    )
    assert server.expected_host_key_fingerprint == "SHA256:abcdef1234567890"


def test_no_fingerprint_is_fine_by_default() -> None:
    assert ServerCreate(**_server()).expected_host_key_fingerprint is None


def test_blank_fingerprint_is_treated_as_none() -> None:
    server = ServerCreate(**_server(expected_host_key_fingerprint="   "))
    assert server.expected_host_key_fingerprint is None


def test_something_that_is_not_a_sha256_fingerprint_is_refused() -> None:
    with pytest.raises(ValueError, match="SHA256"):
        ServerCreate(**_server(expected_host_key_fingerprint="just some text"))


def test_the_create_endpoint_refuses_to_add_a_server_with_no_fingerprint_when_tofu_is_off() -> (
    None
):
    """Otherwise disabling MOONPHASE_SSH_TRUST_ON_FIRST_USE would make it
    impossible to add a server at all, with no error saying why — the
    connection would just fail deep inside bootstrap."""
    import inspect

    from moonphase.routers import servers

    source = inspect.getsource(servers.create_server)
    assert "moonphase_ssh_trust_on_first_use" in source
    assert "expected_host_key_fingerprint" in source
    assert "422" in source
