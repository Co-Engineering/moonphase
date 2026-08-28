"""Sysbox compatibility/probe/install logic, exercised without real SSH.

Every other docker_remote/provision test in this repo runs against a real
`moonphase/fake-server` Docker-in-Docker image — but that image cannot
exercise Sysbox at all: Sysbox needs a real second kernel with matching
compatibility, which no container-based test rig here provides. So this file
mocks `ssh.run` directly instead, recording every command issued, which is
also what makes "an incompatible host must never even attempt a download"
directly assertable.
"""

from __future__ import annotations

import pytest

from moonphase import sysbox_remote
from moonphase.ssh import CommandResult, SSHError

UBUNTU_OLD_KERNEL_OS_RELEASE = (
    'NAME="Ubuntu"\nVERSION="22.04.3 LTS (Jammy Jellyfish)"\nID=ubuntu\n'
    'ID_LIKE=debian\nVERSION_ID="22.04"\n'
)
DEBIAN_OS_RELEASE = (
    'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nNAME="Debian GNU/Linux"\n'
    'VERSION_ID="12"\nVERSION="12 (bookworm)"\nID=debian\n'
)
FEDORA_OS_RELEASE = 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID="39"\n'


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(exit_status=0, stdout=stdout, stderr="")


def _fail(stderr: str = "") -> CommandResult:
    return CommandResult(exit_status=1, stdout="", stderr=stderr)


class FakeSSH:
    """Records every command; answers by the first matching substring."""

    def __init__(self) -> None:
        self.responses: list[tuple[str, CommandResult]] = []
        self.calls: list[str] = []

    def set(self, substring: str, result: CommandResult) -> None:
        self.responses.append((substring, result))

    async def run(self, conn, command, *, timeout: float = 60.0, stdin=None):
        self.calls.append(command)
        for substring, result in self.responses:
            if substring in command:
                return result
        raise AssertionError(f"unstubbed command: {command!r}")


def _fake(monkeypatch) -> FakeSSH:
    fake = FakeSSH()
    monkeypatch.setattr(sysbox_remote.ssh, "run", fake.run)
    return fake


# --- probe_compatibility -----------------------------------------------------


async def test_a_modern_debian_kernel_is_compatible_via_idmap_alone(monkeypatch) -> None:
    """The common case going forward: no shiftfs at all, and it should never
    even be checked for — that module is Ubuntu-only and this host is not."""
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(DEBIAN_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64\n"))
    fake.set("uname -r", _ok("6.1.0-13-amd64\n"))

    result = await sysbox_remote.probe_compatibility(None)

    assert result.compatible is True
    assert result.kernel_supports_idmap is True
    assert result.shiftfs_available is False
    assert not any("modinfo" in c for c in fake.calls), fake.calls


async def test_an_old_ubuntu_kernel_falls_back_to_shiftfs_when_present(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(UBUNTU_OLD_KERNEL_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64\n"))
    fake.set("uname -r", _ok("5.15.0-91-generic\n"))
    fake.set("modinfo shiftfs", _ok("filename: /lib/modules/.../shiftfs.ko\n"))

    result = await sysbox_remote.probe_compatibility(None)

    assert result.compatible is True
    assert result.kernel_supports_idmap is False
    assert result.shiftfs_available is True


async def test_an_old_ubuntu_kernel_without_shiftfs_is_incompatible(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(UBUNTU_OLD_KERNEL_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64\n"))
    fake.set("uname -r", _ok("5.15.0-91-generic\n"))
    fake.set("modinfo shiftfs", _fail("modinfo: ERROR: Module shiftfs not found."))

    result = await sysbox_remote.probe_compatibility(None)

    assert result.compatible is False
    assert "kernel" in (result.detail or "").lower()


async def test_an_unsupported_os_is_refused_by_name(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(FEDORA_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64\n"))
    fake.set("uname -r", _ok("6.8.0-1-amd64\n"))

    result = await sysbox_remote.probe_compatibility(None)

    assert result.compatible is False
    assert "Ubuntu or Debian" in (result.detail or "")
    # Fedora with a modern kernel would otherwise pass the idmap check —
    # this confirms the OS check is independently enforced, not subsumed by it.
    assert not any("modinfo" in c for c in fake.calls)


async def test_an_unsupported_architecture_is_refused(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(DEBIAN_OS_RELEASE))
    fake.set("uname -m", _ok("ppc64le\n"))
    fake.set("uname -r", _ok("6.1.0-13-powerpc64le\n"))

    result = await sysbox_remote.probe_compatibility(None)

    assert result.compatible is False
    assert "ppc64le" in (result.detail or "")


# --- probe --------------------------------------------------------------------


async def test_probe_reports_not_installed_when_the_binary_is_missing(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("command -v sysbox-runc", _fail())

    info = await sysbox_remote.probe(None)

    assert info.installed is False


async def test_probe_reports_unregistered_when_docker_does_not_know_it(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("command -v sysbox-runc", _ok("/usr/bin/sysbox-runc\n"))
    fake.set("sysbox-runc --version", _ok("sysbox-runc\nversion: 0.6.7\n"))
    fake.set("docker info", _ok('{"runc":{"path":"runc"}}\n'))

    info = await sysbox_remote.probe(None)

    assert info.installed is True
    assert info.registered_as_runtime is False
    assert "not registered" in (info.detail or "")


async def test_probe_reports_fully_installed_when_both_agree(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("command -v sysbox-runc", _ok("/usr/bin/sysbox-runc\n"))
    fake.set("sysbox-runc --version", _ok("sysbox-runc\nversion: 0.6.7\n"))
    fake.set(
        "docker info",
        _ok('{"runc":{"path":"runc"},"sysbox-runc":{"path":"/usr/bin/sysbox-runc"}}\n'),
    )

    info = await sysbox_remote.probe(None)

    assert info.installed is True
    assert info.registered_as_runtime is True
    assert info.version == "sysbox-runc"
    assert info.detail is None


# --- install --------------------------------------------------------------


async def test_install_refuses_an_incompatible_host_without_downloading_anything(
    monkeypatch,
) -> None:
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(FEDORA_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64\n"))
    fake.set("uname -r", _ok("6.8.0-1-amd64\n"))

    info = await sysbox_remote.install(None, "deploy")

    assert info.installed is False
    assert not any(
        cmd.startswith(("curl", "sudo -n dpkg")) for cmd in fake.calls
    ), fake.calls


async def test_install_without_passwordless_sudo_raises_before_downloading(
    monkeypatch,
) -> None:
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(DEBIAN_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64\n"))
    fake.set("uname -r", _ok("6.1.0-13-amd64\n"))
    fake.set("sudo -n true", _fail())

    with pytest.raises(SSHError, match="passwordless sudo"):
        await sysbox_remote.install(None, "deploy")

    assert not any(cmd.startswith("curl") for cmd in fake.calls)


async def test_install_without_docker_raises_before_downloading(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(DEBIAN_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64\n"))
    fake.set("uname -r", _ok("6.1.0-13-amd64\n"))
    fake.set("sudo -n true", _ok())
    fake.set("command -v docker", _fail())

    with pytest.raises(SSHError, match="Docker must be installed"):
        await sysbox_remote.install(None, "deploy")

    assert not any(cmd.startswith("curl") for cmd in fake.calls)


async def test_install_happy_path_downloads_dpkgs_and_reprobes(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("os-release", _ok(DEBIAN_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64\n"))
    fake.set("uname -r", _ok("6.1.0-13-amd64\n"))
    fake.set("sudo -n true", _ok())
    fake.set("command -v docker", _ok("/usr/bin/docker\n"))
    fake.set("curl -fsSL", _ok())
    fake.set("sudo -n dpkg -i", _ok())
    fake.set("sudo -n apt-get update", _ok())
    fake.set("sudo -n apt-get install -f", _ok())
    fake.set("rm -f /tmp/sysbox-ce", _ok())
    fake.set("command -v sysbox-runc", _ok("/usr/bin/sysbox-runc\n"))
    fake.set("sysbox-runc --version", _ok("sysbox-runc\nversion: 0.6.7\n"))
    fake.set(
        "docker info",
        _ok('{"sysbox-runc":{"path":"/usr/bin/sysbox-runc"}}\n'),
    )

    info = await sysbox_remote.install(None, "deploy")

    assert info.installed is True
    assert info.registered_as_runtime is True

    # The .deb filename is arch-substituted, and every step ran in order.
    assert any("sysbox-ce_" in c and "linux_amd64.deb" in c for c in fake.calls)
    ordered = [c for c in fake.calls if c.split()[0] in ("curl", "sudo")]
    assert any(c.startswith("curl") for c in ordered)
    dpkg_index = next(i for i, c in enumerate(fake.calls) if "dpkg -i" in c)
    curl_index = next(i for i, c in enumerate(fake.calls) if c.startswith("curl"))
    assert curl_index < dpkg_index
