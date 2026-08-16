"""The harness seam.

A harness is "some coding agent that runs in a terminal". Moonphase only needs
to know four things about one: how to launch it, how to give it credentials,
whether those credentials are present, and where it writes its transcript.

Everything above this interface — containers, tmux, the PTY bridge, the UI — is
harness-agnostic, so adding OpenCode is a new subclass and a new enum value
rather than a change to the session machinery.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# StrEnum rather than (str, Enum): these values are interpolated into SQL enum
# casts and shell commands, and StrEnum guarantees str(x) is "claude_code"
# rather than "HarnessKind.CLAUDE_CODE".
class HarnessKind(StrEnum):
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"


class HarnessAuthMode(StrEnum):
    OAUTH = "oauth"
    API_KEY = "api_key"


@dataclass
class HarnessCredential:
    """Decrypted credential material on its way into a container."""

    mode: HarnessAuthMode
    api_key: str | None = None
    # The harness's own credential file, verbatim (e.g. .credentials.json).
    oauth_blob: str | None = None


@dataclass
class AuthStatus:
    authenticated: bool
    mode: HarnessAuthMode | None = None
    detail: str | None = None


@dataclass
class LaunchSpec:
    """How to start the harness inside an already-running container."""

    # Argv of the process tmux should own.
    command: list[str]
    workdir: str = "/workspace"
    env: dict[str, str] = field(default_factory=dict)


class Harness(abc.ABC):
    kind: HarnessKind
    display_name: str
    # Modes the harness supports, most-preferred first.
    supported_auth_modes: tuple[HarnessAuthMode, ...]

    @abc.abstractmethod
    def launch_spec(self) -> LaunchSpec:
        """Argv and environment for the interactive session."""

    @abc.abstractmethod
    def credential_files(self, credential: HarnessCredential) -> dict[str, str]:
        """Files to materialise in the container home, as {path: contents}.

        Written with mode 0600 before the session starts.
        """

    @abc.abstractmethod
    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        """Environment variables the harness reads for authentication."""

    def seed_config_files(self) -> dict[str, str]:
        """First-run config to write, as {path: contents}, only if absent.

        For skipping cosmetic first-run wizards so a user attaching from a
        phone lands on a usable prompt. Never write anything here that
        pre-answers a security decision on the user's behalf — those prompts
        exist because the user, not Moonphase, should answer them.
        """
        return {}

    def profile_files(self, profile: Any) -> dict[str, str]:
        """The user's global configuration, as {path: contents}.

        Written on every session start, so editing the profile reaches every
        project on its next restart without re-provisioning. Unlike
        `seed_config_files`, these overwrite: the profile is the source of
        truth for the files it owns.
        """
        return {}

    def auth_status_script(self) -> str | None:
        """A `sh` snippet printing the harness's own auth status as JSON.

        Preferred over `auth_probe_script` when the harness offers it, since
        checking for the presence of a credentials file says nothing about
        whether the credential is still valid.
        """
        return None

    def login_command(self) -> list[str] | None:
        """Argv for an interactive sign-in, driven over a PTY and relayed.

        None means the harness cannot be signed into interactively and must
        use an API key.
        """
        return None

    def login_url_pattern(self) -> str | None:
        """Regex matching the authorization URL the login flow prints."""
        return None

    @abc.abstractmethod
    def auth_probe_script(self) -> str:
        """A `sh` snippet whose exit status reveals whether auth is present.

        A snippet rather than an argv, because the caller runs it in a shell
        that has already sourced the credential env file. Nesting another
        `sh -c` would drop those variables, since sourcing creates shell
        variables and only the caller's surrounding `set -a` exports them.
        """

    @abc.abstractmethod
    def transcript_dir(self, workdir: str = "/workspace") -> str:
        """Directory the harness writes JSONL session transcripts into.

        The phone client tails this rather than scraping the terminal, so the
        two surfaces stay in sync without a scraping protocol.
        """

    @abc.abstractmethod
    def version_command(self) -> list[str]:
        """Command printing the installed harness version."""


_REGISTRY: dict[HarnessKind, Harness] = {}


def register(harness: Harness) -> Harness:
    _REGISTRY[harness.kind] = harness
    return harness


def get(kind: HarnessKind | str) -> Harness:
    if isinstance(kind, str):
        kind = HarnessKind(kind)
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise ValueError(f"No harness registered for {kind!r}.") from None


def available() -> list[Harness]:
    return list(_REGISTRY.values())
