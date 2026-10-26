"""Lifting Ubuntu's user-namespace restriction on a server, for agy's sandbox.

No server here: a fake stands in for `ssh.run` and records each command. What
matters is that the change is made on the host, persistently and reversibly,
and that nothing is attempted without sudo.
"""

from __future__ import annotations

import pytest

from moonphase import ssh, userns_remote
from moonphase.ssh import SSHError


class _Host:
    def __init__(self, *, value: str | None = "1", sudo: bool = True) -> None:
        self.value = value
        self.lifted = False
        self.sudo = sudo
        self.commands: list[str] = []

    async def run(self, _conn: object, command: str, **kwargs: object) -> ssh.CommandResult:
        self.commands.append(command)
        if command == "sudo -n true":
            return ssh.CommandResult(exit_status=0 if self.sudo else 1, stdout="", stderr="")
        if "tee " + userns_remote.CONF_PATH in command:
            assert kwargs["stdin"] == userns_remote.CONF
            self.lifted, self.value = True, "0"
        elif "rm -f " + userns_remote.CONF_PATH in command:
            self.lifted, self.value = False, "1"
        elif command.startswith("cat "):
            out = (self.value + "\n" if self.value is not None else "") + "\n"
            return ssh.CommandResult(
                exit_status=0, stdout=out + ("lifted\n" if self.lifted else ""), stderr=""
            )
        return ssh.CommandResult(exit_status=0, stdout="", stderr="")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch):
    def make(**kwargs: object) -> _Host:
        fake = _Host(**kwargs)  # type: ignore[arg-type]
        monkeypatch.setattr(ssh, "run", fake.run)
        return fake

    return make


async def test_a_restricted_ubuntu_kernel_reads_as_restricted(host) -> None:
    host(value="1")
    info = await userns_remote.probe(None)  # type: ignore[arg-type]
    assert info.restricted is True
    assert not info.lifted_by_moonphase


async def test_a_kernel_without_the_setting_has_nothing_to_lift(host) -> None:
    host(value=None)
    info = await userns_remote.probe(None)  # type: ignore[arg-type]
    assert info.restricted is None


async def test_lifting_it_survives_a_reboot_and_restoring_it_undoes_that(host) -> None:
    fake = host(value="1")

    lifted = await userns_remote.lift(None)  # type: ignore[arg-type]
    assert lifted.restricted is False and lifted.lifted_by_moonphase
    # In /etc/sysctl.d, so the boot applies it again, and in effect now.
    assert any(
        f"tee {userns_remote.CONF_PATH}" in c and f"sysctl -q -w {userns_remote.SYSCTL}=0" in c
        for c in fake.commands
    )

    restored = await userns_remote.restore(None)  # type: ignore[arg-type]
    assert restored.restricted is True and not restored.lifted_by_moonphase


async def test_nothing_is_changed_without_passwordless_sudo(host) -> None:
    fake = host(value="1", sudo=False)

    with pytest.raises(SSHError, match="passwordless sudo"):
        await userns_remote.lift(None)  # type: ignore[arg-type]

    assert fake.commands == ["sudo -n true"]
