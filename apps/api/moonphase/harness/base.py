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
    PYDANTIC_AI = "pydantic_ai"


class HarnessAuthMode(StrEnum):
    OAUTH = "oauth"
    API_KEY = "api_key"


@dataclass
class HarnessCredential:
    """Decrypted credential material on its way into a container.

    An OAuth sign-in yields one of two shapes depending on the flow: a
    long-lived token meant to be exported as an environment variable, or the
    harness's own credential file to be written verbatim. Both are modelled,
    because which one you get is not ours to choose.
    """

    mode: HarnessAuthMode
    api_key: str | None = None
    oauth_token: str | None = None
    oauth_blob: str | None = None


@dataclass
class AuthStatus:
    authenticated: bool
    mode: HarnessAuthMode | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SessionSpace:
    """Where one session keeps its private state inside a shared container.

    Two people in one project run two agents in one container, and neither
    one's credentials, history or commit identity may leak into the other's. A
    session therefore gets its own HOME — which isolates a harness's config, its
    transcripts and `~/.gitconfig` in one move, without depending on any
    particular tool honouring any particular override variable — and its own
    working directory, which is a git worktree on its own branch.

    The defaults describe the shared layout every session used before sessions
    had owners, so a row written back then still resolves to where its files are.
    """

    home: str = "/home/dev"
    workdir: str = "/workspace"

    @property
    def env_file(self) -> str:
        return f"{self.home}/.moonphase-env"

    @property
    def git_config(self) -> str:
        return f"{self.home}/.gitconfig"


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
    def launch_spec(
        self, *, resume: bool = False, credential: HarnessCredential | None = None
    ) -> LaunchSpec:
        """Argv and environment for the interactive session.

        `resume` asks the harness to pick up its previous conversation rather
        than start a new one. It matters after a host reboot: the container
        comes back because of its restart policy, but everything inside it
        started fresh, so without this "it survived" would mean an empty prompt
        in the right directory — which is not what anyone meant.

        Harnesses that cannot resume should ignore it and start normally.

        `credential` is passed because for some harnesses the command depends on
        it. A model-agnostic agent has to be told which model to use, and which
        one is possible follows from the key it was given — `clai` defaults to
        OpenAI and takes the model only as an argument, so an Anthropic key
        without one fails asking for an OpenAI key. Harnesses tied to a single
        provider ignore it.
        """

    @abc.abstractmethod
    def credential_files(
        self, credential: HarnessCredential, space: SessionSpace
    ) -> dict[str, str]:
        """Files to materialise in the container home, as {path: contents}.

        Written with mode 0600 before the session starts.
        """

    @abc.abstractmethod
    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        """Environment variables the harness reads for authentication."""

    def seed_config_files(self, space: SessionSpace) -> dict[str, str]:
        """First-run config to write, as {path: contents}, only if absent.

        For skipping cosmetic first-run wizards so a user attaching from a
        phone lands on a usable prompt. Never write anything here that
        pre-answers a security decision on the user's behalf — those prompts
        exist because the user, not Moonphase, should answer them.
        """
        return {}

    def profile_files(self, profile: Any, space: SessionSpace) -> dict[str, str]:
        """The user's global configuration, as {path: contents}.

        Written on every session start, so editing the profile reaches every
        project on its next restart without re-provisioning. Unlike
        `seed_config_files`, these overwrite: the profile is the source of
        truth for the files it owns.
        """
        return {}

    def profile_file_target(self, space: SessionSpace) -> str | None:
        """Path of a config file the harness also mutates on its own, if any.

        Some profile-owned settings live inside a file the harness treats as
        its own state (trust decisions, history, project list) rather than a
        file Moonphase fully owns — `profile_files()`'s blind overwrite would
        destroy that state. Returning a path here routes that file through
        `merge_into_profile_file` instead, which reads it first. None means
        the harness has no such file.
        """
        return None

    def merge_into_profile_file(self, existing: str | None, profile: Any) -> str | None:
        """New contents for `profile_file_target`, given what is there now.

        `existing` is None when the file does not exist yet. Return None to
        leave the file untouched this session.
        """
        del existing, profile
        return None

    def activity_signals(self) -> Any:
        """Hints for reading a still terminal, as an `ActivitySignals`.

        Returned loosely typed to keep this module free of a dependency on
        `activity`, which imports the harness registry. Patterns only refine a
        pane that has already stopped changing, so an out-of-date pattern
        mislabels a state rather than missing the transition.
        """
        from ..activity import ActivitySignals

        return ActivitySignals()

    def parse_transcript_record(self, record: Any) -> list[Any]:
        """Turn one transcript line into zero or more `TranscriptEvent`s.

        Loosely typed for the same reason as `activity_signals`: the transcript
        module imports the harness registry, so naming its types here would be
        circular. A harness that writes no transcript returns nothing and the
        feed simply stays empty.
        """
        del record
        return []

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
    def auth_probe_script(self, space: SessionSpace) -> str:
        """A `sh` snippet whose exit status reveals whether auth is present.

        A snippet rather than an argv, because the caller runs it in a shell
        that has already sourced the credential env file. Nesting another
        `sh -c` would drop those variables, since sourcing creates shell
        variables and only the caller's surrounding `set -a` exports them.
        """

    @abc.abstractmethod
    def transcript_dir(self, space: SessionSpace) -> str:
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
