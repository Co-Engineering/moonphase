"""OpenCode harness.

The second agent Moonphase can run, and the one the seam was drawn for. It is a
terminal TUI like Claude Code, so the session machinery — tmux, the PTY bridge,
attach and detach — needs nothing new; what differs is where it keeps its
credentials, what it calls its resume flag, and that it is not tied to one model
provider.

Everything OpenCode owns lives under `$HOME/.local/share/opencode`, so giving
each session its own HOME separates two people's accounts in one container for
free, exactly as it does for Claude Code.
"""

from __future__ import annotations

import json
from typing import Any

from .base import (
    Harness,
    HarnessAuthMode,
    HarnessCredential,
    HarnessKind,
    LaunchSpec,
    SessionSpace,
    register,
)

__all__ = ["OpenCode"]


def _data_home(space: SessionSpace) -> str:
    """Where OpenCode keeps auth, logs and per-project session storage."""
    return f"{space.home}/.local/share/opencode"


def _config_home(space: SessionSpace) -> str:
    return f"{space.home}/.config/opencode"


class OpenCode(Harness):
    kind = HarnessKind.OPENCODE
    display_name = "OpenCode"
    # API key first, and this is the honest order rather than a preference.
    # OpenCode signs in against whichever provider you use, and its own
    # `auth login` is a TUI that picks a provider from a list — not something
    # that can be driven over a relay the way `claude setup-token` can. So the
    # supported path here is a key.
    supported_auth_modes = (HarnessAuthMode.API_KEY,)

    def launch_spec(
        self, *, resume: bool = False, credential: HarnessCredential | None = None
    ) -> LaunchSpec:
        # Tied to one provider, so the credential says nothing about how to
        # start it.
        del credential
        # `--continue` reopens the most recent session in this directory, which
        # is what makes a session survive its container restarting under it.
        command = ["opencode", "--continue"] if resume else ["opencode"]
        return LaunchSpec(command=command, workdir="/workspace", env={})

    def credential_files(
        self, credential: HarnessCredential, space: SessionSpace
    ) -> dict[str, str]:
        """OpenCode's own auth file, written when we hold a key.

        It reads provider credentials from `auth.json` rather than only from the
        environment, and writing it means `opencode auth list` agrees with what
        the session can actually do — so a user checking from inside the
        terminal is not told they are signed out while working.

        The shape is `{provider: {type, key}}`, which is what the file holds for
        an API-key provider.
        """
        if credential.mode is not HarnessAuthMode.API_KEY or not credential.api_key:
            return {}
        provider = _provider_for(credential.api_key)
        return {
            f"{_data_home(space)}/auth.json": json.dumps(
                {provider: {"type": "api", "key": credential.api_key}}, indent=2
            )
        }

    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        """The same key in the environment, which every provider also reads.

        Both, rather than one: the file is what OpenCode's own commands report
        on, and the variable is what the provider SDK underneath it picks up.
        Setting only one leaves a way for them to disagree.
        """
        if credential.mode is not HarnessAuthMode.API_KEY or not credential.api_key:
            return {}
        provider = _provider_for(credential.api_key)
        variable = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
        }.get(provider)
        return {variable: credential.api_key} if variable else {}

    def seed_config_files(self, space: SessionSpace) -> dict[str, str]:
        """Enough config to land on a usable prompt.

        Only the theme, and only because a first attach from a phone should not
        open on a picker. Nothing here answers a permission question — OpenCode
        asks before it edits or runs things, and that prompt is the user's to
        answer.
        """
        return {
            f"{_config_home(space)}/tui.json": json.dumps({"theme": "system"}, indent=2)
        }

    def profile_files(self, profile: Any, space: SessionSpace) -> dict[str, str]:
        """The user's global instructions, under the name OpenCode reads.

        OpenCode's convention is `AGENTS.md`, and the profile already holds the
        text people write for exactly this purpose. Reusing it means a global
        instruction written once applies to whichever agent a project runs,
        rather than being retyped per harness.
        """
        files: dict[str, str] = {}
        if getattr(profile, "claude_md", None):
            files[f"{_config_home(space)}/AGENTS.md"] = profile.claude_md
        return files

    def activity_signals(self) -> Any:
        from ..activity import ActivitySignals

        return ActivitySignals(
            # OpenCode asks before it edits a file or runs a command, and the
            # prompt is a numbered list with an arrow on the selection — the
            # same shape Claude Code uses, which is not a coincidence: both are
            # terminal pickers.
            prompt_patterns=(
                r"❯\s*\d\.",
                r"\(y/n\)",
                r"Enter to confirm",
                r"Do you want to\b",
                r"Allow\b.*\?",
            ),
            busy_patterns=(),
        )

    def auth_probe_script(self, space: SessionSpace) -> str:
        return (
            f'test -s "{_data_home(space)}/auth.json" '
            '|| test -n "$ANTHROPIC_API_KEY" '
            '|| test -n "$OPENAI_API_KEY"'
        )

    def transcript_dir(self, space: SessionSpace) -> str:
        """Where session and message data lands.

        Per project, under a slug OpenCode derives itself. Moonphase does not
        currently read these — the format is its own, not the JSONL the feed
        parses — so the terminal is the whole surface for now and the feed stays
        empty. Named accurately rather than left pointing somewhere wrong.
        """
        return f"{_data_home(space)}/project"

    def version_command(self) -> list[str]:
        return ["opencode", "--version"]


def _provider_for(api_key: str) -> str:
    """Which provider a key belongs to, from its own prefix.

    Anthropic keys start `sk-ant-`, OpenAI's start `sk-`. Guessing from shape is
    unattractive and it is what there is: the user pastes a key, and asking them
    to also pick a provider from a list is a question whose answer is written on
    the thing they just pasted.
    """
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("sk-"):
        return "openai"
    return "anthropic"


register(OpenCode())
