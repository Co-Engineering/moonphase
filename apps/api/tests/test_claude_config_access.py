"""Access levels for project-scope Claude config.

`GET /config` must require CAN_CONTROL, not CAN_OBSERVE (what a plain viewer
share grants) -- env_vars exists specifically to hold things like a
project-only database URL, unencrypted, in the same row. `PUT /config` must
require CAN_ADMINISTER, not CAN_CONTROL -- it applies, unreviewed, to every
other collaborator's session including a project admin's own.

No database here: these pin exactly which access level each endpoint asks
`runtime.load_project_context` for, by making that call raise before
anything DB-backed runs, and catching the access level it was asked to
check against.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from moonphase import runtime
from moonphase.auth import Principal
from moonphase.routers import claude_config
from moonphase.runtime import CAN_ADMINISTER, CAN_CONTROL, Forbidden
from moonphase.schemas import ClaudeConfigIn


def _principal() -> Principal:
    return Principal(user_id="u1", email=None, claims={"sub": "u1"})


@pytest.mark.asyncio
async def test_get_project_config_requires_can_control_not_can_observe(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_load_project_context(claims, project_id, *, require=CAN_CONTROL):
        captured["require"] = require
        raise Forbidden("stop here — this test only checks what access level was asked for")

    monkeypatch.setattr(runtime, "load_project_context", fake_load_project_context)

    with pytest.raises(HTTPException) as caught:
        await claude_config.get_project_config(uuid.uuid4(), principal=_principal())
    assert caught.value.status_code == 403
    assert captured["require"] == CAN_CONTROL


@pytest.mark.asyncio
async def test_update_project_config_requires_can_administer_not_can_control(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_load_project_context(claims, project_id, *, require=CAN_CONTROL):
        captured["require"] = require
        raise Forbidden("stop here — this test only checks what access level was asked for")

    monkeypatch.setattr(runtime, "load_project_context", fake_load_project_context)

    with pytest.raises(HTTPException) as caught:
        await claude_config.update_project_config(
            uuid.uuid4(), payload=ClaudeConfigIn(), principal=_principal()
        )
    assert caught.value.status_code == 403
    assert captured["require"] == CAN_ADMINISTER
