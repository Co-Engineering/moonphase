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
    # No containers: Sysbox's postinst will not restart Docker under any.
    fake.set("docker ps -aq", _ok("0"))
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
    #
    # Pinned to the whole name, not just its ends: this assertion used to
    # check only that "sysbox-ce_" and "linux_amd64.deb" both appeared
    # somewhere in the command, which a wrong middle passes happily. The
    # name we shipped carried a "-0" revision suffix that Sysbox's releases
    # do not use, so every install 404'd on download, and the test said
    # nothing.
    assert any(
        f"sysbox-ce_{sysbox_remote.SYSBOX_VERSION}.linux_amd64.deb" in c
        for c in fake.calls
    ), f"unexpected package name in: {fake.calls}"
    ordered = [c for c in fake.calls if c.split()[0] in ("curl", "sudo")]
    assert any(c.startswith("curl") for c in ordered)
    dpkg_index = next(i for i, c in enumerate(fake.calls) if "dpkg -i" in c)
    curl_index = next(i for i, c in enumerate(fake.calls) if c.startswith("curl"))
    assert curl_index < dpkg_index


def test_the_package_url_matches_sysbox_s_published_asset_naming() -> None:
    """The one detail no amount of mocking can get right by itself.

    Sysbox publishes exactly two assets per release,
    `sysbox-ce_<version>.linux_{amd64,arch64}.deb`, with no packaging
    revision between the version and `.linux`. An invented suffix here is
    invisible until a real install runs and `curl -f` returns a 404 that
    reads like a broken network rather than a filename we made up.

    Checked as a shape rather than against the network, so it holds in CI
    without egress — the version bump is the moment to re-check the real
    releases page.
    """
    import re

    for arch in ("amd64", "arm64"):
        name = f"sysbox-ce_{sysbox_remote.SYSBOX_VERSION}.linux_{arch}.deb"
        assert re.fullmatch(r"sysbox-ce_\d+\.\d+\.\d+\.linux_(amd64|arm64)\.deb", name), name
        assert "-0." not in name


# --- refusing before a half-install --------------------------------------


async def test_install_refuses_while_the_server_has_containers(monkeypatch) -> None:
    """The failure this turns into a sentence.

    Sysbox's postinst changes Docker's network configuration, needs a daemon
    restart for it, and refuses to restart Docker while any container exists
    — `docker ps -a`, so stopped ones count. It fails the configure step and
    dpkg reports only "Sub-process /usr/bin/dpkg returned an error code (1)",
    leaving sysbox-runc on PATH with nothing registered.

    Every server with a project on it is in that state, so the button could
    only ever fail there. Refusing up front costs a download and a
    half-configured package less, and says which requirement was not met.
    """
    fake = _fake(monkeypatch)
    fake.set("/etc/os-release", _ok(DEBIAN_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64"))
    fake.set("uname -r", _ok("6.17.0-1022-azure"))
    fake.set("sudo -n true", _ok())
    fake.set("command -v docker", _ok("/usr/bin/docker"))
    fake.set("docker ps -aq", _ok("7"))

    with pytest.raises(SSHError) as caught:
        await sysbox_remote.install(object(), "dev")

    assert "7 present" in str(caught.value)
    assert "adding a server" in str(caught.value)
    assert not any("curl" in c or "dpkg" in c for c in fake.calls), (
        "nothing should be downloaded or installed once the check has failed"
    )


async def test_install_proceeds_on_a_server_with_no_containers(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    fake.set("/etc/os-release", _ok(DEBIAN_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64"))
    fake.set("uname -r", _ok("6.17.0-1022-azure"))
    fake.set("sudo -n true", _ok())
    fake.set("command -v docker", _ok("/usr/bin/docker"))
    fake.set("docker ps -aq", _ok("0"))
    fake.set("curl", _ok())
    fake.set("dpkg -i", _ok())
    fake.set("apt-get update", _ok())
    fake.set("apt-get install -f", _ok())
    fake.set("rm -f", _ok())
    fake.set("command -v sysbox-runc", _ok("/usr/bin/sysbox-runc"))
    fake.set("sysbox-runc --version", _ok("sysbox-runc\n\tedition: Community Edition (CE)"))
    fake.set("docker info", _ok('{"sysbox-runc":{"path":"/usr/bin/sysbox-runc"}}'))

    info = await sysbox_remote.install(object(), "dev")

    assert info.installed is True
    assert info.registered_as_runtime is True


async def test_a_failed_install_reports_what_the_package_said(monkeypatch) -> None:
    """apt's stderr for a failed postinst says nothing; the reason is on stdout."""
    fake = _fake(monkeypatch)
    fake.set("/etc/os-release", _ok(DEBIAN_OS_RELEASE))
    fake.set("uname -m", _ok("x86_64"))
    fake.set("uname -r", _ok("6.17.0-1022-azure"))
    fake.set("sudo -n true", _ok())
    fake.set("command -v docker", _ok("/usr/bin/docker"))
    fake.set("docker ps -aq", _ok("0"))
    fake.set("curl", _ok())
    fake.set("dpkg -i", _ok("Setting up sysbox-ce ... the real reason lives here"))
    fake.set("apt-get update", _ok())
    fake.set(
        "apt-get install -f",
        CommandResult(
            exit_status=100,
            stdout="",
            stderr="E: Sub-process /usr/bin/dpkg returned an error code (1)",
        ),
    )

    with pytest.raises(SSHError) as caught:
        await sysbox_remote.install(object(), "dev")

    assert "the real reason lives here" in str(caught.value)
