"""Getting agy's terminal sandbox — and so Auto Mode — to start.

No server here. A fake host answers the setting `userns_remote` reads and
writes, and a fake container answers the probe that stands in for agy's
sandbox: refused while the host restricts user namespaces, refused under
Docker's seccomp filter whatever the host says, allowed otherwise.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from moonphase import agy_sandbox, docker_remote, ssh, userns_remote
from moonphase.docker_remote import ContainerInfo


class _Server:
    def __init__(self, *, restricted: bool, sudo: bool = True, sysbox: bool = True) -> None:
        self.value = "1" if restricted else "0"
        self.lifted = False
        self.sudo = sudo
        self.sysbox = sysbox
        self.probes = 0

    async def run(self, _conn: Any, command: str, **_: Any) -> ssh.CommandResult:
        if command == "sudo -n true":
            return ssh.CommandResult(exit_status=0 if self.sudo else 1, stdout="", stderr="")
        if "tee " + userns_remote.CONF_PATH in command:
            self.value, self.lifted = "0", True
        elif command.startswith("cat "):
            out = f"{self.value}\n\n" + ("lifted\n" if self.lifted else "")
            return ssh.CommandResult(exit_status=0, stdout=out, stderr="")
        return ssh.CommandResult(exit_status=0, stdout="", stderr="")

    async def exec_capture(
        self, _conn: Any, _container: str, command: list[str], **_: Any
    ) -> ssh.CommandResult:
        assert command == agy_sandbox.PROBE
        self.probes += 1
        works = self.sysbox and self.value == "0"
        return ssh.CommandResult(
            exit_status=0 if works else 1,
            stdout="",
            stderr="" if works else "unshare: write failed /proc/self/uid_map",
        )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    def make(**kwargs: Any) -> _Server:
        fake = _Server(**kwargs)
        monkeypatch.setattr(ssh, "run", fake.run)
        monkeypatch.setattr(docker_remote, "exec_capture", fake.exec_capture)
        return fake

    return make


async def test_the_owner_starting_agy_gets_the_restriction_lifted(server) -> None:
    """The fix is made when it is needed, not left on a page to be found."""
    fake = server(restricted=True)

    note = await agy_sandbox.prepare(None, "c", administers_server=True)  # type: ignore[arg-type]

    assert fake.lifted
    assert note == agy_sandbox.LIFTED


async def test_nothing_changes_or_is_said_where_it_already_works(server) -> None:
    fake = server(restricted=False)

    assert await agy_sandbox.prepare(None, "c", administers_server=True) is None  # type: ignore[arg-type]
    assert not fake.lifted
    assert fake.probes == 1


async def test_someone_elses_server_is_never_changed(server) -> None:
    fake = server(restricted=True)

    note = await agy_sandbox.prepare(None, "c", administers_server=False)  # type: ignore[arg-type]

    assert not fake.lifted
    assert note and "owner can allow them" in note


async def test_without_sudo_it_says_so_and_the_session_still_starts(server) -> None:
    fake = server(restricted=True, sudo=False)

    note = await agy_sandbox.prepare(None, "c", administers_server=True)  # type: ignore[arg-type]

    assert not fake.lifted
    assert note and "passwordless sudo" in note


async def test_a_container_without_sysbox_is_named_as_the_reason(server) -> None:
    """The host is fine; Docker's seccomp filter is what refuses. Lifting the
    host's restriction again would not help, and is not what it says."""
    server(restricted=False, sysbox=False)

    note = await agy_sandbox.prepare(None, "c", administers_server=True)  # type: ignore[arg-type]

    assert note == agy_sandbox.CONTAINER_NOTE


# --- Docker access, switched after creation ------------------------------------


@pytest.mark.parametrize(
    ("runtime", "docker_access", "recreated"),
    [
        ("runc", True, True),
        ("sysbox-runc", False, True),
        ("sysbox-runc", True, False),
        ("runc", False, False),
        ("", True, False),
    ],
)
async def test_the_container_follows_the_projects_docker_access(
    monkeypatch: pytest.MonkeyPatch, runtime: str, docker_access: bool, recreated: bool
) -> None:
    from types import SimpleNamespace

    from moonphase.routers import projects

    provisioned: list[dict[str, Any]] = []

    async def current_image(_principal: Any, _project: Any) -> str:
        return "img"

    async def provision(_principal: Any, **kwargs: Any) -> str:
        provisioned.append(kwargs)
        return "id"

    async def environment_rows(_principal: Any) -> list[Any]:
        return []

    monkeypatch.setattr(projects, "_current_image_for", current_image)
    monkeypatch.setattr(projects, "_provision_container", provision)
    monkeypatch.setattr(projects, "_environment_rows", environment_rows)
    project = {
        "org_id": "o", "server_id": "s", "workspace_volume": "w", "home_volume": "h",
        "environment": None, "repo_url": None, "preview_port": None,
        "docker_access": docker_access,
    }
    ctx = SimpleNamespace(project=project, container="c")

    result = await projects._recreate_if_stale(
        None,  # type: ignore[arg-type]
        ctx,  # type: ignore[arg-type]
        ContainerInfo(id="i", name="c", state="running", status="", image="img", runtime=runtime),
    )

    assert result is recreated
    if recreated:
        assert provisioned[0]["docker_access"] is docker_access


# --- agy settings --------------------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [
        {"agentMode": "yolo"},
        {"toolPermission": "sometimes"},
        {"modelProvider": "gemini"},
        {"trustedWorkspaces": ["/"]},
    ],
)
def test_agy_settings_it_would_ignore_or_that_moonphase_owns_are_refused(
    settings: dict[str, Any],
) -> None:
    from moonphase.routers.profile import _check_antigravity_settings

    with pytest.raises(HTTPException) as refused:
        _check_antigravity_settings(settings)
    assert refused.value.status_code == 422


def test_agy_settings_it_knows_are_accepted() -> None:
    from moonphase.routers.profile import _check_antigravity_settings

    _check_antigravity_settings(
        {"agentMode": "auto", "toolPermission": "always-proceed", "verbosity": "low"}
    )
