"""`run_container`'s --runtime flag, without a real SSH connection.

Every other docker_remote test in this repo exercises run_container against
a real Docker-in-Docker fake-server image; this one is a pure command-shape
check, since the thing worth pinning down here is what flags get built for a
given `runtime` argument — not whether `docker run` itself works.
"""

from __future__ import annotations

from moonphase import docker_remote
from moonphase.ssh import CommandResult


class _RecordingSSH:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, conn, command, *, timeout: float = 60.0, stdin=None):
        self.commands.append(command)
        return CommandResult(exit_status=0, stdout="abc123\n", stderr="")


def _fake(monkeypatch) -> _RecordingSSH:
    fake = _RecordingSSH()
    monkeypatch.setattr(docker_remote.ssh, "run", fake.run)
    return fake


async def test_no_runtime_argument_adds_no_runtime_flag(monkeypatch) -> None:
    """Today's default behavior, unchanged."""
    fake = _fake(monkeypatch)

    await docker_remote.run_container(
        None,
        name="mp-test",
        image="moonphase/runtime-claude:debian",
        workspace_volume="mp-test-workspace",
        home_volume="mp-test-home",
    )

    assert "--runtime" not in fake.commands[0]


async def test_docker_access_adds_the_sysbox_runtime_flag(monkeypatch) -> None:
    fake = _fake(monkeypatch)

    await docker_remote.run_container(
        None,
        name="mp-test",
        image="moonphase/runtime-claude:debian",
        workspace_volume="mp-test-workspace",
        home_volume="mp-test-home",
        runtime="sysbox-runc",
    )

    assert "--runtime sysbox-runc" in fake.commands[0]


async def test_privileged_and_cap_add_never_appear_regardless_of_runtime(
    monkeypatch,
) -> None:
    """Sysbox documents these must not be combined with --runtime=sysbox-runc
    — the runtime itself grants what a nested Docker workload needs, and
    stacking --privileged on top defeats the isolation it exists to provide.
    This is an invariant of the function, not just of one call."""
    fake = _fake(monkeypatch)

    for runtime in (None, "sysbox-runc"):
        await docker_remote.run_container(
            None,
            name="mp-test",
            image="moonphase/runtime-claude:debian",
            workspace_volume="mp-test-workspace",
            home_volume="mp-test-home",
            runtime=runtime,
        )

    for command in fake.commands:
        assert "--privileged" not in command
        assert "--cap-add" not in command
