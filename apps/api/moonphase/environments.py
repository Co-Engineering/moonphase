"""Project environments.

The base distribution a project's container runs on. It matters more than it
looks: whatever the agent installs with a package manager, and whatever the
project's own tooling assumes about system libraries, comes from here.

An environment is just a base image plus optional setup commands. Moonphase
layers its own requirements on top and builds the image on the managed server,
so defining a new one is data entry rather than a release — see `imagebuild`.

Built-ins live here rather than in the database so a fresh install has sensible
options before anything is configured. Organisation-defined environments are
rows in `public.environments` and are merged with these by the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import imagebuild

DEFAULT_ENVIRONMENT = "debian"


@dataclass(frozen=True)
class Environment:
    key: str
    display_name: str
    description: str
    base_image: str
    setup_script: str | None = None
    builtin: bool = True

    @property
    def image(self) -> str:
        """Tag derived from the recipe, so an edited definition rebuilds."""
        return imagebuild.image_tag(self.key, self.base_image, self.setup_script)


# Headless Chromium needs no Xvfb/DISPLAY — just the browser binary and its
# shared libraries, which `playwright install --with-deps` handles for a
# supported base rather than this hand-maintaining an apt package list. The
# browser goes to a fixed, world-readable path (rather than the default
# ~/.cache/ms-playwright) because this script runs as root before the `dev`
# user in imagebuild.py's recipe even exists — the Browser MCP template
# points PLAYWRIGHT_BROWSERS_PATH at the same path.
# The MCP server the Browser template runs, pinned. The template pins the same
# version — `apps/web/src/components/ClaudeConfig.tsx`, kept honest by a test.
#
# Pinned because the two halves are installed at different moments: the browser
# when this image is built, the server when a session starts. Playwright refuses
# a browser revision it did not expect, so `@latest` on the server side meant
# the pair drifted apart the day a new Playwright shipped — an image holding
# chromium-1234 against a server asking for chromium-1237, and an agent told
# the browser "isn't installed" when it plainly was.
PLAYWRIGHT_MCP_VERSION = "0.0.79"

_BROWSER_SETUP_SCRIPT = f"""
set -eux
mkdir -p /opt/playwright-browsers
# Ask the pinned server which Playwright it runs, and install that one's
# browsers. One version to bump rather than two to keep in step.
playwright_version=$(npm view @playwright/mcp@{PLAYWRIGHT_MCP_VERSION} dependencies.playwright)
PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \\
  npx -y playwright@"$playwright_version" install --with-deps chromium
chmod -R a+rX /opt/playwright-browsers
""".strip()

BUILTINS: tuple[Environment, ...] = (
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
    Environment(
        key="python",
        display_name="Python 3.12",
        description="Debian with a current CPython already installed.",
        base_image="python:3.12-bookworm",
    ),
    Environment(
        key="node",
        display_name="Node 22",
        description="Debian with Node and npm already installed.",
        base_image="node:22-bookworm",
    ),
    Environment(
        key="browser",
        display_name="Debian 12 + browser tools",
        description=(
            "Debian with headless Chromium pre-installed, for an agent that needs "
            "to render or screenshot a page rather than just curl it. Add the "
            "Browser template under Settings → Claude → MCP servers to use it."
        ),
        base_image="debian:bookworm-slim",
        setup_script=_BROWSER_SETUP_SCRIPT,
    ),
)

_BUILTIN_BY_KEY = {env.key: env for env in BUILTINS}


def builtin_keys() -> set[str]:
    return set(_BUILTIN_BY_KEY)


def from_row(row: dict[str, Any]) -> Environment:
    return Environment(
        key=str(row["key"]),
        display_name=str(row["display_name"]),
        description=str(row.get("description") or ""),
        base_image=str(row["base_image"]),
        setup_script=row.get("setup_script"),
        builtin=False,
    )


def merge(custom_rows: list[dict[str, Any]]) -> list[Environment]:
    """Built-ins plus this organization's own, with custom winning on key.

    Allowing a custom entry to shadow a built-in is how someone pins `debian`
    to a different base without every existing project changing key.
    """
    merged = dict(_BUILTIN_BY_KEY)
    for row in custom_rows:
        env = from_row(row)
        merged[env.key] = env
    return sorted(merged.values(), key=lambda e: (not e.builtin, e.display_name))


def resolve(key: str | None, custom_rows: list[dict[str, Any]]) -> Environment:
    """Find an environment by key, falling back to the default.

    Falling back rather than raising keeps a project openable after the
    environment it was created with has been deleted — the container already
    exists, and refusing to show it would help nobody.
    """
    for env in merge(custom_rows):
        if env.key == key:
            return env
    return _BUILTIN_BY_KEY[DEFAULT_ENVIRONMENT]
