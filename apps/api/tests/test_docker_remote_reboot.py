"""`docker_remote.reboot`, without a real SSH connection or a real machine to
reboot — this is a pure command-shape and error-handling check. See
test_sysbox_remote.py for why other host-level operations are mocked the same
way: there is no rig here that can safely exercise an actual reboot.
"""

from __future__ import annotations

import pytest

from moonphase import docker_remote
from moonphase.ssh import CommandResult, SSHError


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(exit_status=0, stdout=stdout, stderr="")


def _fail(stderr: str = "") -> CommandResult:
    return CommandResult(exit_status=1, stdout="", stderr=stderr)


class FakeSSH:
    """Records every command; answers (or raises) by the first matching
    substring."""

    def __init__(self) -> None:
        self.responses: list[tuple[str, CommandResult | Exception]] = []
        self.calls: list[str] = []

    def set(self, substring: str, result: CommandResult | Exception) -> None:
        self.responses.append((substring, result))

    async def run(self, conn, command, *, timeout: float = 60.0, stdin=None):
        self.calls.append(command)
        for substring, result in self.responses:
            if substring in command:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unstubbed command: {command!r}")


def _fake(monkeypatch) -> FakeSSH:
    fake = FakeSSH()
    monkeypatch.setattr(docker_remote.ssh, "run", fake.run)
    return fake


async def test_reboot_without_passwordless_sudo_raises_before_rebooting(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("sudo -n true", _fail())

    with pytest.raises(SSHError, match="passwordless sudo"):
        await docker_remote.reboot(None)

    assert not any("reboot" in cmd for cmd in fake.calls)


async def test_reboot_happy_path_issues_systemctl_reboot(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("sudo -n true", _ok())
    fake.set("systemctl reboot", _ok())

    await docker_remote.reboot(None)

    assert any("sudo -n systemctl reboot" in cmd for cmd in fake.calls)


async def test_reboot_treats_the_connection_dying_mid_command_as_success(monkeypatch) -> None:
    """The whole point of this call is that the machine may go down before it
    ever answers — that is the reboot working, not a failure to surface."""
    fake = _fake(monkeypatch)
    fake.set("sudo -n true", _ok())
    fake.set("systemctl reboot", SSHError("connection lost"))

    await docker_remote.reboot(None)  # must not raise
