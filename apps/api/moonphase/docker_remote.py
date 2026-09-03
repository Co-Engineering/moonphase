"""Docker control on a managed server, driven over SSH.

Deliberately shells out to the `docker` CLI rather than using DOCKER_HOST or the
Python SDK. The CLI is already on the box, needs no daemon socket forwarding,
and lets the SSH layer keep full control of credentials (which never touch disk
in the API container). The cost is quoting discipline — hence `shlex.quote` on
every interpolated value below.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass

import asyncssh

from . import ssh
from .ssh import CommandResult, SSHError

log = logging.getLogger(__name__)


@dataclass
class DockerInfo:
    installed: bool
    server_version: str | None = None
    usable_by_user: bool = False
    detail: str | None = None


@dataclass
class ContainerInfo:
    id: str
    name: str
    state: str
    status: str
    # The image this container was created from. A container keeps it for
    # life — there is no changing it without recreating — so comparing it
    # against the environment's current image is how a stale container is
    # recognised. See _recreate_if_stale in routers/projects.py.
    image: str = ""


async def probe(conn: asyncssh.SSHClientConnection) -> DockerInfo:
    """Determine whether Docker exists and whether our SSH user may drive it."""
    which = await ssh.run(conn, "command -v docker", timeout=15)
    if not which.ok:
        return DockerInfo(installed=False, detail="`docker` is not on PATH.")

    version = await ssh.run(
        conn, "docker version --format '{{.Server.Version}}'", timeout=25
    )
    if version.ok:
        return DockerInfo(
            installed=True,
            server_version=version.stdout.strip() or None,
            usable_by_user=True,
        )

    stderr = version.stderr.lower()
    if "permission denied" in stderr or "dial unix" in stderr:
        return DockerInfo(
            installed=True,
            usable_by_user=False,
            detail=(
                "Docker is installed but this user cannot reach the daemon socket. "
                "Add the user to the `docker` group and reconnect."
            ),
        )
    return DockerInfo(
        installed=True,
        usable_by_user=False,
        detail=(version.stderr or version.stdout).strip()[:400] or "Docker daemon unreachable.",
    )


async def install(conn: asyncssh.SSHClientConnection, ssh_user: str) -> DockerInfo:
    """Install Docker via the official convenience script, then grant group access.

    Requires passwordless sudo. This is the one place Moonphase needs elevated
    rights on a managed server; everything afterwards runs as the plain user.
    """
    sudo_check = await ssh.run(conn, "sudo -n true", timeout=15)
    if not sudo_check.ok:
        raise SSHError(
            "Docker is not installed and this user does not have passwordless sudo, "
            "so Moonphase cannot install it. Install Docker manually, then retry."
        )

    log.info("installing docker on remote host")
    script = await ssh.run(
        conn,
        "curl -fsSL https://get.docker.com -o /tmp/moonphase-get-docker.sh",
        timeout=120,
    )
    script.check("Downloading the Docker install script")

    run_script = await ssh.run(conn, "sudo -n sh /tmp/moonphase-get-docker.sh", timeout=600)
    run_script.check("Installing Docker")

    await ssh.run(conn, f"sudo -n usermod -aG docker {shlex.quote(ssh_user)}", timeout=30)
    await ssh.run(conn, "sudo -n systemctl enable --now docker", timeout=60)
    await ssh.run(conn, "rm -f /tmp/moonphase-get-docker.sh", timeout=15)

    # Group membership only applies to new sessions, so drop the pooled
    # connection and let the caller reconnect before probing again.
    return DockerInfo(
        installed=True,
        usable_by_user=False,
        detail="Docker installed. Reconnecting to pick up the new `docker` group membership.",
    )


async def volume_create(conn: asyncssh.SSHClientConnection, name: str) -> None:
    result = await ssh.run(conn, f"docker volume create {shlex.quote(name)}", timeout=30)
    result.check(f"Creating volume {name}")


async def volume_remove(conn: asyncssh.SSHClientConnection, name: str) -> None:
    await ssh.run(conn, f"docker volume rm -f {shlex.quote(name)}", timeout=30)


async def image_present(conn: asyncssh.SSHClientConnection, image: str) -> bool:
    result = await ssh.run(
        conn, f"docker image inspect {shlex.quote(image)} --format '{{{{.Id}}}}'", timeout=30
    )
    return result.ok


async def pull(conn: asyncssh.SSHClientConnection, image: str) -> CommandResult:
    return await ssh.run(conn, f"docker pull {shlex.quote(image)}", timeout=900)


async def inspect(conn: asyncssh.SSHClientConnection, name: str) -> ContainerInfo | None:
    """Container details, or None when it does not exist.

    Deliberately avoids `.State.Health`: for a container with no healthcheck —
    which is all of ours — `.State` is a map with no such key and the whole
    template errors out rather than rendering empty.
    """
    result = await ssh.run(
        conn,
        "docker inspect --format "
        "'{{.Id}}|{{.Name}}|{{.State.Status}}|{{.State.StartedAt}}|{{.Config.Image}}' "
        + shlex.quote(name),
        timeout=30,
    )
    if not result.ok:
        return None
    parts = result.stdout.strip().split("|")
    if len(parts) < 3:
        return None
    return ContainerInfo(
        id=parts[0],
        name=parts[1].lstrip("/"),
        state=parts[2],
        status=parts[3] if len(parts) > 3 else parts[2],
        # Absent from an older daemon's output, or from a caller that
        # stubbed the format: an empty image means "unknown", and the
        # staleness check treats unknown as "leave it alone" rather than
        # recreating a container it cannot actually judge.
        image=parts[4] if len(parts) > 4 else "",
    )


async def run_container(
    conn: asyncssh.SSHClientConnection,
    *,
    name: str,
    image: str,
    workspace_volume: str,
    home_volume: str,
    env: dict[str, str] | None = None,
    published_ports: dict[int, int] | None = None,
    cpus: str | None = None,
    memory: str | None = None,
    runtime: str | None = None,
) -> str:
    """Start the long-lived project container and return its id.

    The container does nothing on its own — `tini -g -- sleep infinity` just
    keeps the namespace alive so tmux sessions inside it survive independently
    of any client. Restart policy brings it back after a host reboot.

    `runtime` is Docker's `--runtime` flag — "sysbox-runc" for a project with
    Docker access turned on, otherwise the daemon's own default. It is a
    creation-time property; there is no "change a running container's
    runtime", and this is the only call site that creates one.
    """
    args = [
        "docker", "run", "-d",
        "--name", name,
        "--restart", "unless-stopped",
        "--hostname", name,
        "--label", "moonphase=1",
        "--label", f"moonphase.project={name}",
        "-v", f"{workspace_volume}:/workspace",
        "-v", f"{home_volume}:/home/dev",
        "-w", "/workspace",
    ]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    for host_port, container_port in (published_ports or {}).items():
        # Bind to loopback only: the backend reaches these through the SSH
        # tunnel, and nothing should be exposed on the public interface.
        args += ["-p", f"127.0.0.1:{host_port}:{container_port}"]
    if cpus:
        args += ["--cpus", cpus]
    if memory:
        args += ["--memory", memory]
    if runtime:
        # Sysbox documents that --privileged and extra --cap-add/
        # --security-opt entries must not be combined with
        # --runtime=sysbox-runc — the runtime itself grants what a nested
        # Docker workload needs via per-container user-namespace
        # virtualization, and stacking --privileged on top defeats the
        # isolation Sysbox exists to provide. Never add a
        # --cap-add/--privileged parameter to this function alongside it.
        args += ["--runtime", runtime]
    args += [image, "sleep", "infinity"]

    command = " ".join(shlex.quote(a) for a in args)
    result = await ssh.run(conn, command, timeout=300)
    result.check(f"Starting container {name}")
    return result.stdout.strip()


async def start(conn: asyncssh.SSHClientConnection, name: str) -> None:
    result = await ssh.run(conn, f"docker start {shlex.quote(name)}", timeout=60)
    result.check(f"Starting container {name}")


async def stop(conn: asyncssh.SSHClientConnection, name: str) -> None:
    await ssh.run(conn, f"docker stop -t 10 {shlex.quote(name)}", timeout=60)


async def remove(conn: asyncssh.SSHClientConnection, name: str) -> None:
    await ssh.run(conn, f"docker rm -f {shlex.quote(name)}", timeout=60)


async def exec_capture(
    conn: asyncssh.SSHClientConnection,
    container: str,
    command: list[str],
    *,
    user: str = "dev",
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> CommandResult:
    """Run a command inside the container and capture its output."""
    args = ["docker", "exec", "-u", user]
    if workdir:
        args += ["-w", workdir]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    args += [container, *command]
    return await ssh.run(conn, " ".join(shlex.quote(a) for a in args), timeout=timeout)


async def copy_path(
    conn: asyncssh.SSHClientConnection,
    container: str,
    src: str,
    dst: str,
    *,
    timeout: float = 60.0,
) -> None:
    """Copy `src` to `dst`, both inside `container`.

    A no-op, not an error, when `src` does not exist — a session that has not
    written anything at that path yet (no transcript, say) is not a failure.

    `-a` rather than `-r`: it preserves modification times, which matters when
    `src` holds more than one file, since whatever later decides which one is
    "the newest" needs that ordering to survive the copy.
    """
    parent = shlex.quote(dst.rsplit("/", 1)[0])
    quoted_src = shlex.quote(src)
    quoted_dst = shlex.quote(dst)
    script = (
        f"[ -e {quoted_src} ] || exit 0; mkdir -p {parent} && cp -a {quoted_src} {quoted_dst}"
    )
    result = await exec_capture(conn, container, ["sh", "-c", script], timeout=timeout)
    result.check(f"Copying {src} to {dst} inside {container}")


def exec_tty_command(
    container: str,
    command: list[str],
    *,
    user: str = "dev",
    workdir: str | None = None,
) -> str:
    """Build the `docker exec -it` string used to attach an interactive PTY.

    Returned as a string rather than executed, because the caller runs it on an
    SSH channel it owns with a PTY already requested.
    """
    args = ["docker", "exec", "-it", "-u", user]
    if workdir:
        args += ["-w", workdir]
    args += [container, *command]
    return " ".join(shlex.quote(a) for a in args)


async def list_moonphase_containers(
    conn: asyncssh.SSHClientConnection,
) -> list[ContainerInfo]:
    result = await ssh.run(
        conn,
        "docker ps -a --filter label=moonphase=1 --format "
        "'{\"id\":\"{{.ID}}\",\"name\":\"{{.Names}}\",\"state\":\"{{.State}}\",\"status\":\"{{.Status}}\"}'",
        timeout=30,
    )
    if not result.ok:
        return []
    out: list[ContainerInfo] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(
            ContainerInfo(
                id=row.get("id", ""),
                name=row.get("name", ""),
                state=row.get("state", ""),
                status=row.get("status", ""),
            )
        )
    return out
