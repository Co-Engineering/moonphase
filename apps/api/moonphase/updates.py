"""Whether a newer Moonphase has been released than the one running.

An instance could not previously say what it was, let alone whether it was
behind: the version was a constant in the source that nobody moved, so "is this
up to date" had no answer and the only way to find out was to upgrade and see.

Both halves are facts now. What is running is stamped into the image at build
time, from the tag or commit it was built from. What is available is the latest
release on GitHub — releases rather than commits or image digests, because a
release is the thing somebody decided was ready, and every other measure says
"behind" every time anyone pushes anything.

Deliberately no Docker socket here. Answering this question needs one public
HTTP request, and a container that can talk to the host's Docker daemon can do
considerably more than answer questions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEFAULT_REPO = "Co-Engineering/moonphase"
GITHUB_TIMEOUT = 15.0

# The answer changes when somebody cuts a release, which is not something worth
# asking GitHub about on every page load.
CACHE_SECONDS = 60 * 60

# A release tag: v1.2.3, with an optional pre-release or build suffix.
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


@dataclass
class UpdateState:
    """What is running, what has been released, and whether they differ."""

    # The release this build is, or None for a build from main or from source.
    running_version: str | None = None
    # Known for any published image, and what to quote when reporting a problem
    # even on a development build.
    running_commit: str | None = None
    latest_version: str | None = None
    release_url: str | None = None
    release_notes: str | None = None
    published_at: str | None = None
    # None when the question could not be answered. GitHub being unreachable,
    # or a development build with nothing to compare against, is not the same as
    # being up to date — and saying so would be a lie somebody acts on.
    update_available: bool | None = None
    detail: str | None = None


def _version_tuple(tag: str) -> tuple[int, int, int] | None:
    match = _TAG.match(tag.strip())
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def is_newer(latest: str, running: str) -> bool:
    """Whether `latest` is a later release than `running`.

    Compared as numbers, not as text: "v0.10.0" sorts before "v0.9.0" as a
    string, which would tell somebody on the newest release that they are behind
    and then stop telling them when they actually are.

    Anything unparseable falls back to inequality, which errs towards offering
    an update that changes nothing rather than hiding one that matters.
    """
    left, right = _version_tuple(latest), _version_tuple(running)
    if left is None or right is None:
        return latest.strip() != running.strip()
    return left > right


def running_version() -> str | None:
    """The release this build is, if it was built from a tag."""
    value = (os.environ.get("MOONPHASE_RELEASE") or "").strip()
    return value if value and value not in {"unknown", "edge"} else None


def running_commit() -> str | None:
    value = (os.environ.get("MOONPHASE_COMMIT") or "").strip()
    return value if value and value != "unknown" else None


_cache: tuple[float, UpdateState] | None = None
_lock = asyncio.Lock()


async def _latest_release() -> dict | None:
    """The newest published release, or None if there are none.

    `/releases/latest` ignores pre-releases and drafts, which is what makes it
    the right endpoint here: the rolling `edge` build is a pre-release, and
    offering it as an update would hand people the thing they already run.
    """
    repo = os.environ.get("MOONPHASE_REPO") or DEFAULT_REPO
    async with httpx.AsyncClient(timeout=GITHUB_TIMEOUT) as client:
        response = await client.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "moonphase",
            },
        )
    if response.status_code == 404:
        # No releases yet. Not an error, and not "up to date" either.
        return None
    response.raise_for_status()
    return response.json()


async def check(*, force: bool = False) -> UpdateState:
    """Compare the running build with the latest release.

    Never raises. A network failure means the question is unanswered, which the
    caller shows as such: turning it into "up to date" would be a lie, and into
    an error would put a red banner on a screen where nothing is wrong.
    """
    global _cache

    async with _lock:
        if not force and _cache is not None:
            checked_at, state = _cache
            if time.monotonic() - checked_at < CACHE_SECONDS:
                return state

        version, commit = running_version(), running_commit()

        try:
            release = await _latest_release()
            failure = None
        except Exception as exc:  # noqa: BLE001 — any failure is "cannot say"
            log.info("update check failed: %s", exc)
            release, failure = None, f"Could not reach GitHub: {exc}"

        if failure is not None:
            state = UpdateState(version, commit, detail=failure)
        elif release is None:
            state = UpdateState(
                version,
                commit,
                detail="No releases have been published yet, so there is "
                "nothing to compare this against.",
            )
        else:
            latest = str(release.get("tag_name") or "").strip()
            notes = (release.get("body") or "").strip()
            common = {
                "latest_version": latest or None,
                "release_url": release.get("html_url"),
                "release_notes": notes[:2000] or None,
                "published_at": release.get("published_at"),
            }
            if not version:
                # A build from main or from source is not a release, so it is
                # neither ahead of nor behind one. Offering "update to v0.2.0"
                # to somebody running the tip would be offering a downgrade.
                state = UpdateState(
                    version,
                    commit,
                    **common,
                    update_available=None,
                    detail="This is a development build, so it is not one of "
                    "the published releases.",
                )
            else:
                state = UpdateState(
                    version,
                    commit,
                    **common,
                    update_available=bool(latest) and is_newer(latest, version),
                )

        _cache = (time.monotonic(), state)
        return state


def forget() -> None:
    """Drop the cached answer, so the next check asks again."""
    global _cache
    _cache = None
