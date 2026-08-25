"""A branch name reaches a git command line, so it is an injection surface.

Shell-quoting is not enough. `git fetch origin <refspec>` reads an argument
beginning with `-` as an option however carefully it is quoted, and
`--upload-pack=<command>` runs that command. A branch name is chosen by
whoever starts a session, so without this check that was arbitrary code
execution inside the project's container, reachable by anyone who could start
a session.

Verified against real git while writing this: `git fetch --depth 50 origin
'--upload-pack=touch FILE #:refs/remotes/origin/x'` creates FILE.
"""

from __future__ import annotations

import pytest

from moonphase.workspaces import is_safe_branch


@pytest.mark.parametrize(
    "name",
    [
        "main",
        "develop",
        "feature/add-thing",
        "release/v0.7.0",
        "user/2026-08-25_fix",
        "v1.2.3",
        "a",
    ],
)
def test_ordinary_branch_names_are_allowed(name: str) -> None:
    """The check has to stay out of the way of names people actually use."""
    assert is_safe_branch(name)


@pytest.mark.parametrize(
    "name",
    [
        # The attack. Anything starting with a dash lands in option position.
        "--upload-pack=touch /tmp/pwned",
        "--upload-pack=sh -c id",
        "-o",
        "--exec=whoami",
        # Shell metacharacters, which quoting handles but which no branch has.
        "main; touch /tmp/pwned",
        "main$(id)",
        "main`id`",
        "main|id",
        "main&whoami",
        "main\nrm -rf /",
        # git's own rules.
        "..",
        "feature/../../etc/passwd",
        "trailing/",
        "some.lock",
        ".hidden",
        "feature/.hidden",
        "double//slash",
        "",
    ],
)
def test_dangerous_or_illegal_names_are_refused(name: str) -> None:
    assert not is_safe_branch(name)


def test_the_command_builder_refuses_rather_than_trusting_its_caller() -> None:
    """The check lives at the edge too, but this is the function that builds
    the command line — so it is where the guarantee has to hold."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from moonphase import workspaces
    from moonphase.ssh import SSHError

    async def attempt() -> None:
        with (
            patch.object(workspaces, "ensure_repository", AsyncMock()),
            patch.object(workspaces, "_run", AsyncMock()) as ran,
        ):
            with pytest.raises(SSHError):
                await workspaces.ensure_worktree(
                    None,  # type: ignore[arg-type]
                    "container",
                    "session",
                    author_name="a",
                    author_email="a@b.c",
                    start_point="--upload-pack=touch /tmp/pwned",
                )
            # Nothing was run at all — it is refused before a command exists.
            ran.assert_not_awaited()

    asyncio.run(attempt())
