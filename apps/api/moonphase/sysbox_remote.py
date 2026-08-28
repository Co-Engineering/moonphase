"""Sysbox runtime control on a managed server, driven over SSH.

Sysbox (`sysbox-runc`) lets an *unprivileged* container run Docker and other
nested containers safely, through per-container user-namespace
virtualization — the alternative to `--privileged` or mounting the host's
`docker.sock`, both of which hand a project's container a path to the host
itself. It is a host-level dependency: a package installed on the managed
server and registered as a Docker runtime in `/etc/docker/daemon.json`,
alongside Docker.

Mirrors docker_remote.py's shape deliberately: probe compatibility, probe
current state, install. Requires Docker to already be installed and usable —
callers must run `ensure_docker()` first.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass

import asyncssh

from . import ssh
from .ssh import SSHError

log = logging.getLogger(__name__)

# Pinned deliberately rather than resolved against "latest": an install
# target that can change under us is not reproducible and not rollback-safe.
# Bump by hand, and re-verify the asset name below against
# https://github.com/nestybox/sysbox/releases at the same time — Sysbox has
# no get.docker.com-style convenience script.
#
# Verified against the real releases on 2026-08-28: v0.7.1 is current, and
# every release from 0.6.7 through 0.7.1 publishes exactly two assets,
# `sysbox-ce_<version>.linux_amd64.deb` and `…_arm64.deb`.
SYSBOX_VERSION = "0.7.1"

# Ubuntu/Debian ID-mapped mounts (a mainline kernel feature) work without
# shiftfs from this version on; below it, Sysbox falls back to the
# Ubuntu-only, out-of-tree shiftfs module. Re-verify against Sysbox's current
# distro-compatibility docs at implementation time — this threshold has
# moved before and this sandbox has no compatible kernel to test against.
MIN_IDMAP_KERNEL = (5, 19)

_SUPPORTED_OS = {"ubuntu", "debian"}
_ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64"}


@dataclass
class SysboxCompatibility:
    """What the host would need for Sysbox to work at all.

    Checked before every install attempt so an incompatible host is refused
    with a clear reason, rather than failing deep inside a half-installed
    package.
    """

    compatible: bool
    os_id: str | None = None
    arch: str | None = None
    kernel_version: str | None = None
    kernel_supports_idmap: bool = False
    shiftfs_available: bool = False
    detail: str | None = None


@dataclass
class SysboxInfo:
    installed: bool
    registered_as_runtime: bool = False
    version: str | None = None
    detail: str | None = None


def _parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def _kernel_at_least(kernel_release: str, minimum: tuple[int, int]) -> bool:
    match = re.match(r"^(\d+)\.(\d+)", kernel_release)
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) >= minimum


async def probe_compatibility(conn: asyncssh.SSHClientConnection) -> SysboxCompatibility:
    """Check what Sysbox actually requires, before attempting anything."""
    os_release = await ssh.run(conn, "cat /etc/os-release", timeout=15)
    fields = _parse_os_release(os_release.stdout if os_release.ok else "")
    os_id = fields.get("ID")

    arch_raw = (await ssh.run(conn, "uname -m", timeout=15)).stdout.strip()
    arch = _ARCH_MAP.get(arch_raw, arch_raw or None)

    kernel_raw = (await ssh.run(conn, "uname -r", timeout=15)).stdout.strip()
    kernel_supports_idmap = _kernel_at_least(kernel_raw, MIN_IDMAP_KERNEL)

    # Only worth checking on Ubuntu, and only when the modern path is not
    # already available — a stock Debian kernel never ships this module.
    shiftfs_available = False
    if os_id == "ubuntu" and not kernel_supports_idmap:
        shiftfs_available = (await ssh.run(conn, "modinfo shiftfs", timeout=15)).ok

    reasons: list[str] = []
    if os_id not in _SUPPORTED_OS:
        reasons.append(f"host OS is {os_id or 'unknown'}; Sysbox supports Ubuntu or Debian")
    if arch not in ("amd64", "arm64"):
        reasons.append(f"architecture {arch or arch_raw!r} is not amd64 or arm64")
    if not kernel_supports_idmap and not shiftfs_available:
        reasons.append(
            f"kernel {kernel_raw or 'unknown'} lacks ID-mapped mounts (needs "
            f">= {'.'.join(map(str, MIN_IDMAP_KERNEL))}) and shiftfs is "
            "unavailable (shiftfs is an Ubuntu-only fallback for older kernels)"
        )

    return SysboxCompatibility(
        compatible=not reasons,
        os_id=os_id,
        arch=arch,
        kernel_version=kernel_raw or None,
        kernel_supports_idmap=kernel_supports_idmap,
        shiftfs_available=shiftfs_available,
        detail="; ".join(reasons) or None,
    )


async def probe(conn: asyncssh.SSHClientConnection) -> SysboxInfo:
    """Determine whether sysbox-runc exists and is registered with the daemon."""
    which = await ssh.run(conn, "command -v sysbox-runc", timeout=15)
    if not which.ok:
        return SysboxInfo(installed=False, detail="`sysbox-runc` is not on PATH.")

    version = await ssh.run(conn, "sysbox-runc --version", timeout=15)
    version_str = version.stdout.strip().splitlines()[0] if version.ok else None

    # Reflects what the *running* daemon actually knows, not just what
    # daemon.json says on disk — the file can be edited without docker having
    # been restarted to pick it up.
    runtimes = await ssh.run(conn, "docker info --format '{{json .Runtimes}}'", timeout=25)
    registered = runtimes.ok and "sysbox-runc" in runtimes.stdout

    return SysboxInfo(
        installed=True,
        registered_as_runtime=registered,
        version=version_str,
        detail=None if registered else (
            "sysbox-runc is installed but not registered as a Docker runtime. "
            "Check /etc/docker/daemon.json and that docker was restarted."
        ),
    )


async def install(conn: asyncssh.SSHClientConnection, ssh_user: str) -> SysboxInfo:
    """Install Sysbox from the sysbox-ce .deb, then confirm it registered.

    Requires passwordless sudo (same requirement as docker_remote.install)
    and Docker already installed — Sysbox's package registers itself as a
    Docker runtime and needs the docker.service unit to already exist.
    """
    compat = await probe_compatibility(conn)
    if not compat.compatible:
        return SysboxInfo(installed=False, detail=compat.detail)

    sudo_check = await ssh.run(conn, "sudo -n true", timeout=15)
    if not sudo_check.ok:
        raise SSHError(
            "Sysbox requires passwordless sudo to install, and this user does "
            "not have it. Install Sysbox manually, then retry."
        )

    docker_check = await ssh.run(conn, "command -v docker", timeout=15)
    if not docker_check.ok:
        raise SSHError("Docker must be installed before Sysbox.")

    # Community Edition asset naming, checked against the release assets
    # themselves rather than the releases page's prose: there is no `-0`
    # revision suffix. Getting this wrong is silent until the install runs —
    # the URL simply 404s, and a 404 from `curl -f` on a fresh server reads
    # like a network problem rather than a name we made up.
    filename = f"sysbox-ce_{SYSBOX_VERSION}.linux_{compat.arch}.deb"
    url = f"https://github.com/nestybox/sysbox/releases/download/v{SYSBOX_VERSION}/{filename}"

    log.info("installing sysbox on remote host (%s)", filename)
    download = await ssh.run(
        conn, f"curl -fsSL -o /tmp/{filename} {shlex.quote(url)}", timeout=120
    )
    download.check("Downloading the Sysbox package")

    # dpkg -i is expected to report missing dependencies; apt-get install -f
    # resolves them from the already-configured apt sources. This is the
    # documented two-step install for a standalone .deb, and it's what the
    # package's own postinst script relies on running to register
    # sysbox-runc in /etc/docker/daemon.json's "runtimes" stanza, start
    # sysbox-mgr/sysbox-fs, and restart docker — none of that is done
    # manually here.
    await ssh.run(conn, f"sudo -n dpkg -i /tmp/{filename}", timeout=120)
    update = await ssh.run(conn, "sudo -n apt-get update -qq", timeout=120)
    update.check("Updating apt package lists")
    fix_deps = await ssh.run(conn, "sudo -n apt-get install -f -y -qq", timeout=300)
    fix_deps.check("Installing Sysbox")

    await ssh.run(conn, f"rm -f /tmp/{filename}", timeout=15)

    # Trust the re-probe, not the install script's exit code: the failure
    # mode worth catching is "dpkg succeeded but didn't actually register".
    info = await probe(conn)
    if not info.installed:
        return SysboxInfo(
            installed=False,
            detail="Sysbox package installed but `sysbox-runc` is still not on PATH.",
        )
    return info
