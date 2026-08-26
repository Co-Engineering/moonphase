"""Built-in environments — pure data, no server involved."""

from __future__ import annotations

from moonphase import environments


def test_the_browser_environment_is_a_builtin() -> None:
    assert "browser" in environments.builtin_keys()


def test_the_browser_environment_installs_chromium_as_root_before_dev_exists() -> None:
    """imagebuild.py's recipe runs setup_script under `USER root`, before the
    `dev` user is created -- a script that assumed it could write to a
    per-user cache dir would silently install somewhere `dev` can't read."""
    browser = environments.resolve("browser", custom_rows=[])

    assert browser.setup_script is not None
    assert "playwright install" in browser.setup_script
    assert "chromium" in browser.setup_script
    # World-readable: installed as root, used later as `dev`.
    assert "chmod" in browser.setup_script and "a+rX" in browser.setup_script


def test_resolving_an_unknown_key_falls_back_to_the_default() -> None:
    env = environments.resolve("not-a-real-key", custom_rows=[])
    assert env.key == environments.DEFAULT_ENVIRONMENT


def test_a_custom_environment_can_still_shadow_a_builtin_key() -> None:
    custom = [
        {
            "key": "browser",
            "display_name": "My Browser Image",
            "description": "",
            "base_image": "my-registry/browser:latest",
            "setup_script": None,
        }
    ]
    merged = environments.merge(custom)
    browser = next(e for e in merged if e.key == "browser")

    assert browser.builtin is False
    assert browser.base_image == "my-registry/browser:latest"


def test_every_builtin_has_a_distinct_image_tag() -> None:
    """A recipe collision would mean two environments silently share one
    image -- editing one would rebuild the other out from under it."""
    tags = [env.image for env in environments.BUILTINS]
    assert len(tags) == len(set(tags))
