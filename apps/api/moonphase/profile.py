"""The global workspace profile.

One profile per organization, materialised into every project container when
its session starts. This is what makes "sign in once, configure once" true:
adding a server or a project never asks for credentials or settings again.

Materialisation is idempotent and runs on every session start rather than only
at container creation, so editing the profile takes effect on the next restart
of any project without re-provisioning anything.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass, field
from typing import Any

import asyncssh

from . import docker_remote, ssh
from .harness import Harness, HarnessAuthMode, HarnessCredential, SessionSpace

log = logging.getLogger(__name__)

MOONPHASE_DIR = "/home/dev/.moonphase"
ENV_FILE = f"{MOONPHASE_DIR}/env"
def git_credentials_file(space: SessionSpace) -> str:
    """Per session: a token is a person's, not a container's."""
    return f"{space.home}/.git-credentials"


@dataclass
class VcsCredential:
    provider: str
    token: str
    account: str | None = None


@dataclass
class WorkspaceProfile:
    """Everything a user configures once and expects everywhere."""

    org_id: str
    claude_settings_json: str | None = None
    claude_md: str | None = None
    mcp_json: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    git_user_name: str | None = None
    git_user_email: str | None = None

    # Resolved separately from the private schema.
    harness_credential: HarnessCredential | None = None
    vcs_credential: VcsCredential | None = None

    @property
    def has_harness_auth(self) -> bool:
        return self.harness_credential is not None

    @property
    def has_vcs_auth(self) -> bool:
        return self.vcs_credential is not None


async def write_file(
    conn: asyncssh.SSHClientConnection,
    container: str,
    path: str,
    contents: str,
    *,
    mode: str = "600",
    overwrite: bool = True,
) -> None:
    """Write a file inside a container without it touching the host disk.

    Contents travel over stdin of `docker exec`, so secrets never appear in the
    remote process list the way `echo <token>` would.
    """
    quoted = shlex.quote(path)
    directory = shlex.quote(path.rsplit("/", 1)[0])
    if overwrite:
        inner = f"mkdir -p {directory} && cat > {quoted} && chmod {mode} {quoted}"
    else:
        inner = (
            f"mkdir -p {directory}; "
            f"if [ ! -e {quoted} ]; then cat > {quoted} && chmod {mode} {quoted}; "
            f"else cat > /dev/null; fi"
        )
    command = (
        f"docker exec -i -u dev {shlex.quote(container)} sh -c " + shlex.quote(inner)
    )
    result = await ssh.run(conn, command, timeout=60, stdin=contents)
    result.check(f"Writing {path} into {container}")


async def read_file(
    conn: asyncssh.SSHClientConnection, container: str, path: str
) -> str | None:
    """Read a file from a container, or None when it does not exist."""
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", f"cat {shlex.quote(path)} 2>/dev/null"], timeout=30
    )
    if not result.ok or not result.stdout:
        return None
    return result.stdout


def _git_credentials_line(credential: VcsCredential) -> str:
    # GitHub accepts any username when the password is a token; x-access-token
    # is the documented placeholder and avoids implying it is a real account.
    return f"https://x-access-token:{credential.token}@github.com"


async def apply(
    conn: asyncssh.SSHClientConnection,
    container: str,
    harness: Harness,
    profile: WorkspaceProfile,
    space: SessionSpace | None = None,
) -> None:
    """Materialise one person's profile into one session's private state.

    Everything below is written under the session's own HOME. Two sessions in
    the same container therefore hold two different accounts, two different
    tokens and two different commit identities, and neither can see the other's
    — which is the whole reason a session has an owner.

    `git config` is run with GIT_CONFIG_GLOBAL pointed at that HOME rather than
    `--global`, which would resolve to the container's shared /home/dev and let
    the last session to start decide who everybody commits as.

    Ordering matters: harness config first (so a fresh container is usable even
    if credentials fail), then credentials, then git.
    """
    space = space or SessionSpace()
    git_env = f"GIT_CONFIG_GLOBAL={shlex.quote(space.git_config)}"

    # --- harness configuration ---------------------------------------------
    for path, contents in harness.profile_files(profile, space).items():
        await write_file(conn, container, path, contents, mode="600")

    # Some profile-owned settings (e.g. Claude Code's MCP servers) live inside
    # a file the harness also mutates on its own, so it is read first and only
    # the profile's keys are merged in rather than overwritten wholesale.
    merge_target = harness.profile_file_target(space)
    if merge_target is not None:
        existing = await read_file(conn, container, merge_target)
        merged = harness.merge_into_profile_file(existing, profile)
        if merged is not None:
            await write_file(conn, container, merge_target, merged, mode="600")

    # --- harness credentials ------------------------------------------------
    if profile.harness_credential is not None:
        for path, contents in harness.credential_files(
            profile.harness_credential, space
        ).items():
            await write_file(conn, container, path, contents, mode="600")

    # --- environment ---------------------------------------------------------
    env: dict[str, str] = dict(profile.env_vars)
    # HOME is what separates one session's harness state from another's, and
    # GIT_CONFIG_GLOBAL does the same for identity and credential helpers.
    env["HOME"] = space.home
    env["GIT_CONFIG_GLOBAL"] = space.git_config
    if profile.harness_credential is not None:
        env.update(harness.credential_env(profile.harness_credential))
    if profile.vcs_credential is not None:
        # Exposed so the agent can call `gh` without being handed the token
        # explicitly in a prompt.
        env["GH_TOKEN"] = profile.vcs_credential.token
        env["GITHUB_TOKEN"] = profile.vcs_credential.token

    body = "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items())
    await write_file(conn, container, space.env_file, body, mode="600")

    # --- git ------------------------------------------------------------------
    git_config: list[str] = []
    if profile.git_user_name:
        git_config.append(f"git config --global user.name {shlex.quote(profile.git_user_name)}")
    if profile.git_user_email:
        git_config.append(
            f"git config --global user.email {shlex.quote(profile.git_user_email)}"
        )

    if profile.vcs_credential is not None:
        await write_file(
            conn,
            container,
            git_credentials_file(space),
            _git_credentials_line(profile.vcs_credential) + "\n",
            mode="600",
        )
        git_config.append(
            "git config --global credential.helper "
            + shlex.quote(f"store --file={git_credentials_file(space)}")
        )
        # Rewrite ssh and bare git URLs to authenticated https, so a repo the
        # agent discovers in a README clones without a second credential.
        #
        # `insteadOf` is multi-valued and plain `git config` *replaces* rather
        # than appends, so a second plain set would silently discard the first
        # rewrite. Clear the key, then --add each value; that is also what keeps
        # this idempotent across session restarts instead of growing the list.
        git_config.append(
            "git config --global --unset-all url.'https://github.com/'.insteadOf "
            "2>/dev/null || true"
        )
        git_config.append(
            "git config --global --add url.'https://github.com/'.insteadOf "
            "'git@github.com:'"
        )
        git_config.append(
            "git config --global --add url.'https://github.com/'.insteadOf "
            "'ssh://git@github.com/'"
        )
    else:
        # Leaving a stale helper or rewrite configured after disconnecting
        # GitHub would make git prompt against a file we just deleted, or
        # rewrite ssh URLs to https with no credential behind them.
        git_config.append("git config --global --unset-all credential.helper || true")
        git_config.append(
            "git config --global --unset-all url.'https://github.com/'.insteadOf "
            "2>/dev/null || true"
        )
        await docker_remote.exec_capture(
            conn, container, ["rm", "-f", git_credentials_file(space)], timeout=30
        )

    if git_config:
        await docker_remote.exec_capture(
            conn,
            container,
            ["sh", "-c", f"export {git_env}; " + "; ".join(git_config)],
            timeout=60,
        )


def profile_from_row(
    row: dict[str, Any],
    harness_credential: HarnessCredential | None = None,
    vcs_credential: VcsCredential | None = None,
) -> WorkspaceProfile:
    raw_env = row.get("env_vars") or {}
    if isinstance(raw_env, str):
        try:
            raw_env = json.loads(raw_env)
        except json.JSONDecodeError:
            raw_env = {}
    return WorkspaceProfile(
        org_id=str(row["org_id"]),
        claude_settings_json=row.get("claude_settings_json"),
        claude_md=row.get("claude_md"),
        mcp_json=row.get("mcp_json"),
        env_vars={str(k): str(v) for k, v in dict(raw_env).items()},
        git_user_name=row.get("git_user_name"),
        git_user_email=row.get("git_user_email"),
        harness_credential=harness_credential,
        vcs_credential=vcs_credential,
    )


def credential_from_row(row: dict[str, Any] | None) -> HarnessCredential | None:
    if row is None:
        return None
    return HarnessCredential(
        mode=HarnessAuthMode(row["auth_mode"]),
        api_key=row.get("api_key"),
        oauth_token=row.get("oauth_token"),
        oauth_blob=row.get("oauth_blob"),
    )
