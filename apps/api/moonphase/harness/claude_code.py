"""Claude Code harness."""

from __future__ import annotations

import json
from typing import Any

from .base import (
    AuthStatus,
    Harness,
    HarnessAuthMode,
    HarnessCredential,
    HarnessKind,
    LaunchSpec,
    register,
)

__all__ = ["ClaudeCode", "AuthStatus"]

# Claude Code stores per-project transcripts under ~/.claude/projects/<slug>,
# where <slug> is the working directory with every '/' turned into '-'.
CLAUDE_HOME = "/home/dev/.claude"

# The OAuth authorization URL `claude setup-token` prints for the user to open.
LOGIN_URL_PATTERN = r"https://claude\.com/\S*oauth\S*"


def _project_slug(workdir: str) -> str:
    return workdir.replace("/", "-")


class ClaudeCode(Harness):
    kind = HarnessKind.CLAUDE_CODE
    display_name = "Claude Code"
    # OAuth first: it uses the user's existing Pro/Max subscription, which is
    # what most people actually want. API key is the fallback for teams.
    supported_auth_modes = (HarnessAuthMode.OAUTH, HarnessAuthMode.API_KEY)

    def launch_spec(self) -> LaunchSpec:
        # Launched through a wrapper (see sessions.py) so the pane survives the
        # harness exiting and drops to a shell instead of killing the window.
        return LaunchSpec(command=["claude"], workdir="/workspace", env={})

    def credential_files(self, credential: HarnessCredential) -> dict[str, str]:
        if credential.mode is HarnessAuthMode.OAUTH and credential.oauth_blob:
            return {f"{CLAUDE_HOME}/.credentials.json": credential.oauth_blob}
        return {}

    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        if credential.mode is HarnessAuthMode.API_KEY and credential.api_key:
            return {"ANTHROPIC_API_KEY": credential.api_key}
        return {}

    def seed_config_files(self) -> dict[str, str]:
        # Skips the theme picker on first attach. Deliberately does NOT set
        # the per-project trust flag: that prompt guards against hostile
        # content in a cloned repo, and answering it is the user's call, not
        # something Moonphase should quietly do for them.
        return {
            "/home/dev/.claude.json": json.dumps(
                {"hasCompletedOnboarding": True, "theme": "dark"}
            )
        }

    def profile_files(self, profile: Any) -> dict[str, str]:
        """The user's global Claude Code configuration.

        `settings.json` and the global `CLAUDE.md` are exactly the things
        people expect to set once and have everywhere, so they are owned by
        the profile and overwritten on each session start.
        """
        files: dict[str, str] = {}
        if profile.claude_settings_json:
            files[f"{CLAUDE_HOME}/settings.json"] = profile.claude_settings_json
        if profile.claude_md:
            files[f"{CLAUDE_HOME}/CLAUDE.md"] = profile.claude_md
        if profile.mcp_json:
            files[f"{CLAUDE_HOME}/.mcp.json"] = profile.mcp_json
        return files

    def auth_probe_script(self) -> str:
        return f'test -s "{CLAUDE_HOME}/.credentials.json" || test -n "$ANTHROPIC_API_KEY"'

    def auth_status_script(self) -> str:
        # Authoritative, unlike checking for a file: it reports whether the
        # credential actually works and which method is in use.
        return "claude auth status --json 2>/dev/null"

    def login_command(self) -> list[str]:
        # Produces a long-lived token tied to the user's Claude subscription,
        # which is what makes a single global sign-in possible.
        return ["claude", "setup-token"]

    def login_url_pattern(self) -> str:
        return LOGIN_URL_PATTERN

    def transcript_dir(self, workdir: str = "/workspace") -> str:
        return f"{CLAUDE_HOME}/projects/{_project_slug(workdir)}"

    def credential_paths(self) -> list[str]:
        """Files to harvest after an interactive login succeeds."""
        return [f"{CLAUDE_HOME}/.credentials.json"]

    def version_command(self) -> list[str]:
        return ["claude", "--version"]


register(ClaudeCode())
