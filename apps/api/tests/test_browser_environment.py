"""The browser and the server that drives it have to agree on a version.

Playwright refuses a browser revision it did not expect. The two halves are
installed at different moments — the browser when the environment image is
built, the MCP server when a session starts — so leaving either unpinned means
they drift apart the day a new Playwright ships. That is what happened: an
image holding chromium-1234 against a server asking for chromium-1237, and an
agent reporting the browser "isn't installed" when it plainly was.

One version is pinned, in `environments.py`, and the browser is derived from
it rather than guessed at.
"""

from __future__ import annotations

import re
from pathlib import Path

from moonphase.environments import _BROWSER_SETUP_SCRIPT, PLAYWRIGHT_MCP_VERSION

TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "apps/web/src/components/ClaudeConfig.tsx"
).read_text()


def test_the_template_pins_the_same_server_the_image_was_built_for() -> None:
    """Different versions here is the whole bug, and nothing else would catch
    it: both halves work perfectly on their own."""
    browser_block = TEMPLATE[
        TEMPLATE.index("label: 'Browser'") : TEMPLATE.index("label: 'Remote (HTTP)'")
    ]
    pinned = re.search(r"@playwright/mcp@([\w.\-]+)", browser_block)

    assert pinned, "the Browser template must pin a version, not track @latest"
    assert pinned.group(1) == PLAYWRIGHT_MCP_VERSION


def test_the_template_does_not_track_latest() -> None:
    """`@latest` resolves when a session starts, against a browser chosen when
    the image was built. They cannot be relied on to agree."""
    assert "@playwright/mcp@latest" not in TEMPLATE


def test_the_browser_is_installed_for_the_pinned_server() -> None:
    """Derived rather than pinned separately: two pins would be two things to
    keep in step, and this has already drifted once."""
    assert f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}" in _BROWSER_SETUP_SCRIPT
    assert "dependencies.playwright" in _BROWSER_SETUP_SCRIPT
    # And the browser is installed from that answer, not from whatever npm
    # would resolve on its own.
    assert 'playwright@"$playwright_version"' in _BROWSER_SETUP_SCRIPT


def test_the_recipe_version_moved() -> None:
    """Existing containers keep their old image until the recipe changes, so a
    fix to the setup script that does not bump it reaches nobody."""
    from moonphase.imagebuild import RECIPE_VERSION

    assert int(RECIPE_VERSION) >= 6


def test_each_change_to_the_image_gets_its_own_recipe_version() -> None:
    """Two changes to what the image contains landed in one release and both
    called themselves v6 — the browser pinning and tmux's allow-passthrough.

    A container keeps the image it was built with, and the tag comes from this
    number, so a second change sharing the first's version reaches none of the
    containers built in between. The note beside it records which change took
    which number, so the next one has somewhere to look.
    """
    from moonphase import imagebuild

    source = Path(imagebuild.__file__).read_text()
    note = source[: source.index("RECIPE_VERSION =")]

    assert "v7" in note and "v6" in note, "say which change took which version"
    assert int(imagebuild.RECIPE_VERSION) >= 7
