"""Knowing whether this instance is behind.

The rule the whole feature rests on: a button appears only when there is
something to apply. A screen that offers "Update" to somebody already on the
newest release teaches people to ignore the one time it matters.
"""

from __future__ import annotations

import pytest

from moonphase import updates

# --- comparing releases ------------------------------------------------------


@pytest.mark.parametrize(
    ("latest", "running", "expected"),
    [
        ("v0.2.0", "v0.1.0", True),
        ("v0.1.0", "v0.1.0", False),
        ("v0.1.0", "v0.2.0", False),
        # The case a string comparison gets backwards, and the reason this is
        # parsed rather than compared as text: "v0.10.0" < "v0.9.0" as strings.
        ("v0.10.0", "v0.9.0", True),
        ("v0.9.0", "v0.10.0", False),
        ("v1.0.0", "v0.99.99", True),
        # With and without the v, since a tag may be written either way.
        ("1.2.0", "v1.1.0", True),
        ("v1.2.0", "1.2.0", False),
    ],
)
def test_which_release_is_newer(latest: str, running: str, expected: bool) -> None:
    assert updates.is_newer(latest, running) is expected


def test_a_prerelease_suffix_does_not_confuse_the_comparison() -> None:
    assert updates.is_newer("v0.2.0", "v0.2.0-rc.1") is False
    assert updates.is_newer("v0.3.0", "v0.2.0-rc.1") is True


def test_something_unparseable_falls_back_to_being_different() -> None:
    """Erring towards offering an update that changes nothing, rather than
    hiding one that matters."""
    assert updates.is_newer("nightly-2026-08-21", "nightly-2026-08-20") is True
    assert updates.is_newer("same", "same") is False


# --- what the instance says it is ---------------------------------------------


def test_a_development_build_is_not_a_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """`edge` is the tag every push produces, so it is not a version. Comparing
    it against a release would offer somebody on the tip a downgrade."""
    monkeypatch.setenv("MOONPHASE_RELEASE", "edge")
    assert updates.running_version() is None

    monkeypatch.setenv("MOONPHASE_RELEASE", "unknown")
    assert updates.running_version() is None

    monkeypatch.setenv("MOONPHASE_RELEASE", "v0.2.0")
    assert updates.running_version() == "v0.2.0"


@pytest.mark.asyncio
async def test_a_development_build_is_never_told_to_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOONPHASE_RELEASE", "edge")
    monkeypatch.setenv("MOONPHASE_COMMIT", "abc123")

    async def release() -> dict:
        return {"tag_name": "v0.1.0", "html_url": "https://example.test/r"}

    monkeypatch.setattr(updates, "_latest_release", release)
    updates.forget()

    state = await updates.check(force=True)
    assert state.update_available is None, "a development build is not behind"
    assert state.latest_version == "v0.1.0"
    assert "development build" in (state.detail or "")


@pytest.mark.asyncio
async def test_no_releases_is_not_the_same_as_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the first release there is nothing to compare against, and saying
    "up to date" would be a guess dressed as a fact."""
    monkeypatch.setenv("MOONPHASE_RELEASE", "v0.1.0")

    async def none() -> None:
        return None

    monkeypatch.setattr(updates, "_latest_release", none)
    updates.forget()

    state = await updates.check(force=True)
    assert state.update_available is None
    assert "No releases" in (state.detail or "")


@pytest.mark.asyncio
async def test_github_being_unreachable_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOONPHASE_RELEASE", "v0.1.0")

    async def boom() -> None:
        raise RuntimeError("no network")

    monkeypatch.setattr(updates, "_latest_release", boom)
    updates.forget()

    state = await updates.check(force=True)
    assert state.update_available is None, "a network blip is not 'up to date'"
    assert "Could not reach GitHub" in (state.detail or "")


@pytest.mark.asyncio
async def test_a_released_build_behind_the_latest_is_offered_the_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOONPHASE_RELEASE", "v0.1.0")

    async def release() -> dict:
        return {
            "tag_name": "v0.2.0",
            "html_url": "https://example.test/v0.2.0",
            "body": "Added things",
        }

    monkeypatch.setattr(updates, "_latest_release", release)
    updates.forget()

    state = await updates.check(force=True)
    assert state.update_available is True
    assert state.latest_version == "v0.2.0"
    assert state.release_notes == "Added things"


@pytest.mark.asyncio
async def test_the_newest_release_is_not_offered_an_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOONPHASE_RELEASE", "v0.2.0")

    async def release() -> dict:
        return {"tag_name": "v0.2.0"}

    monkeypatch.setattr(updates, "_latest_release", release)
    updates.forget()

    assert (await updates.check(force=True)).update_available is False


# --- applying it --------------------------------------------------------------


def test_the_request_carries_no_command() -> None:
    """The updater's blast radius is one action, and this is why: what the API
    writes is a nonce, and the updater reads nothing out of it but whether it
    changed. Nothing an API caller can say becomes something the host runs."""
    import inspect

    from moonphase.routers import people

    source = inspect.getsource(people.apply_update)
    assert "token_hex" in source

    script = (
        __import__("pathlib").Path(__file__).resolve().parents[3]
        / "docker/updater.sh"
    ).read_text()
    # The only thing it runs is compose, in a fixed directory.
    assert "docker compose pull" in script
    assert "docker compose up -d" in script
    # Never the contents of the request.
    assert 'eval' not in script
    assert '$(cat "$REQUEST")' not in script


def test_the_updater_is_not_reachable_from_outside() -> None:
    """It holds the Docker socket, which is the whole reason it is a separate
    container. A published port would undo that."""
    compose = (
        __import__("pathlib").Path(__file__).resolve().parents[3]
        / "docker-compose.update.yml"
    ).read_text()

    assert "/var/run/docker.sock" in compose
    assert "ports:" not in compose, "the updater must publish nothing"
    # And the compose project it acts on is read-only to it.
    assert ".:/project:ro" in compose


def test_one_click_updates_are_opt_in() -> None:
    """The default stack has no container that can reach the host's daemon, and
    adding one is a decision an administrator makes deliberately."""
    default = (
        __import__("pathlib").Path(__file__).resolve().parents[3]
        / "docker-compose.yml"
    ).read_text()

    assert "docker.sock" not in default
