"""Pydantic AI harness.

`clai` running the coder agent from `pydantic-ai-harness` — a terminal agent
with workspace-rooted file access, an allowlisted shell, glob and grep. Not the
bare `clai` chat, which talks to a model and cannot touch the repository it is
sitting in; that distinction is the whole reason this launches with `-a`.

Model-agnostic by design: the agent is chosen with `-a` and the model with `-m`,
so the same harness runs against Anthropic, OpenAI or anything else Pydantic AI
supports. Which one it uses follows from the key it was given.
"""

from __future__ import annotations

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

__all__ = ["PydanticAI"]

# The agent that makes this a coding harness rather than a chat window.
CODER_AGENT = "pydantic_ai_harness.coder:coder_agent"

# Sensible defaults per provider, overridable per project through the profile's
# environment variables. Named rather than latest-aliased so a session does not
# silently change model underneath somebody mid-task.
DEFAULT_MODELS = {
    "anthropic": "anthropic:claude-fable-5",
    "openai": "openai:gpt-5.1",
}


class PydanticAI(Harness):
    kind = HarnessKind.PYDANTIC_AI
    display_name = "Pydantic AI"
    # A key, and only a key. There is no subscription sign-in to relay: the
    # agent authenticates as whichever model provider it is pointed at, using
    # that provider's own environment variable.
    supported_auth_modes = (HarnessAuthMode.API_KEY,)

    def launch_spec(
        self, *, resume: bool = False, credential: HarnessCredential | None = None
    ) -> LaunchSpec:
        """Argv for an interactive session.

        The model is an argument, not an environment variable. `clai` takes
        `-m provider:model` and nothing else — there is no CLAI_MODEL — and it
        defaults to OpenAI, so an Anthropic key with no `-m` fails asking for an
        OpenAI key, which is a confusing way to be told the wrong thing was
        assumed. The provider follows from the key, so this follows from the
        credential.

        `resume` is accepted and ignored, which the base class allows and which
        is worth stating plainly: `clai` persists steps programmatically —
        `continue_run` and `fork_run` against a file or SQLite backend — and
        exposes no flag for it. A restarted container therefore comes back to a
        fresh conversation in the right directory rather than to the one it was
        having. Claude Code and OpenCode both resume; this does not, and
        pretending otherwise with a flag that does nothing would be worse.
        """
        del resume
        model = DEFAULT_MODELS[_provider_for(_key_of(credential))]
        return LaunchSpec(
            command=["clai", "-a", CODER_AGENT, "-m", model],
            workdir="/workspace",
            env={},
        )

    def credential_files(
        self, credential: HarnessCredential, space: SessionSpace
    ) -> dict[str, str]:
        """None. Pydantic AI reads provider keys from the environment only."""
        del credential, space
        return {}

    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        """The provider's own variable, which is how Pydantic AI reads a key.

        The model that has to match it is not here — it is an argument, see
        `launch_spec`.
        """
        if credential.mode is not HarnessAuthMode.API_KEY or not credential.api_key:
            return {}

        provider = _provider_for(credential.api_key)
        variable = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
        }[provider]
        return {variable: credential.api_key}

    def profile_files(self, profile: Any, space: SessionSpace) -> dict[str, str]:
        """The user's global instructions, under the name this agent reads."""
        files: dict[str, str] = {}
        if getattr(profile, "claude_md", None):
            files[f"{space.workdir}/AGENTS.md"] = profile.claude_md
        return files

    def activity_signals(self) -> Any:
        from ..activity import ActivitySignals

        return ActivitySignals(
            # The coder agent asks before it writes a file or runs a command.
            prompt_patterns=(
                r"\(y/n\)",
                r"\[y/N\]",
                r"Allow\b.*\?",
                r"Do you want to\b",
                r"❯\s*\d\.",
            ),
            busy_patterns=(),
        )

    def auth_probe_script(self, space: SessionSpace) -> str:
        del space
        return 'test -n "$ANTHROPIC_API_KEY" || test -n "$OPENAI_API_KEY"'

    def transcript_dir(self, space: SessionSpace) -> str:
        """Where step persistence would write, if a project turns it on.

        Nothing reads this yet: `clai` does not write a transcript of its own
        accord, so the feed stays empty and the terminal is the whole surface.
        Pointing it at a real directory rather than an invented one keeps the
        tail harmless — it finds nothing instead of erroring on a path that
        cannot exist.
        """
        return f"{space.home}/.pydantic-ai/sessions"

    def version_command(self) -> list[str]:
        return ["clai", "--version"]


def _key_of(credential: HarnessCredential | None) -> str:
    """The API key, if there is one to read."""
    if credential is None or credential.mode is not HarnessAuthMode.API_KEY:
        return ""
    return credential.api_key or ""


def _provider_for(api_key: str) -> str:
    """Which provider a key belongs to, from its own prefix.

    An empty key falls through to the Anthropic default, which is what a session
    starting before a credential is attached will get. It will fail to
    authenticate either way; failing against the provider the instance is most
    likely configured for gives the more useful error.
    """
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("sk-"):
        return "openai"
    return "anthropic"


register(PydanticAI())
