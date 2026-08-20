"""The relayed sign-in machinery.

The real flow needs a Claude account, so these tests stand a scripted
interactive program in the harness's place. That is the point: it isolates the
part Moonphase owns — scrape a URL from a PTY, type a code back into it, and
harvest whatever credential appears — from the part Anthropic owns.

Both harvest shapes are covered, because which one a given flow produces is not
something Moonphase gets to decide: a credentials file on disk, or a long-lived
token printed to the terminal.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import pytest

from moonphase import login, provision, ssh
from moonphase.harness import Harness, HarnessAuthMode, HarnessCredential, HarnessKind
from moonphase.harness.base import LaunchSpec
from moonphase.ssh import SSHTarget

FAKE_SERVER_IMAGE = "moonphase/fake-server:latest"
RUNTIME_IMAGE = "moonphase/runtime-claude:latest"
SSH_PORT = 22722
SSH_USER = "deploy"
SSH_PASSWORD = "moonphase-test"

SCRIPT_PATH = "/home/dev/fake-login.sh"
GOOD_CODE = "GOODCODE-123"

# Mimics the shape of the real flow: banner, URL, prompt, then an outcome that
# depends on what was typed.
FILE_FLOW = f"""#!/bin/sh
echo "Welcome to the harness"
echo ""
echo "Browser didn't open? Use the url below to sign in"
echo "https://claude.com/cai/oauth/authorize?code=xyz&client_id=test&scope=user"
echo ""
printf 'Paste code here if prompted > '
read code
if [ "$code" = "{GOOD_CODE}" ]; then
  mkdir -p /home/dev/.claude
  printf '%s' '{{"claudeAiOauth":{{"accessToken":"fake-token"}}}}' \
    > /home/dev/.claude/.credentials.json
  echo "Login successful"
else
  echo "Invalid code supplied"
fi
"""

TOKEN_FLOW = f"""#!/bin/sh
echo "Welcome to the harness"
echo "https://claude.com/cai/oauth/authorize?code=xyz&client_id=test&scope=user"
printf 'Paste code here if prompted > '
read code
if [ "$code" = "{GOOD_CODE}" ]; then
  echo ""
  echo "Your long-lived token (set CLAUDE_CODE_OAUTH_TOKEN):"
  echo "sk-ant-oat01-FAKEfake0123456789abcdefghijklmnop"
else
  echo "Invalid code supplied"
fi
"""


class ScriptedHarness(Harness):
    """A harness whose 'login' is a shell script we control."""

    kind = HarnessKind.CLAUDE_CODE
    display_name = "Scripted"
    supported_auth_modes = (HarnessAuthMode.OAUTH,)

    def __init__(self, script: str) -> None:
        self.script = script

    def launch_spec(self, *, resume: bool = False) -> LaunchSpec:
        return LaunchSpec(command=["sh"])

    def credential_files(self, credential, space) -> dict[str, str]:
        return {}

    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        return {}

    def seed_config_files(self, space) -> dict[str, str]:
        return {SCRIPT_PATH: self.script}

    def auth_probe_script(self, space) -> str:
        return "true"

    def auth_status_script(self) -> str:
        return 'echo {"loggedIn":false}'

    def login_command(self) -> list[str]:
        return ["sh", SCRIPT_PATH]

    def login_url_pattern(self) -> str:
        return r"https://claude\.com/\S*oauth\S*"

    def credential_paths(self) -> list[str]:
        return ["/home/dev/.claude/.credentials.json"]

    def transcript_dir(self, space) -> str:
        return "/home/dev/.claude/projects"

    def version_command(self) -> list[str]:
        return ["true"]


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
    name = f"moonphase-login-{uuid.uuid4().hex[:8]}"
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
    for cid in _docker(
        "ps", "-aq", "--filter", "label=moonphase.login=1", check=False
    ).stdout.split():
        _docker("rm", "-f", cid, check=False)


async def _connect() -> SSHTarget:
    result = await provision.bootstrap(
        server_id=str(uuid.uuid4()),
        server_name="login-test",
        host="127.0.0.1",
        port=SSH_PORT,
        ssh_user=SSH_USER,
        auth_mode="password_bootstrap",
        password=SSH_PASSWORD,
        auto_install_docker=False,
    )
    assert result.status == "online", result.detail
    return SSHTarget(
        server_id=str(uuid.uuid4()),
        host="127.0.0.1",
        port=SSH_PORT,
        username=SSH_USER,
        private_key=result.generated_private_key,
        known_host_key_fp=result.host_key_fingerprint,
    )


async def _until_ready(
    session: login.LoginSession, timeout: float = 120.0
) -> login.LoginSession:
    """Wait for the background preparation to settle, as the client does.

    Polled rather than signalled: the client has only HTTP and does exactly
    this, so the test exercises the same path.
    """
    deadline = time.time() + timeout
    while time.time() < deadline and session.state == "starting":  # noqa: ASYNC110
        await asyncio.sleep(0.5)
    return session


async def _drive(conn, harness: ScriptedHarness, code: str) -> login.LoginSession:
    session = await login.start(
        conn,
        org_id=str(uuid.uuid4()),
        server_id=str(uuid.uuid4()),
        harness=harness,
        image=RUNTIME_IMAGE,
    )
    # start() returns before the work: preparing a sign-in can take minutes on
    # a cold server, and a request held open that long reported a network error
    # for a flow that was succeeding. The client polls, and so does this.
    session = await _until_ready(session)
    assert session.state == "awaiting_code", f"{session.state}: {session.detail}"
    assert session.url and session.url.startswith("https://claude.com/")

    await login.submit_code(conn, session, code)
    # Returning immediately is the contract: a request that blocked for the
    # whole exchange is indistinguishable from a hang, which is what the first
    # implementation did.
    assert session.state == "verifying"

    deadline = time.time() + 60
    while time.time() < deadline and session.state == "verifying":
        session = await login.advance(conn, session, harness)
        if session.state != "verifying":
            break
        await asyncio.sleep(1)
    return session


@pytest.mark.asyncio(loop_scope="module")
async def test_captures_a_credentials_file(fake_server: str) -> None:
    target = await _connect()
    conn = await ssh.pool.get(target)
    try:
        harness = ScriptedHarness(FILE_FLOW)
        session = await _drive(conn, harness, GOOD_CODE)

        assert session.state == "complete", f"{session.detail}\n{session.pane}"
        assert session.oauth_blob and "accessToken" in session.oauth_blob
        assert session.oauth_token is None
        print(f"\n  captured credentials file: {session.oauth_blob[:40]}…")

        # The throwaway container must not outlive the flow.
        remaining = _docker(
            "ps", "-aq", "--filter", f"name={session.container}", check=False
        ).stdout.strip()
        assert not remaining, "login container was left running"
        print("  throwaway container cleaned up")
    finally:
        await ssh.pool.close_all()


@pytest.mark.asyncio(loop_scope="module")
async def test_captures_a_printed_token(fake_server: str) -> None:
    """The `setup-token` shape: nothing on disk, a token on the screen.

    The first implementation only looked for a file, so this flow polled until
    it timed out — reporting a successful sign-in as a failure.
    """
    target = await _connect()
    conn = await ssh.pool.get(target)
    try:
        harness = ScriptedHarness(TOKEN_FLOW)
        session = await _drive(conn, harness, GOOD_CODE)

        assert session.state == "complete", f"{session.detail}\n{session.pane}"
        assert session.oauth_token == "sk-ant-oat01-FAKEfake0123456789abcdefghijklmnop"
        assert session.oauth_blob is None
        print(f"\n  scraped token: {session.oauth_token[:24]}…")

        # And it must reach the harness as the variable it reads.
        from moonphase.harness import get as get_harness

        env = get_harness("claude_code").credential_env(
            HarnessCredential(mode=HarnessAuthMode.OAUTH, oauth_token=session.oauth_token)
        )
        assert env == {"CLAUDE_CODE_OAUTH_TOKEN": session.oauth_token}
        print("  exported as CLAUDE_CODE_OAUTH_TOKEN")
    finally:
        await ssh.pool.close_all()


@pytest.mark.asyncio(loop_scope="module")
async def test_a_rejected_code_fails_fast_with_the_terminal_shown(
    fake_server: str,
) -> None:
    target = await _connect()
    conn = await ssh.pool.get(target)
    try:
        harness = ScriptedHarness(FILE_FLOW)
        session = await _drive(conn, harness, "WRONG-CODE")

        assert session.state == "error", session.state
        assert session.detail and "rejected" in session.detail.lower()
        # Without the pane the user has no idea what happened.
        assert "Invalid code supplied" in session.pane
        print(f"\n  rejected cleanly: {session.detail}")
    finally:
        await ssh.pool.close_all()


def test_signing_in_builds_its_image_rather_than_assuming_one() -> None:
    """Signing in must not depend on having created a project first.

    It did: the relay was handed the bare MOONPHASE_RUNTIME_IMAGE tag, which
    nothing ever builds — projects use a per-environment tag instead. On a
    freshly added server `docker run` failed with "Unable to find image
    locally", and the button appeared to do nothing at all.
    """
    import inspect

    from moonphase import login

    source = inspect.getsource(login._prepare)

    assert "ensure_image" in source
    # And before the container is started, not after. Matched on the argument
    # list rather than the word, which also appears in the comment explaining
    # why this is here.
    assert source.index("ensure_image") < source.index('"docker", "run"')


def test_the_relay_uses_the_same_image_a_project_would() -> None:
    """So whichever happens first pays for the build and the other finds it."""
    import inspect

    from moonphase.routers import profile

    source = inspect.getsource(profile)

    assert "default_env.image" in source
    assert "moonphase_runtime_image" not in source


def test_starting_a_sign_in_does_not_wait_for_the_work() -> None:
    """Preparing a sign-in builds an image, starts a container and waits for a
    URL — minutes on a cold machine. Doing that inside the request is what made
    the button report a network error while working perfectly underneath.

    The client already polls the session, so there is nothing to wait for.
    """
    import inspect

    from moonphase import login

    source = inspect.getsource(login.start)

    assert "ensure_image" not in source
    assert "ssh.run" not in source
    assert "_prepare" in source


def test_the_session_says_what_it_is_doing_while_it_does_it() -> None:
    """The slow step is a container build. A spinner with no words next to it
    is indistinguishable from being stuck."""
    import inspect

    from moonphase import login

    source = inspect.getsource(login._prepare)

    assert "Preparing the container image" in source
    assert "Waiting for the sign-in link" in source


@pytest.mark.asyncio
async def test_sweeping_spares_the_sessions_still_in_flight(fake_server: str) -> None:
    """A sign-in lives in memory, so restarting the API forgets every attempt in
    flight while their containers keep running on the server. Nothing removed
    them, so they piled up one per abandoned attempt.

    Sweeping by label alone would have two simultaneous sign-ins kill each
    other, so a container a live session still names is left where it is.
    """
    target = await _connect()
    conn = await ssh.pool.get(target)
    try:
        live = f"{login.CONTAINER_PREFIX}keepme"
        orphan = f"{login.CONTAINER_PREFIX}orphan"
        for name in (live, orphan):
            result = await ssh.run(
                conn,
                f"docker run -d --name {name} --label moonphase.login=1 "
                f"{RUNTIME_IMAGE} sleep 300",
                timeout=120,
            )
            assert result.ok, result.stderr

        session = login.LoginSession(
            id="live",
            org_id=str(uuid.uuid4()),
            harness_kind=str(HarnessKind.CLAUDE_CODE),
            server_id=str(uuid.uuid4()),
            container=live,
        )
        login._sessions[session.id] = session
        try:
            removed = await login.sweep_stale(conn)
        finally:
            login.forget(session.id)

        assert removed == 1

        listing = await ssh.run(
            conn,
            "docker ps -a --filter label=moonphase.login=1 --format '{{.Names}}'",
            timeout=60,
        )
        names = listing.stdout.split()
        assert live in names
        assert orphan not in names

        await ssh.run(conn, f"docker rm -f {live}", timeout=60)
    finally:
        await ssh.pool.close_all()
