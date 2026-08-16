"""Project environments.

The base distribution a project's container runs on. It matters more than it
looks: whatever the agent installs with a package manager, and whatever the
project's own tooling assumes about libc or system packages, comes from here.

Deliberately a small closed catalogue rather than free-form image names. Every
entry is an image Moonphase builds from the same recipe, so the harness, tmux,
git and the tunnelling helpers are guaranteed present. Letting a user point a
project at an arbitrary image would mean containers that silently cannot run a
session, and the failure would surface as an unexplained blank terminal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import get_settings

DEFAULT_ENVIRONMENT = "debian"


@dataclass(frozen=True)
class Environment:
    key: str
    display_name: str
    description: str
    base_image: str

    @property
    def image(self) -> str:
        return get_settings().moonphase_runtime_image_template.format(environment=self.key)


ENVIRONMENTS: tuple[Environment, ...] = (
    Environment(
        key="debian",
        display_name="Debian 12",
        description="Stable and small. A good default unless you need otherwise.",
        base_image="debian:bookworm-slim",
    ),
    Environment(
        key="ubuntu",
        display_name="Ubuntu 24.04",
        description="Newer system libraries and the widest package coverage.",
        base_image="ubuntu:24.04",
    ),
)

_BY_KEY = {env.key: env for env in ENVIRONMENTS}


def get(key: str | None) -> Environment:
    """Resolve an environment, falling back to the default for unknown keys.

    Falling back rather than raising keeps projects created before this
    existed — and any row whose value predates a catalogue change — openable.
    """
    if not key:
        return _BY_KEY[DEFAULT_ENVIRONMENT]
    return _BY_KEY.get(key, _BY_KEY[DEFAULT_ENVIRONMENT])


def keys() -> list[str]:
    return [env.key for env in ENVIRONMENTS]


def available() -> list[Environment]:
    return list(ENVIRONMENTS)
