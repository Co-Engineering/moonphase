"""Getting agy's terminal sandbox, and so its Auto Mode, to start.

Auto Mode only runs inside agy's terminal sandbox, and on Linux that sandbox is
built on unprivileged user namespaces. Two things stand in the way on a
Moonphase server, and agy says neither: it just falls back to asking about
every command.

* The host. Ubuntu restricts user namespaces to programs an AppArmor profile
  names (see `userns_remote`). Lifted here, before the session starts, when
  the person starting it administers the server — the same call the server
  page offers, made at the moment it is needed instead of left for someone to
  find.
* The container. Docker's default seccomp filter refuses the namespaces
  outright in an ordinary container; under Sysbox — a project with Docker
  access — they work. A runtime is fixed when a container is created, so this
  can only say so; the project's Docker access switch recreates it.

Whether it worked is not inferred from either: the container is asked to
create a user namespace, as the session's user, which is what agy's sandbox
does first.
"""

from __future__ import annotations

import logging

import asyncssh

from . import docker_remote, userns_remote
from .ssh import SSHError

log = logging.getLogger(__name__)

# What the sandbox does first: a user namespace, mapped so the user is root
# inside it. Refused on the uid_map write under Ubuntu's restriction, and on
# the unshare itself under Docker's seccomp filter.
PROBE = ["unshare", "--user", "--map-root-user", "true"]

LIFTED = (
    "Allowed user namespaces on this server, so Antigravity's Auto Mode can run. "
    "You can put the restriction back on the server's page."
)


def _restricted_note(administers: bool, detail: str | None = None) -> str:
    if administers:
        return (
            "Auto Mode is unavailable: this server restricts user namespaces, which "
            "agy's sandbox needs, and Moonphase could not lift that"
            + (f" ({detail})" if detail else "")
            + ". agy will ask before each command."
        )
    return (
        "Auto Mode is unavailable: this server restricts user namespaces, which "
        "agy's sandbox needs. Its owner can allow them on the server's page; until "
        "then agy asks before each command."
    )


CONTAINER_NOTE = (
    "Auto Mode is unavailable in this project: its container cannot create user "
    "namespaces, which agy's sandbox needs. Turn on Docker access for the project "
    "(⋯ → Docker access) so it runs under Sysbox; until then agy asks before each "
    "command."
)


async def prepare(
    conn: asyncssh.SSHClientConnection,
    container: str,
    *,
    administers_server: bool,
) -> str | None:
    """Make the sandbox possible here if Moonphase can, and say why if not.

    Returns a note for the person starting the session, or None when the
    sandbox was already possible and nothing changed.
    """
    lifted = False
    try:
        info = await userns_remote.probe(conn)
    except SSHError as exc:
        log.info("agy sandbox: could not read the server's setting: %s", exc)
        info = None
    if info is not None and info.restricted:
        if not administers_server:
            return _restricted_note(False)
        try:
            info = await userns_remote.lift(conn)
        except SSHError as exc:
            return _restricted_note(True, str(exc))
        lifted = True

    result = await docker_remote.exec_capture(conn, container, PROBE, timeout=20)
    if not result.ok:
        missing = result.exit_status == 127 or "not found" in (result.stderr or "")
        if missing:
            # An environment without util-linux has no way to ask; agy will
            # find out for itself.
            return LIFTED if lifted else None
        log.info("agy sandbox: %s cannot create a user namespace: %s", container, result.stderr)
        if info is not None and info.restricted:
            return _restricted_note(administers_server)
        return CONTAINER_NOTE
    return LIFTED if lifted else None
