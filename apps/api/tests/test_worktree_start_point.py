"""A new session starts from origin's current tip, not the clone's old copy.

`/workspace` is a shallow, single-branch clone, and nothing ever updates its
local default branch after cloning — so a session started "from main" started
from main as it was the day the project was created. These run the real
worktree script with bash against real repositories: an upstream, a shallow
clone of it standing in for `/workspace`, and sessions under a temp directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from moonphase import changes, workspaces

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "ada@example.test",
        "GIT_COMMITTER_NAME": "Ada",
        "GIT_COMMITTER_EMAIL": "ada@example.test",
        # The upstream is a local path; a shallow clone of one needs file://.
        "GIT_ALLOW_PROTOCOL": "file",
    }


class Repos:
    """An upstream with history, and a shallow clone of it as `/workspace`."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.env = _env(self.home)
        self.upstream = tmp / "upstream.git"
        self.seed = tmp / "seed"
        self.workspace = tmp / "workspace"
        self.sessions = tmp / "sessions"

        self.git(tmp, "init", "-q", "--bare", "-b", "main", str(self.upstream))
        self.git(tmp, "init", "-q", "-b", "main", str(self.seed))
        self.git(self.seed, "remote", "add", "origin", str(self.upstream))
        self.commit_upstream("main", count=60)
        self.git(
            tmp, "clone", "-q", "--depth", "50",
            f"file://{self.upstream}", str(self.workspace),
        )

    def git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True
        )
        assert result.returncode == 0, f"git {args}: {result.stderr}"
        return result.stdout.strip()

    def commit_upstream(self, branch: str, count: int = 1, base: str | None = None) -> str:
        """Advance `branch` upstream by `count` commits and return its tip."""
        if base is not None:
            self.git(self.seed, "checkout", "-q", "-B", branch, base)
        elif self.git(self.seed, "symbolic-ref", "--short", "HEAD") != branch:
            self.git(self.seed, "checkout", "-q", branch)
        for _ in range(count):
            self.git(self.seed, "commit", "-q", "--allow-empty", "-m", f"{branch} work")
        self.git(self.seed, "push", "-q", "-f", "origin", f"{branch}:{branch}")
        return self.git(self.seed, "rev-parse", "HEAD")

    def commit_local(self, ref: str) -> str:
        """A commit only the workspace has, on top of `ref`."""
        tree = self.git(self.workspace, "rev-parse", f"{ref}^{{tree}}")
        sha = self.git(
            self.workspace, "commit-tree", tree, "-p", ref, "-m", "local only"
        )
        self.git(self.workspace, "update-ref", ref, sha)
        return sha

    def upstream_tip(self, branch: str) -> str:
        return self.git(self.upstream, "rev-parse", f"refs/heads/{branch}")

    def workdir(self, session: str) -> Path:
        return self.sessions / session / "work"

    def start(
        self, session: str, *, ok: bool = True, **kwargs: str | None
    ) -> tuple[subprocess.CompletedProcess[str], str | None, str | None]:
        script = workspaces.worktree_script(
            str(self.workdir(session)),
            workspaces.branch_for(session),
            repo_root=str(self.workspace),
            **kwargs,  # type: ignore[arg-type]
        )
        result = subprocess.run(
            ["bash", "-c", workspaces.wrap_for_login_shell(script)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if ok:
            assert result.returncode == 0, result.stderr
        base, note = workspaces.read_worktree_output(result.stdout)
        return result, base, note

    def head(self, session: str) -> str:
        return self.git(self.workdir(session), "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


def test_the_default_branch_starts_from_origins_tip_after_upstream_moved(
    repos: Repos,
) -> None:
    stale = repos.git(repos.workspace, "rev-parse", "refs/heads/main")
    tip = repos.commit_upstream("main", count=57)

    _, base, note = repos.start("alice")

    assert repos.head("alice") == tip != stale
    assert base == "refs/remotes/origin/main"
    assert note is None
    # The workspace's own branch is left alone; only origin's copy moves.
    assert repos.git(repos.workspace, "rev-parse", "refs/heads/main") == stale


def test_a_named_branch_that_is_behind_locally_starts_from_origin(repos: Repos) -> None:
    repos.commit_upstream("develop", base="main")
    repos.git(repos.workspace, "fetch", "-q", "origin", "develop:develop")
    tip = repos.commit_upstream("develop", count=3)

    _, base, note = repos.start("alice", start_branch="develop")

    assert repos.head("alice") == tip
    assert base == "refs/remotes/origin/develop"
    assert note is None


def test_a_branch_fetched_earlier_is_refreshed(repos: Repos) -> None:
    repos.commit_upstream("feature", base="main")
    repos.start("first", start_branch="feature")
    tip = repos.commit_upstream("feature", count=2)

    _, base, _ = repos.start("second", start_branch="feature")

    assert repos.head("second") == tip
    assert base == "refs/remotes/origin/feature"


def test_local_commits_ahead_of_origin_are_kept_and_said_so(repos: Repos) -> None:
    ahead = repos.commit_local("refs/heads/main")

    _, base, note = repos.start("alice")

    assert repos.head("alice") == ahead
    assert base == "refs/heads/main"
    assert note and "not on origin" in note


def test_a_diverged_local_branch_is_kept_and_said_so(repos: Repos) -> None:
    repos.commit_upstream("main", count=2)
    ahead = repos.commit_local("refs/heads/main")

    _, base, note = repos.start("alice")

    assert repos.head("alice") == ahead
    assert base == "refs/heads/main"
    assert note and "not on origin" in note


def test_a_branch_only_on_origin_is_fetched(repos: Repos) -> None:
    tip = repos.commit_upstream("only-upstream", base="main", count=2)

    _, base, note = repos.start("alice", start_branch="only-upstream")

    assert repos.head("alice") == tip
    assert base == "refs/remotes/origin/only-upstream"
    assert note is None


def test_a_missing_branch_names_itself_and_what_origin_said(repos: Repos) -> None:
    result, _, _ = repos.start("alice", ok=False, start_branch="nowhere")

    assert result.returncode != 0
    assert "No branch called 'nowhere' was found locally or on origin." in result.stderr
    assert "nowhere" in result.stderr.split("origin.", 1)[1]


def test_a_repository_without_origin_starts_from_its_own_branch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home)
    repo = tmp_path / "workspace"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "Moonphase: initial commit"],
        cwd=repo, env=env, check=True,
    )
    workdir = tmp_path / "sessions" / "alice" / "work"
    script = workspaces.worktree_script(
        str(workdir), "moonphase/alice", repo_root=str(repo)
    )

    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    base, note = workspaces.read_worktree_output(result.stdout)
    assert base == "refs/heads/main"
    assert note is None


def test_an_unreachable_origin_falls_back_to_the_local_copy(repos: Repos) -> None:
    local = repos.git(repos.workspace, "rev-parse", "refs/heads/main")
    repos.git(
        repos.workspace, "remote", "set-url", "origin", str(repos.tmp / "gone.git")
    )

    _, base, note = repos.start("alice")

    assert repos.head("alice") == local
    assert base == "refs/remotes/origin/main"
    assert note and "Could not refresh main from origin" in note
    assert "gone.git" in note


def test_a_start_commit_is_used_exactly_and_never_fetched(repos: Repos) -> None:
    sha = repos.git(repos.workspace, "rev-parse", "HEAD~3")
    # Any fetch would now fail and say so in the note.
    repos.git(
        repos.workspace, "remote", "set-url", "origin", str(repos.tmp / "gone.git")
    )

    _, base, note = repos.start("dup", start_commit=sha)

    assert repos.head("dup") == sha
    assert base == sha
    assert note is None


def test_an_existing_session_branch_is_reused_and_the_request_noted(repos: Repos) -> None:
    repos.start("alice")
    first = repos.head("alice")
    repos.git(repos.workspace, "worktree", "remove", "--force", str(repos.workdir("alice")))
    repos.commit_upstream("other", base="main", count=2)

    _, base, note = repos.start("alice", start_branch="other")

    assert repos.head("alice") == first
    assert base is None
    assert note and "moonphase/alice already existed" in note and "other" in note


def test_an_existing_session_branch_is_reused_quietly_when_nothing_was_asked(
    repos: Repos,
) -> None:
    repos.start("alice")
    repos.git(repos.workspace, "worktree", "remove", "--force", str(repos.workdir("alice")))

    _, base, note = repos.start("alice")

    assert base is None
    assert note is None


def test_a_tag_named_like_the_branch_does_not_win(repos: Repos) -> None:
    tip = repos.commit_upstream("release", base="main", count=2)
    repos.git(repos.workspace, "tag", "release", "HEAD~5")

    _, base, _ = repos.start("alice", start_branch="release")

    assert repos.head("alice") == tip
    assert base == "refs/remotes/origin/release"


def test_the_base_is_recorded_for_the_changes_view(repos: Repos) -> None:
    repos.start("alice")

    recorded = repos.git(
        repos.workspace, "config", "--get", "branch.moonphase/alice.moonphaseBase"
    )

    assert recorded == "refs/remotes/origin/main"


def test_the_session_branch_does_not_track_where_it_started(repos: Repos) -> None:
    """Tracking origin/main would point `git pull` and `git push` in the
    session at main itself."""
    repos.start("alice")

    result = subprocess.run(
        ["git", "config", "--get", "branch.moonphase/alice.merge"],
        cwd=repos.workspace, env=repos.env, capture_output=True, text=True,
    )

    assert result.stdout.strip() == ""


def test_sessions_started_at_once_both_get_their_checkout(repos: Repos) -> None:
    tip = repos.commit_upstream("main", count=3)
    procs = [
        subprocess.Popen(
            [
                "bash", "-c",
                workspaces.worktree_script(
                    str(repos.workdir(name)),
                    workspaces.branch_for(name),
                    repo_root=str(repos.workspace),
                ),
            ],
            env=repos.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for name in ("alice", "bob", "carol")
    ]
    for proc in procs:
        _, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, stderr

    assert {repos.head(name) for name in ("alice", "bob", "carol")} == {tip}
    if shutil.which("flock"):
        assert (repos.workspace / ".git" / "moonphase-worktree.lock").exists()


def test_the_command_builder_refuses_anything_but_a_full_sha() -> None:
    from moonphase.ssh import SSHError

    for bad in ("HEAD", "abc123", "main", "--upload-pack=x", "A" * 40, "a" * 40 + "\n"):
        with pytest.raises(SSHError):
            workspaces.worktree_script("/w", "moonphase/s", start_commit=bad)


def test_fetches_rewrite_githubs_ssh_addresses_to_https() -> None:
    """There is no key in the container, so an origin written as
    `git@github.com:` could not be fetched at all."""
    flags = workspaces._git_env_flags(None)

    assert "url.https://github.com/.insteadOf=git@github.com:" in flags
    assert "url.https://github.com/.insteadOf=ssh://git@github.com/" in flags
    assert "core.askPass=" in flags
    assert "credential.helper" not in flags


def test_every_call_gets_a_credential_file_of_its_own() -> None:
    """One shared path let a branch list and a session start delete each
    other's credential mid-fetch."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from moonphase.ssh import CommandResult

    async def attempt() -> None:
        ran = CommandResult(0, "MOONPHASE_BASE=refs/remotes/origin/main\n", "")
        with (
            patch.object(workspaces, "ensure_repository", AsyncMock()),
            patch.object(workspaces, "_run", AsyncMock(return_value=ran)) as run,
            patch.object(workspaces, "_write_fetch_credential", AsyncMock()) as write,
            patch.object(workspaces, "_clear_fetch_credential", AsyncMock()) as clear,
        ):
            worktree = await workspaces.ensure_worktree(
                None, "c", "alice", token="t0ken"  # type: ignore[arg-type]
            )
            await workspaces.ensure_worktree(
                None, "c", "bob", token="t0ken"  # type: ignore[arg-type]
            )
            await workspaces.list_branches(None, "c", token="t0ken")  # type: ignore[arg-type]

        written = [c.args[2] for c in write.await_args_list]
        cleared = [c.args[2] for c in clear.await_args_list]
        assert len(set(written)) == 3
        assert cleared == written
        for path, call in zip(written, run.await_args_list, strict=True):
            assert path in call.args[2]
        assert worktree.base == "refs/remotes/origin/main"
        assert worktree.note is None

    asyncio.run(attempt())


def test_a_start_commit_needs_no_credential() -> None:
    import asyncio
    from unittest.mock import AsyncMock, patch

    from moonphase.ssh import CommandResult

    async def attempt() -> None:
        with (
            patch.object(workspaces, "ensure_repository", AsyncMock()),
            patch.object(workspaces, "_run", AsyncMock(return_value=CommandResult(0, "", ""))),
            patch.object(workspaces, "_write_fetch_credential", AsyncMock()) as write,
        ):
            await workspaces.ensure_worktree(
                None,  # type: ignore[arg-type]
                "c",
                "dup",
                start_commit="a" * 40,
                token="t0ken",
            )
        write.assert_not_awaited()

    asyncio.run(attempt())


def test_changes_diffs_against_the_recorded_base_not_the_stale_local_branch(
    repos: Repos,
) -> None:
    """Before the base was recorded, the diff ran from merge-base(HEAD, local
    main) — and with local main frozen at clone time, every commit that had
    landed upstream since showed up as the session's own work."""
    upstream_file = repos.seed / "upstream.txt"
    upstream_file.write_text("landed upstream\n")
    repos.git(repos.seed, "add", "upstream.txt")
    repos.git(repos.seed, "commit", "-q", "-m", "upstream change")
    repos.git(repos.seed, "push", "-q", "origin", "main")
    repos.start("alice")
    workdir = repos.workdir("alice")
    (workdir / "mine.txt").write_text("session work\n")
    repos.git(workdir, "add", "mine.txt")
    repos.git(workdir, "commit", "-q", "-m", "session change")

    out = changes.parse(
        subprocess.run(
            ["sh", "-c", changes._script(str(workdir), changes.MAX_PATCH_BYTES)],
            env=repos.env, capture_output=True, text=True, check=True,
        ).stdout
    )

    assert [f.path for f in out.files] == ["mine.txt"]
    assert out.base == "main"


def test_changes_prefers_origins_copy_when_nothing_was_recorded(repos: Repos) -> None:
    """Sessions created before the base was recorded still diff against
    origin's main where there is one, not the stale local copy."""
    upstream_file = repos.seed / "upstream.txt"
    upstream_file.write_text("landed upstream\n")
    repos.git(repos.seed, "add", "upstream.txt")
    repos.git(repos.seed, "commit", "-q", "-m", "upstream change")
    repos.git(repos.seed, "push", "-q", "origin", "main")
    repos.start("alice")
    repos.git(
        repos.workspace, "config", "--unset", "branch.moonphase/alice.moonphaseBase"
    )
    workdir = repos.workdir("alice")
    (workdir / "mine.txt").write_text("session work\n")

    out = changes.parse(
        subprocess.run(
            ["sh", "-c", changes._script(str(workdir), changes.MAX_PATCH_BYTES)],
            env=repos.env, capture_output=True, text=True, check=True,
        ).stdout
    )

    assert [f.path for f in out.files] == ["mine.txt"]
    assert out.base == "main"
