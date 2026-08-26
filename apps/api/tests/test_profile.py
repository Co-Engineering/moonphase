"""Global profile materialisation.

The product claim is that you sign in and configure once, and every project
gets it. These tests check that against a real container rather than trusting
the plumbing: settings files land where the harness reads them, credentials are
0600, git is configured to use the token, and disconnecting actually removes
what was written.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid

import pytest

from moonphase import docker_remote, provision, sessions, ssh
from moonphase import profile as profile_mod
from moonphase.harness import HarnessAuthMode, HarnessCredential, SessionSpace
from moonphase.harness import get as get_harness
from moonphase.profile import VcsCredential, WorkspaceProfile
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 22622
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


# The shared, single-user layout: what a session with no owner used to get,
# and still what `apply` writes when no space is given.
SPACE = SessionSpace()

@pytest.fixture(scope="module")
def fake_server():
    name = f"moonphase-profile-{uuid.uuid4().hex[:8]}"
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
async def test_profile_reaches_the_container(fake_server: str) -> None:
    server_id = str(uuid.uuid4())
    container = f"mp-profile-{uuid.uuid4().hex[:8]}"

    try:
        result = await provision.bootstrap(
            server_id=server_id,
            server_name="profile-test",
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
        wp = WorkspaceProfile(
            org_id=str(uuid.uuid4()),
            claude_md="# Global preferences\nPrefer small commits.\n",
            claude_settings_json='{"permissions":{"allow":["Bash(npm test)"]}}',
            mcp_json='{"mcpServers":{}}',
            env_vars={"MOONPHASE_TEST_VAR": "hello-from-profile"},
            git_user_name="Ada Lovelace",
            git_user_email="ada@example.com",
            harness_credential=HarnessCredential(
                mode=HarnessAuthMode.API_KEY, api_key="sk-ant-profile-not-real"
            ),
            vcs_credential=VcsCredential(
                provider="github", token="ghp_fake_token_for_test", account="ada"
            ),
        )

        # What a real session start does first: the harness seeds its own
        # state file, and `apply` merges into it rather than replacing it.
        # The assertion below is about that state surviving — which needs
        # it to exist, and nothing here had created it.
        for path, contents in harness.seed_config_files(SPACE).items():
            await profile_mod.write_file(conn, container, path, contents, mode="600")

        await profile_mod.apply(conn, container, harness, wp)

        # --- harness configuration -----------------------------------------
        claude_md = await profile_mod.read_file(
            conn, container, "/home/dev/.claude/CLAUDE.md"
        )
        assert claude_md and "Prefer small commits" in claude_md

        settings = await profile_mod.read_file(
            conn, container, "/home/dev/.claude/settings.json"
        )
        assert settings and "Bash(npm test)" in settings
        print("\n  global CLAUDE.md and settings.json written")

        # --- MCP servers merge into ~/.claude.json, not a file under .claude/ --
        claude_json = await profile_mod.read_file(conn, container, "/home/dev/.claude.json")
        assert claude_json is not None
        claude_state = json.loads(claude_json)
        assert claude_state.get("mcpServers") == {}
        # The seed file's own keys must survive the merge.
        assert claude_state.get("hasCompletedOnboarding") is True
        print("  MCP servers merged into ~/.claude.json without clobbering it")

        # --- environment ----------------------------------------------------
        env = await profile_mod.read_file(conn, container, SPACE.env_file)
        assert env and "MOONPHASE_TEST_VAR" in env
        assert "ANTHROPIC_API_KEY" in env, "harness credential missing from env"
        assert "GH_TOKEN" in env, "github token missing from env"

        mode = await docker_remote.exec_capture(
            conn, container, ["stat", "-c", "%a", SPACE.env_file]
        )
        assert mode.stdout.strip() == "600", f"env file mode {mode.stdout.strip()}"
        print("  env file written, mode 600")

        # The launcher must actually export these to the harness.
        assert await sessions.is_authenticated(conn, container, harness)
        seen = await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c", f"set -a; . {SPACE.env_file}; set +a; "
                         'printf "%s" "$MOONPHASE_TEST_VAR"'],
        )
        assert seen.stdout.strip() == "hello-from-profile"
        print("  variables are exported into the harness environment")

        # --- git --------------------------------------------------------------
        name = await docker_remote.exec_capture(
            conn, container, ["git", "config", "--global", "user.name"]
        )
        assert name.stdout.strip() == "Ada Lovelace"

        helper = await docker_remote.exec_capture(
            conn, container, ["git", "config", "--global", "credential.helper"]
        )
        # Pinned to this session's own file rather than git's default location,
        # so two people working in one container cannot read each other's token.
        assert helper.stdout.strip() == (
            f"store --file={profile_mod.git_credentials_file(SPACE)}"
        )

        creds = await profile_mod.read_file(
            conn, container, profile_mod.git_credentials_file(SPACE)
        )
        assert creds and "ghp_fake_token_for_test" in creds
        creds_mode = await docker_remote.exec_capture(
            conn, container, ["stat", "-c", "%a", profile_mod.git_credentials_file(SPACE)]
        )
        assert creds_mode.stdout.strip() == "600"

        # ssh-style URLs must be rewritten, or a repo referenced as git@ still
        # prompts for a key we do not have.
        rewrite = await docker_remote.exec_capture(
            conn, container,
            ["git", "config", "--global", "--get-all", "url.https://github.com/.insteadOf"],
        )
        values = rewrite.stdout.split()
        # Both forms must survive. `git config` without --add replaces, so a
        # naive implementation keeps only the last one and `git clone
        # git@github.com:...` still fails.
        assert "git@github.com:" in values, f"scp-style rewrite missing: {values}"
        assert "ssh://git@github.com/" in values, f"ssh:// rewrite missing: {values}"
        print("  git identity, credential helper and both URL rewrites configured")

        # Re-applying must not accumulate duplicates.
        await profile_mod.apply(conn, container, harness, wp)
        again = await docker_remote.exec_capture(
            conn, container,
            ["git", "config", "--global", "--get-all", "url.https://github.com/.insteadOf"],
        )
        assert len(again.stdout.split()) == 2, (
            f"rewrites accumulated on re-apply: {again.stdout.split()}"
        )
        print("  re-applying does not duplicate git rewrites")

        # --- editing the profile takes effect ---------------------------------
        wp.claude_md = "# Updated\nNow with different guidance.\n"
        await profile_mod.apply(conn, container, harness, wp)
        updated = await profile_mod.read_file(
            conn, container, "/home/dev/.claude/CLAUDE.md"
        )
        assert updated and "Now with different guidance" in updated
        assert "Prefer small commits" not in updated
        print("  re-applying the profile overwrites owned files")

        # --- MCP servers merge without clobbering Claude Code's own state ------
        # Simulate Claude Code itself having written a trust decision into
        # ~/.claude.json between session starts.
        await docker_remote.exec_capture(
            conn, container,
            ["sh", "-c",
             "printf '%s' "
             '\'{"hasCompletedOnboarding":true,"projects":{"/workspace":{"trusted":true}}}\' '
             "> /home/dev/.claude.json"],
        )
        wp.mcp_json = '{"mcpServers":{"fs":{"command":"npx","args":["srv"]}}}'
        await profile_mod.apply(conn, container, harness, wp)
        after_merge = json.loads(
            await profile_mod.read_file(conn, container, "/home/dev/.claude.json")
        )
        assert after_merge["mcpServers"] == {"fs": {"command": "npx", "args": ["srv"]}}
        assert after_merge["projects"]["/workspace"]["trusted"] is True, (
            "merging MCP servers must not discard Claude Code's own state"
        )
        print("  MCP servers merge without discarding trust decisions/history")

        # Clearing the org's MCP config removes the key rather than leaving it.
        wp.mcp_json = None
        await profile_mod.apply(conn, container, harness, wp)
        after_clear = json.loads(
            await profile_mod.read_file(conn, container, "/home/dev/.claude.json")
        )
        assert "mcpServers" not in after_clear
        assert after_clear["projects"]["/workspace"]["trusted"] is True
        print("  clearing MCP servers removes the key and nothing else")

        # --- disconnecting GitHub cleans up ------------------------------------
        wp.vcs_credential = None
        await profile_mod.apply(conn, container, harness, wp)

        gone = await profile_mod.read_file(
            conn, container, profile_mod.git_credentials_file(SPACE)
        )
        assert gone is None, "git credentials survived disconnection"

        helper_after = await docker_remote.exec_capture(
            conn, container, ["git", "config", "--global", "credential.helper"]
        )
        assert not helper_after.stdout.strip(), (
            "credential.helper still set after disconnect; git would prompt against "
            "a file that no longer exists"
        )
        env_after = await profile_mod.read_file(conn, container, SPACE.env_file)
        assert env_after is not None and "GH_TOKEN" not in env_after
        print("  disconnecting GitHub removes the token and the helper")

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


def test_an_unreadable_claude_json_is_left_alone() -> None:
    """Claude Code keeps its trust decisions and project history in the same
    file it reads MCP servers from, so the merge reads before it writes.

    If what is there cannot be parsed, starting from an empty document would
    hand back a file with the MCP servers in it and everything else gone.
    Losing the MCP configuration is much the smaller of those, so nothing is
    written at all.
    """
    from moonphase.harness.claude_code import ClaudeCode

    class Profile:
        mcp_json = '{"mcpServers": {"browser": {"command": "npx"}}}'

    assert ClaudeCode().merge_into_profile_file("{not json at all", Profile()) is None
    # A JSON document that is not an object is equally not something to merge
    # a key into.
    assert ClaudeCode().merge_into_profile_file("[1, 2, 3]", Profile()) is None


def test_state_beside_the_mcp_servers_survives() -> None:
    """The ordinary path: only the one key this owns is replaced."""
    import json

    from moonphase.harness.claude_code import ClaudeCode

    class Profile:
        mcp_json = '{"mcpServers": {"browser": {"command": "npx"}}}'

    existing = json.dumps(
        {
            "hasTrustDialogAccepted": True,
            "projects": {"/workspace": {"lastUsed": "yesterday"}},
            "mcpServers": {"stale": {"command": "gone"}},
        }
    )

    merged = ClaudeCode().merge_into_profile_file(existing, Profile())
    assert merged is not None
    doc = json.loads(merged)

    assert doc["hasTrustDialogAccepted"] is True
    assert doc["projects"] == {"/workspace": {"lastUsed": "yesterday"}}
    # Replaced, not merged into: the profile is the whole truth for this key.
    assert doc["mcpServers"] == {"browser": {"command": "npx"}}
