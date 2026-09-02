"""A container whose image has moved on gets rebuilt, rather than started as-is.

A container keeps its creation-time image for life — there is no changing it
without recreating one. So every fix that lands in the image reaches existing
projects only if something recreates them, and until now nothing did.

The failure that forced this was real: after the worker's kernel changed,
Sysbox's ID-mapped mounts stopped resolving the old images' layer ownership.
Every root-owned binary appeared as `nobody` inside the container and `sudo`
refused to run, which breaks the one thing Docker access depends on. Bumping
RECIPE_VERSION fixed newly created containers and left every existing one
broken, recoverable only by recreating it by hand.
"""

from __future__ import annotations

import pytest

from moonphase import docker_remote
from moonphase.routers import projects


class _Ctx:
    def __init__(self, image_name: str) -> None:
        self.container = "mp-demo-1234"
        self.target = object()
        self.project = {
            "org_id": "org-1",
            "server_id": "srv-1",
            "workspace_volume": "mp-demo-1234-workspace",
            "home_volume": "mp-demo-1234-home",
            "environment": "browser",
            "repo_url": None,
            "preview_port": None,
            "docker_access": True,
        }


def _info(image: str, state: str = "running") -> docker_remote.ContainerInfo:
    return docker_remote.ContainerInfo(
        id="abc", name="mp-demo-1234", state=state, status=state, image=image
    )


async def test_a_container_on_the_current_image_is_left_alone(monkeypatch) -> None:
    async def wanted(principal, project):
        return "moonphase/env-browser:current"

    monkeypatch.setattr(projects, "_current_image_for", wanted)
    recreated = await projects._recreate_if_stale(
        object(), _Ctx("x"), _info("moonphase/env-browser:current")
    )
    assert recreated is False


async def test_a_container_on_an_older_image_is_recreated(monkeypatch) -> None:
    calls: dict[str, object] = {}

    async def wanted(principal, project):
        return "moonphase/env-browser:current"

    async def rows(principal):
        return []

    async def fake_provision(principal, **kwargs):
        calls.update(kwargs)
        return "new-container-id"

    class _Env:
        image = "moonphase/env-browser:current"

    monkeypatch.setattr(projects, "_current_image_for", wanted)
    monkeypatch.setattr(projects, "_environment_rows", rows)
    monkeypatch.setattr(projects, "_provision_container", fake_provision)
    monkeypatch.setattr(projects.environments, "resolve", lambda key, r: _Env())

    recreated = await projects._recreate_if_stale(
        object(), _Ctx("x"), _info("moonphase/env-browser:stale")
    )

    assert recreated is True
    # The volumes are what carry the work, and they must be reattached by the
    # same names — recreating with fresh ones would silently lose everything.
    assert calls["workspace_volume"] == "mp-demo-1234-workspace"
    assert calls["home_volume"] == "mp-demo-1234-home"
    assert calls["container"] == "mp-demo-1234"
    # Docker access is a creation-time property; dropping it here would
    # quietly demote the project to the plain runtime.
    assert calls["docker_access"] is True


@pytest.mark.parametrize("image", ["", None])
async def test_an_unknown_image_is_never_treated_as_stale(monkeypatch, image) -> None:
    """Better to start a container we cannot judge than to destroy it."""

    async def wanted(principal, project):
        return "moonphase/env-browser:current"

    monkeypatch.setattr(projects, "_current_image_for", wanted)
    recreated = await projects._recreate_if_stale(
        object(), _Ctx("x"), _info(image or "")
    )
    assert recreated is False


async def test_an_unresolvable_environment_is_never_treated_as_stale(monkeypatch) -> None:
    """A deleted custom environment must not condemn every container using it."""

    async def wanted(principal, project):
        return None

    monkeypatch.setattr(projects, "_current_image_for", wanted)
    recreated = await projects._recreate_if_stale(
        object(), _Ctx("x"), _info("moonphase/env-browser:whatever")
    )
    assert recreated is False


def test_inspect_carries_the_image_through() -> None:
    """The staleness check is only as good as inspect's output."""
    import inspect as _inspect

    source = _inspect.getsource(docker_remote.inspect)
    assert "{{.Config.Image}}" in source
    assert "image=parts[4]" in source.replace(" ", "")
