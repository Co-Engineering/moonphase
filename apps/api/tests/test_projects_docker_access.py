"""`_require_sysbox_if_docker_access` — a project cannot ask for Docker
access on a server that cannot grant it.

Factored out of create_project specifically so it is testable with a
fabricated dict, no database or HTTP client needed.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from moonphase.routers.projects import _require_sysbox_if_docker_access


def test_docker_access_without_sysbox_is_refused() -> None:
    server = {"name": "srv-1", "sysbox_version": None}

    with pytest.raises(HTTPException) as caught:
        _require_sysbox_if_docker_access(server, True)

    assert caught.value.status_code == 422
    assert "Sysbox" in caught.value.detail
    assert "srv-1" in caught.value.detail


def test_docker_access_with_sysbox_installed_is_allowed() -> None:
    server = {"name": "srv-1", "sysbox_version": "0.6.7"}

    _require_sysbox_if_docker_access(server, True)  # must not raise


def test_no_docker_access_never_raises_regardless_of_sysbox() -> None:
    without_sysbox = {"name": "srv-1", "sysbox_version": None}
    with_sysbox = {"name": "srv-1", "sysbox_version": "0.6.7"}
    for server in (without_sysbox, with_sysbox):
        _require_sysbox_if_docker_access(server, False)  # must not raise
