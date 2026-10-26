"""Unprivileged user namespaces on a managed server, driven over SSH.

Antigravity's Auto Mode runs commands in agy's terminal sandbox, and on Linux
that sandbox is built on unprivileged user namespaces. Ubuntu, from 23.10 on,
restricts creating one to programs an AppArmor profile allows
(`kernel.apparmor_restrict_unprivileged_userns = 1`); a project container's
processes have no such profile, so agy reports the sandbox unavailable and
falls back from Auto Mode to asking about every command.

Only the host can change that. The setting is the kernel's and is not
namespaced: a container can at most hide it from whoever reads the file, which
is what an earlier version of this did, and then agy reads that the sandbox
will work, while the kernel goes on refusing the namespaces it is built from.
So this lifts the restriction on the server itself, for its whole kernel —
the default on Debian, Fedora and most other distributions, and a hardening
Ubuntu added on top — and only when the server's owner asks. It is written to
`/etc/sysctl.d` so a reboot keeps it, and undone by removing that file.

Requires passwordless sudo, the same as installing Docker or Sysbox.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

import asyncssh

from . import ssh
from .ssh import SSHError

SYSCTL = "kernel.apparmor_restrict_unprivileged_userns"
PROC_PATH = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
# After the distribution's own files (Ubuntu's is numbered 10), so a reboot
# applies this one last.
CONF_PATH = "/etc/sysctl.d/60-moonphase-userns.conf"
CONF = (
    "# Written by Moonphase: lets programs in project containers create user\n"
    "# namespaces, which Antigravity's terminal sandbox (Auto Mode) is built on.\n"
    "# Delete this file, or turn it off from the server's page, to undo it.\n"
    f"{SYSCTL} = 0\n"
)


@dataclass
class UsernsInfo:
    # True while the kernel restricts them; None on a kernel without the
    # setting at all, where there is nothing to lift.
    restricted: bool | None
    # Whether Moonphase's file is in place, i.e. it lifted the restriction.
    lifted_by_moonphase: bool


async def probe(conn: asyncssh.SSHClientConnection) -> UsernsInfo:
    result = await ssh.run(
        conn,
        f"cat {PROC_PATH} 2>/dev/null; echo; "
        f"if [ -e {CONF_PATH} ]; then echo lifted; fi",
        timeout=15,
    )
    lines = [line.strip() for line in result.stdout.splitlines()]
    value = lines[0] if lines else ""
    return UsernsInfo(
        restricted=None if value == "" else value != "0",
        lifted_by_moonphase="lifted" in lines[1:],
    )


async def _require_sudo(conn: asyncssh.SSHClientConnection) -> None:
    if not (await ssh.run(conn, "sudo -n true", timeout=15)).ok:
        raise SSHError(
            "This server does not have passwordless sudo, so Moonphase cannot "
            f"change {SYSCTL}. The same access Docker installation needs would "
            "let it — or set it yourself."
        )


async def lift(conn: asyncssh.SSHClientConnection) -> UsernsInfo:
    """Allow user namespaces on the server, now and after every reboot."""
    await _require_sudo(conn)
    result = await ssh.run(
        conn,
        f"sudo -n tee {CONF_PATH} >/dev/null && sudo -n sysctl -q -w {SYSCTL}=0",
        stdin=CONF,
        timeout=30,
    )
    result.check(f"Setting {SYSCTL}")
    return await probe(conn)


async def restore(conn: asyncssh.SSHClientConnection) -> UsernsInfo:
    """Put the distribution's restriction back."""
    await _require_sudo(conn)
    result = await ssh.run(
        conn,
        f"sudo -n rm -f {shlex.quote(CONF_PATH)} && sudo -n sysctl -q -w {SYSCTL}=1",
        timeout=30,
    )
    result.check(f"Setting {SYSCTL}")
    return await probe(conn)
