"""Two people, one container, nothing shared that shouldn't be.

Sharing a project shares the machine and the code. It must not share the coding
subscription behind them: each session authenticates as its own owner, commits
as its own owner, and works in its own checkout.

That is a claim about files inside a real container, so this checks it there
rather than at the seams. Everything below runs against the fake server with
the host Docker socket mounted, which gives the same sibling-container topology
a real managed server has.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from moonphase import docker_remote, provision, sessions, ssh, workspaces
from moonphase import profile as profile_mod
from moonphase.harness import HarnessAuthMode, HarnessCredential, SessionSpace
from moonphase.harness import get as get_harness
from moonphase.profile import WorkspaceProfile
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 22722
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


def _profile(name: str, email: str, key: str) -> WorkspaceProfile:
    return WorkspaceProfile(
        org_id=str(uuid.uuid4()),
        claude_md=f"# {name}'s rules",
        env_vars={"WHOAMI": name},
        git_user_name=name,
        git_user_email=email,
        harness_credential=HarnessCredential(
            mode=HarnessAuthMode.API_KEY, api_key=key
        ),
    )


@pytest.fixture(scope="module")
def fake_server():
    name = f"moonphase-isolation-{uuid.uuid4().hex[:8]}"
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
async def test_two_sessions_share_nothing_they_should_not(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-iso-{uuid.uuid4().hex[:8]}"

    try:
        result = await provision.bootstrap(
            server_id=server_id,
            server_name="isolation-test",
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
            conn, name=container, image=RUNTIME_IMAGE,
            workspace_volume=f"{container}-workspace",
            home_volume=f"{container}-home",
        )
        await docker_remote.exec_capture(
            conn, container, ["chown", "-R", "dev:dev", "/home/dev", "/workspace"],
            user="root", timeout=120,
        )

        harness = get_harness("claude_code")
        people = {
            "alice": _profile("Alice", "alice@example.test", "sk-ant-alice-key"),
            "bob": _profile("Bob", "bob@example.test", "sk-ant-bob-key"),
        }

        spaces: dict[str, SessionSpace] = {}
        for name, prof in people.items():
            workdir, branch = await workspaces.ensure_worktree(
                conn, container, name,
                author_name=prof.git_user_name or name,
                author_email=prof.git_user_email or f"{name}@example.test",
            )
            space = sessions.space_for(name, workdir)
            spaces[name] = space
            assert branch == f"moonphase/{name}"
            await sessions.ensure_home(conn, container, space)
            await profile_mod.apply(conn, container, harness, prof, space)
        print("\n  two sessions provisioned in one container")

        # --- credentials -----------------------------------------------------
        for name, expected in (("alice", "sk-ant-alice-key"), ("bob", "sk-ant-bob-key")):
            env = await profile_mod.read_file(conn, container, spaces[name].env_file)
            assert env and expected in env, f"{name} did not get their own key"
            other = "sk-ant-bob-key" if name == "alice" else "sk-ant-alice-key"
            assert other not in env, f"{name}'s session can see the other's key"
        print("  each session holds only its own API key")

        # What the harness would actually see, sourced the way the launcher does.
        for name, expected in (("alice", "sk-ant-alice-key"), ("bob", "sk-ant-bob-key")):
            seen = await docker_remote.exec_capture(
                conn, container,
                ["sh", "-c",
                 f"set -a; . {spaces[name].env_file}; set +a; "
                 'printf "%s|%s|%s" "$ANTHROPIC_API_KEY" "$HOME" "$WHOAMI"'],
                timeout=30,
            )
            key, home, whoami = seen.stdout.strip().split("|")
            assert key == expected
            assert home == spaces[name].home
            assert whoami == name.capitalize()
        print("  and exports it with its own HOME")

        # --- harness state ----------------------------------------------------
        for name in people:
            md = await profile_mod.read_file(
                conn, container, f"{spaces[name].home}/.claude/CLAUDE.md"
            )
            assert md and name.capitalize() in md, f"{name} got the wrong CLAUDE.md"
        print("  and its own harness configuration")

        # --- git identity ------------------------------------------------------
        for name, expected in (("alice", "Alice"), ("bob", "Bob")):
            who = await docker_remote.exec_capture(
                conn, container,
                ["sh", "-c",
                 f"cd {spaces[name].workdir} && "
                 f"GIT_CONFIG_GLOBAL={spaces[name].git_config} git config user.name"],
                timeout=30,
            )
            assert who.stdout.strip() == expected, (
                f"commits from {name}'s session would be attributed to "
                f"{who.stdout.strip()!r}"
            )
        print("  commits are attributed to the person who made them")

        # --- working directories ------------------------------------------------
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", f"echo alice-only > {spaces['alice'].workdir}/notes.txt"],
            timeout=30,
        )
        leaked = await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", f"cat {spaces['bob'].workdir}/notes.txt 2>/dev/null || echo absent"],
            timeout=30,
        )
        assert leaked.stdout.strip() == "absent", (
            "one agent's edits landed in the other's checkout"
        )
        print("  and neither agent can overwrite the other's files")

        # Both are real worktrees of the one repository, on their own branches.
        listing = await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", "cd /workspace && git worktree list"],
            timeout=30,
        )
        for name in people:
            assert spaces[name].workdir in listing.stdout
            assert f"moonphase/{name}" in listing.stdout
        assert "/workspace " in listing.stdout or listing.stdout.startswith("/workspace")
        print("  both are worktrees of the same repository")

        # --- and the sessions actually run -------------------------------------
        for name in people:
            await sessions.ensure_session(
                conn, container,
                harness_kind="claude_code",
                workspace_profile=people[name],
                session=name,
                space=spaces[name],
            )
        live = await sessions.client_counts(conn, container)
        assert set(live) == {"alice", "bob"}, f"expected both sessions, saw {live}"
        print("  both tmux sessions are running side by side")

        # The launcher must have put each pane in its own checkout and home.
        for name in people:
            probe = await docker_remote.exec_capture(
                conn, container,
                ["tmux", "display-message", "-p", "-t", name,
                 "#{pane_current_path}"],
                timeout=30,
            )
            assert probe.stdout.strip() == spaces[name].workdir, (
                f"{name}'s pane started in {probe.stdout.strip()!r}"
            )
        print("  each pane started in its owner's worktree")

        # --- removing one leaves the other alone --------------------------------
        await sessions.kill_session(conn, container, "alice")
        await workspaces.remove_worktree(conn, container, "alice")

        gone = await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", f"test -d {spaces['alice'].workdir} && echo present || echo gone"],
            timeout=30,
        )
        assert gone.stdout.strip() == "gone"
        branches = await docker_remote.exec_capture(
            conn, container, ["sh", "-c", "cd /workspace && git branch --list"], timeout=30
        )
        assert "moonphase/alice" in branches.stdout, (
            "closing a session destroyed the only copy of its work"
        )
        still = await sessions.client_counts(conn, container)
        assert "bob" in still, "removing one session took the other down"
        print("  closing one keeps its branch, and leaves the other running")

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
