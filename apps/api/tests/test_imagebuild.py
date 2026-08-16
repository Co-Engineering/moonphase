"""Environment image recipes.

An environment is a definition, not a published image, so these check the two
things that follow from that: the generated recipe really produces a container
Moonphase can drive, and a changed definition produces a different tag rather
than silently reusing a stale build.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from moonphase import docker_remote, environments, imagebuild, provision, sessions, ssh
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
SSH_PORT = 22822
SSH_USER = "deploy"
SSH_PASSWORD = "moonphase-test"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _docker_available() -> bool:
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", check=False).returncode == 0
    except FileNotFoundError:
        return False


# --- pure, no Docker needed -------------------------------------------------


def test_recipe_installs_everything_moonphase_depends_on() -> None:
    recipe = imagebuild.recipe_for("debian:bookworm-slim", None)
    # Losing any of these turns into a symptom far from its cause: no session,
    # no port detection, no previews.
    for package in ("tmux", "socat", "iproute2", "git"):
        assert package in recipe, f"{package} missing from the recipe"
    assert "@anthropic-ai/claude-code" in recipe
    assert "FROM debian:bookworm-slim" in recipe


def test_recipe_rejects_a_base_without_apt() -> None:
    recipe = imagebuild.recipe_for("alpine:3.20", None)
    # The build must fail with an explanation rather than producing a container
    # that starts but cannot hold a session.
    assert "command -v apt-get" in recipe
    assert "Debian or Ubuntu family" in recipe


def test_setup_script_survives_multiline_shell_verbatim() -> None:
    """Folding a script into one RUN corrupts multi-line constructs.

    `if x; then` on separate lines becomes `if x; then;` when joined with
    semicolons, which is a syntax error. Encoding sidesteps the whole class.
    """
    import base64
    import re

    script = "apt-get update\nif [ -d /opt ]; then\n  echo present\nfi"
    recipe = imagebuild.recipe_for("debian:bookworm-slim", script)
    match = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d > /tmp/moonphase-setup", recipe)
    assert match, "setup script was not encoded into the recipe"
    assert base64.b64decode(match.group(1)).decode() == script


def test_tag_changes_when_the_definition_changes() -> None:
    a = imagebuild.image_tag("web", "debian:bookworm-slim", None)
    b = imagebuild.image_tag("web", "ubuntu:24.04", None)
    c = imagebuild.image_tag("web", "debian:bookworm-slim", "apt-get install -y nmap")
    assert a != b, "a different base must not reuse the same image"
    assert a != c, "different setup commands must not reuse the same image"
    assert a == imagebuild.image_tag("web", "debian:bookworm-slim", None)


def test_tag_is_a_valid_docker_reference() -> None:
    tag = imagebuild.image_tag("My Weird Name!", "debian:bookworm-slim", None)
    name, _, version = tag.partition(":")
    assert name.islower() and " " not in name and "!" not in name
    assert version and version.isalnum()


def test_custom_environment_shadows_a_builtin() -> None:
    rows = [
        {
            "key": "debian",
            "display_name": "Debian (pinned)",
            "description": "",
            "base_image": "debian:bullseye-slim",
            "setup_script": None,
        }
    ]
    resolved = environments.resolve("debian", rows)
    assert resolved.base_image == "debian:bullseye-slim"
    assert resolved.builtin is False
    # And it replaces rather than duplicating the built-in.
    assert len([e for e in environments.merge(rows) if e.key == "debian"]) == 1


def test_unknown_environment_falls_back_to_the_default() -> None:
    # A project whose environment was deleted must still be openable.
    assert environments.resolve("deleted-one", []).key == environments.DEFAULT_ENVIRONMENT


# --- against a real server --------------------------------------------------

pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon is not reachable"
)


@pytest.fixture(scope="module")
def fake_server():
    name = f"moonphase-imgbuild-{uuid.uuid4().hex[:8]}"
    _docker("rm", "-f", name, check=False)
    _docker(
        "run", "-d", "--name", name,
        "-p", f"127.0.0.1:{SSH_PORT}:22",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        FAKE_SERVER_IMAGE,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        logs = _docker("logs", name, check=False)
        if "Server listening on 0.0.0.0" in (logs.stdout + logs.stderr):
            break
        time.sleep(0.4)
    else:
        _docker("rm", "-f", name, check=False)
        pytest.fail("fake server never started")
    yield name
    _docker("rm", "-f", name, check=False)


@pytest.mark.asyncio(loop_scope="module")
async def test_builds_a_user_defined_environment_on_the_server(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-env-{uuid.uuid4().hex[:8]}"

    # What a user would type: a base image and a package they want available.
    env = environments.from_row(
        {
            "key": "with-postgres",
            "display_name": "Debian + psql",
            "description": "",
            "base_image": "debian:bookworm-slim",
            "setup_script": (
                "apt-get update\n"
                "apt-get install -y --no-install-recommends postgresql-client\n"
                "rm -rf /var/lib/apt/lists/*"
            ),
        }
    )

    try:
        result = await provision.bootstrap(
            server_id=server_id,
            server_name="imgbuild-test",
            host="127.0.0.1",
            port=SSH_PORT,
            ssh_user=SSH_USER,
            auth_mode="password_bootstrap",
            password=SSH_PASSWORD,
            auto_install_docker=False,
        )
        assert result.status == "online", result.detail

        target = SSHTarget(
            server_id=server_id, host="127.0.0.1", port=SSH_PORT, username=SSH_USER,
            private_key=result.generated_private_key,
            known_host_key_fp=result.host_key_fingerprint,
        )
        conn = await ssh.pool.get(target)

        # Image tags are a deterministic hash of the definition, so a previous
        # run leaves this one present and "did it build?" would be vacuously
        # false. Remove it so the first-build assertion means something.
        await ssh.run(conn, f"docker rmi -f {env.image}", timeout=120)

        built = await imagebuild.ensure_image(
            conn,
            tag=env.image,
            base_image=env.base_image,
            setup_script=env.setup_script,
        )
        assert built is True, "expected a build on first use"
        print(f"\n  built {env.image} on the server")

        # Second call must be a no-op; rebuilding on every project would make
        # creation take minutes forever.
        again = await imagebuild.ensure_image(
            conn,
            tag=env.image,
            base_image=env.base_image,
            setup_script=env.setup_script,
        )
        assert again is False
        print("  second use reused the image")

        # And the result must actually be drivable by Moonphase.
        await docker_remote.volume_create(conn, f"{container}-workspace")
        await docker_remote.volume_create(conn, f"{container}-home")
        await docker_remote.run_container(
            conn, name=container, image=env.image,
            workspace_volume=f"{container}-workspace",
            home_volume=f"{container}-home",
        )
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        for tool in ("tmux", "socat", "ss", "claude", "psql"):
            probe = await docker_remote.exec_capture(
                conn, container, ["sh", "-c", f"command -v {tool}"], timeout=30
            )
            assert probe.ok, f"{tool} missing from the built image"
        print("  image has tmux, socat, ss, claude and the user's psql")

        created = await sessions.ensure_session(
            conn, container, harness_kind="claude_code"
        )
        assert created is True
        assert await sessions.session_exists(conn, container)
        print("  a tmux session starts in the custom environment")

    finally:
        try:
            cleanup = SSHTarget(
                server_id=server_id, host="127.0.0.1", port=SSH_PORT,
                username=SSH_USER, password=SSH_PASSWORD,
            )
            conn_c, _ = await ssh.connect(cleanup)
            await docker_remote.remove(conn_c, container)
            await docker_remote.volume_remove(conn_c, f"{container}-workspace")
            await docker_remote.volume_remove(conn_c, f"{container}-home")
            conn_c.close()
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask failures
            print(f"  cleanup warning: {exc}")
        await ssh.pool.close_all()
