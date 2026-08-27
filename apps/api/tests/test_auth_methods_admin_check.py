"""Defense in depth for PUT /api/setup/methods, alongside the RLS policy.

The RLS policy (`auth_methods_write`) is the real gate. This is the second
layer: `write_methods` checks instance administration itself too, so a future
regression in the DB policy does not silently reopen "any signed-in user can
rewrite the instance's SMTP relay and OAuth secrets" with nothing in the
application layer to catch it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from moonphase.routers.setup import _require_instance_admin


class _Principal:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


def _session(*, is_admin: bool):
    class _Result:
        def first(self) -> Any:
            return object() if is_admin else None

    class _Conn:
        async def execute(self, *args: Any, **kwargs: Any) -> _Result:
            return _Result()

    class _Session:
        async def __aenter__(self) -> _Conn:
            return _Conn()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    return lambda: _Session()


async def test_a_non_admin_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        "moonphase.routers.setup.service_session", _session(is_admin=False)
    )
    with pytest.raises(HTTPException) as excinfo:
        await _require_instance_admin(_Principal("someone"))
    assert excinfo.value.status_code == 403


async def test_an_instance_admin_is_allowed_through(monkeypatch) -> None:
    monkeypatch.setattr(
        "moonphase.routers.setup.service_session", _session(is_admin=True)
    )
    await _require_instance_admin(_Principal("the-admin"))  # must not raise
