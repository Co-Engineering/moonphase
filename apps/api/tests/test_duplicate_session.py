"""Duplicating a session has to carry uncommitted work without touching the
original.

`workspaces.snapshot_worktree` captures a worktree's current state — staged,
unstaged and untracked files — as a commit object, without moving the source
session's HEAD, its index, or anything on disk. `ensure_worktree` then has to
accept that commit's raw SHA as a `start_point`, which it could not do before:
it only ever resolved `start_point` as a branch name.

Runs against the same fake-server-plus-real-container topology as
`test_session_isolation.py`, because both claims are about real git state
inside a real container, not something a mock can stand in for.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from moonphase import docker_remote, provision, ssh, workspaces
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
SSH_PORT = 22723
SSH_USER = "deploy"
SSH_PASSWORD = "moonphase-test"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _docker_available() -> bool:
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", check=False).returncode == 0
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon is not reachable"
)


@pytest.fixture(scope="module")
def fake_server():
    name = f"moonphase-dup-{uuid.uuid4().hex[:8]}"
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
async def test_a_snapshot_seeds_a_new_worktree_without_touching_the_source(
    fake_server: str,
) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-dup-{uuid.uuid4().hex[:8]}"

    try:
        result = await provision.bootstrap(
            server_id=server_id,
            server_name="duplicate-test",
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

        await docker_remote.volume_create(conn, f"{container}-workspace")
        await docker_remote.volume_create(conn, f"{container}-home")
        await docker_remote.run_container(
            conn, name=container, image="moonphase/runtime-claude:latest",
            workspace_volume=f"{container}-workspace",
            home_volume=f"{container}-home",
        )
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        source_workdir, _ = await workspaces.ensure_worktree(
            conn, container, "source",
            author_name="Ada", author_email="ada@example.test",
        )

        # A committed file, then dirty state on top of it: one staged edit,
        # one unstaged edit, and one file git has never seen.
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", (
                f"cd {source_workdir} && "
                "echo committed > tracked.txt && "
                "git add tracked.txt && "
                "git -c user.name=Ada -c user.email=ada@example.test "
                "commit -q -m 'tracked file' && "
                "echo staged-change >> tracked.txt && git add tracked.txt && "
                "echo new-file > untracked.txt"
            )],
            timeout=30,
        )
        before_status = await docker_remote.exec_capture(
            conn, container, ["sh", "-c", f"cd {source_workdir} && git status --porcelain"],
            timeout=30,
        )
        before_head = await docker_remote.exec_capture(
            conn, container, ["sh", "-c", f"cd {source_workdir} && git rev-parse HEAD"],
            timeout=30,
        )
        assert before_status.stdout.strip(), "the fixture should have left it dirty"

        snapshot_sha = await workspaces.snapshot_worktree(
            conn, container, source_workdir,
            author_name="Ada", author_email="ada@example.test",
        )

        # The source worktree must come back exactly as it was: same HEAD,
        # same staged/unstaged/untracked split, nothing extra on disk.
        after_status = await docker_remote.exec_capture(
            conn, container, ["sh", "-c", f"cd {source_workdir} && git status --porcelain"],
            timeout=30,
        )
        after_head = await docker_remote.exec_capture(
            conn, container, ["sh", "-c", f"cd {source_workdir} && git rev-parse HEAD"],
            timeout=30,
        )
        assert after_status.stdout == before_status.stdout, (
            "snapshotting changed the source session's own git status"
        )
        assert after_head.stdout == before_head.stdout, (
            "snapshotting moved the source session's HEAD"
        )

        # The snapshot itself must be usable as a start_point — this is the
        # ensure_worktree fix: it previously only accepted a branch name.
        new_workdir, new_branch = await workspaces.ensure_worktree(
            conn, container, "duplicate",
            author_name="Ada", author_email="ada@example.test",
            start_point=snapshot_sha,
        )
        assert new_branch == "moonphase/duplicate"

        new_tracked = await docker_remote.exec_capture(
            conn, container, ["cat", f"{new_workdir}/tracked.txt"], timeout=30,
        )
        assert new_tracked.stdout == "committed\nstaged-change\n"
        new_untracked = await docker_remote.exec_capture(
            conn, container, ["cat", f"{new_workdir}/untracked.txt"], timeout=30,
        )
        assert new_untracked.stdout == "new-file\n"

        # Everything landed as committed history in the new worktree — a
        # duplicate is a starting point of its own, not still-uncommitted work.
        new_status = await docker_remote.exec_capture(
            conn, container, ["sh", "-c", f"cd {new_workdir} && git status --porcelain"],
            timeout=30,
        )
        assert new_status.stdout == "", "the duplicate should start with a clean tree"

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


@pytest.mark.asyncio(loop_scope="module")
async def test_a_clean_worktree_snapshots_to_its_own_head(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-dup-clean-{uuid.uuid4().hex[:8]}"

    try:
        result = await provision.bootstrap(
            server_id=server_id,
            server_name="duplicate-clean-test",
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

        await docker_remote.volume_create(conn, f"{container}-workspace")
        await docker_remote.volume_create(conn, f"{container}-home")
        await docker_remote.run_container(
            conn, name=container, image="moonphase/runtime-claude:latest",
            workspace_volume=f"{container}-workspace",
            home_volume=f"{container}-home",
        )
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        workdir, _ = await workspaces.ensure_worktree(
            conn, container, "clean",
            author_name="Ada", author_email="ada@example.test",
        )
        head = await docker_remote.exec_capture(
            conn, container, ["sh", "-c", f"cd {workdir} && git rev-parse HEAD"], timeout=30,
        )

        snapshot_sha = await workspaces.snapshot_worktree(
            conn, container, workdir, author_name="Ada", author_email="ada@example.test",
        )
        assert snapshot_sha == head.stdout.strip(), (
            "a clean worktree should snapshot to its own HEAD, not a new commit"
        )

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
