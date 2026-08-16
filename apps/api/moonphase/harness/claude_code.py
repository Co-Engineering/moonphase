"""Claude Code harness."""

from __future__ import annotations

import json

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

    def auth_probe_script(self) -> str:
        # `claude auth status` would be ideal, but its availability varies by
        # version, so probe for the artefacts instead: either a credentials
        # file on disk or a key in the environment.
        return f'test -s "{CLAUDE_HOME}/.credentials.json" || test -n "$ANTHROPIC_API_KEY"'

    def transcript_dir(self, workdir: str = "/workspace") -> str:
        return f"{CLAUDE_HOME}/projects/{_project_slug(workdir)}"

    def version_command(self) -> list[str]:
        return ["claude", "--version"]


register(ClaudeCode())
