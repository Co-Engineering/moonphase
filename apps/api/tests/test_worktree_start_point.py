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

    def commit_upstream_file(self, branch: str, name: str) -> str:
        """A commit upstream that adds `name`, so a diff can tell it apart."""
        if self.git(self.seed, "symbolic-ref", "--short", "HEAD") != branch:
            self.git(self.seed, "checkout", "-q", branch)
        (self.seed / name).write_text(f"{name}\n")
        self.git(self.seed, "add", name)
        self.git(self.seed, "commit", "-q", "-m", f"add {name}")
        self.git(self.seed, "push", "-q", "origin", f"{branch}:{branch}")
        return self.git(self.seed, "rev-parse", "HEAD")

    def commit_in(self, session: str, name: str) -> str:
        """A commit of the session's own, adding `name`."""
        workdir = self.workdir(session)
        (workdir / name).write_text(f"{name}\n")
        self.git(workdir, "add", name)
        self.git(workdir, "commit", "-q", "-m", f"add {name}")
        return self.git(workdir, "rev-parse", "HEAD")

    def changed(self, session: str) -> list[str]:
        out = changes.parse(
            subprocess.run(
                ["sh", "-c", changes._script(str(self.workdir(session)), changes.MAX_PATCH_BYTES)],
                env=self.env, capture_output=True, text=True, check=True,
            ).stdout
        )
        return sorted(f.path for f in out.files)

    def commit_local(self) -> str:
        """A commit only the workspace has, made on its main the ordinary way."""
        self.git(self.workspace, "commit", "-q", "--allow-empty", "-m", "local only")
        return self.git(self.workspace, "rev-parse", "HEAD")

    def has_ref(self, ref: str) -> bool:
        return subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", ref],
            cwd=self.workspace, env=self.env,
        ).returncode == 0

    def record(self, session: str) -> Path:
        gitdir = self.git(self.workdir(session), "rev-parse", "--absolute-git-dir")
        return Path(gitdir) / "moonphase-base"

    def shallow_cut(self) -> None:
        """Draw a shallow boundary through main's history, the way fetching a
        new branch at depth 50 does, then refresh origin's main past it."""
        self.commit_upstream("main", count=60)
        self.commit_upstream("pr-123", base="main")
        self.start("review", start_branch="pr-123")
        self.commit_upstream("main")
        self.start("bob")

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


_AHEAD_NOTE = (
    "Your local main has commits that aren't on origin; this session starts "
    "from origin's main. They are still on the local branch."
)


def test_local_commits_ahead_of_origin_are_mentioned_but_origin_wins(
    repos: Repos,
) -> None:
    tip = repos.upstream_tip("main")
    ahead = repos.commit_local()

    _, base, note = repos.start("alice")

    assert repos.head("alice") == tip
    assert base == "refs/remotes/origin/main"
    assert note == _AHEAD_NOTE
    assert repos.git(repos.workspace, "rev-parse", "refs/heads/main") == ahead


def test_a_diverged_local_branch_is_mentioned_but_origin_wins(repos: Repos) -> None:
    tip = repos.commit_upstream("main", count=2)
    ahead = repos.commit_local()

    _, base, note = repos.start("alice")

    assert repos.head("alice") == tip
    assert base == "refs/remotes/origin/main"
    assert note == _AHEAD_NOTE
    assert repos.git(repos.workspace, "rev-parse", "refs/heads/main") == ahead


def test_an_expired_reflog_does_not_make_a_force_push_look_like_local_work(
    repos: Repos,
) -> None:
    """gc expires the clone's reflog after 90 days; telling an untouched
    branch apart by it then failed, and local won over origin."""
    repos.git(repos.workspace, "reflog", "expire", "--expire=now", "--all")
    rewritten = repos.commit_upstream("main", base="main~3", count=2)

    _, base, note = repos.start("alice")

    assert repos.head("alice") == rewritten
    assert base == "refs/remotes/origin/main"
    assert note is None


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


def test_the_start_is_recorded_in_the_worktrees_own_git_dir(repos: Repos) -> None:
    tip = repos.commit_upstream("main")

    repos.start("alice")

    assert repos.record("alice").read_text() == f"refs/remotes/origin/main\n{tip}\n"
    config = subprocess.run(
        ["git", "config", "--get-regexp", "moonphase"],
        cwd=repos.workspace, env=repos.env, capture_output=True, text=True,
    )
    assert config.stdout == ""


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
    """With no record and no reflog to fall back on, still diff against
    origin's main where there is one, not the stale local copy."""
    upstream_file = repos.seed / "upstream.txt"
    upstream_file.write_text("landed upstream\n")
    repos.git(repos.seed, "add", "upstream.txt")
    repos.git(repos.seed, "commit", "-q", "-m", "upstream change")
    repos.git(repos.seed, "push", "-q", "origin", "main")
    repos.start("alice")
    repos.record("alice").unlink()
    repos.git(
        repos.workspace, "reflog", "expire", "--expire=now", "refs/heads/moonphase/alice"
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


# --- The clone's own default branch never moved, so origin's copy wins ---


def test_a_shallow_fetch_of_another_branch_does_not_make_main_look_diverged(
    repos: Repos,
) -> None:
    """Fetching a branch not yet on disk at depth 50 can draw a shallow
    boundary inside main's history. Ancestry is then unprovable, and the
    untouched clone-time main read as "local has commits"."""
    repos.commit_upstream("main", count=60)
    repos.commit_upstream("pr-123", base="main")
    repos.start("review", start_branch="pr-123")
    tip = repos.commit_upstream("main")

    _, base, note = repos.start("alice")

    assert repos.head("alice") == tip
    assert base == "refs/remotes/origin/main"
    assert note is None


def test_a_force_pushed_default_branch_is_followed_when_local_never_moved(
    repos: Repos,
) -> None:
    rewritten = repos.commit_upstream("main", base="main~3", count=2)

    _, base, note = repos.start("alice")

    assert repos.head("alice") == rewritten
    assert base == "refs/remotes/origin/main"
    assert note is None


def test_a_force_pushed_branch_created_from_origin_is_followed(repos: Repos) -> None:
    repos.commit_upstream("feature", base="main", count=2)
    repos.git(repos.workspace, "fetch", "-q", "origin", "feature:refs/remotes/origin/feature")
    repos.git(repos.workspace, "branch", "feature", "refs/remotes/origin/feature")
    rewritten = repos.commit_upstream("feature", base="feature~2")

    _, base, note = repos.start("alice", start_branch="feature")

    assert repos.head("alice") == rewritten
    assert base == "refs/remotes/origin/feature"
    assert note is None


# --- The Changes view diffs against the commit the session started from ---


def test_changes_ignore_a_force_push_of_where_the_session_started(repos: Repos) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        repos.commit_upstream_file("main", name)
    repos.start("alice")
    repos.commit_in("alice", "mine.txt")
    repos.commit_upstream("main", base="main~3")
    repos.git(repos.workdir("alice"), "fetch", "-q", "origin")

    assert repos.changed("alice") == ["mine.txt"]


def test_changes_survive_a_shallow_fetch_cutting_through_the_base(repos: Repos) -> None:
    """The cut leaves no merge-base at all, and the fallback was the root of
    the session's history — every file the clone had, as the session's work."""
    repos.commit_upstream_file("main", "landed.txt")
    repos.start("alice")
    repos.commit_in("alice", "mine.txt")
    repos.shallow_cut()

    assert repos.changed("alice") == ["mine.txt"]


def _merge_upstream_into(repos: Repos, session: str) -> None:
    workdir = repos.workdir(session)
    repos.git(workdir, "fetch", "-q", "origin")
    repos.git(workdir, "merge", "-q", "--no-edit", "refs/remotes/origin/main")


def test_changes_hide_upstream_work_the_session_merged_in(repos: Repos) -> None:
    repos.start("alice")
    repos.commit_in("alice", "mine.txt")
    repos.commit_upstream_file("main", "upstream.txt")
    _merge_upstream_into(repos, "alice")

    assert repos.changed("alice") == ["mine.txt"]


def test_changes_hide_merged_upstream_work_after_a_shallow_cut(repos: Repos) -> None:
    repos.start("alice")
    repos.commit_in("alice", "mine.txt")
    repos.commit_upstream_file("main", "upstream.txt")
    _merge_upstream_into(repos, "alice")
    repos.shallow_cut()

    assert repos.changed("alice") == ["mine.txt"]


def test_changes_hide_merged_upstream_work_after_a_force_push_below_the_start(
    repos: Repos,
) -> None:
    repos.start("alice")
    repos.commit_in("alice", "mine.txt")
    repos.commit_upstream_file("main", "upstream.txt")
    _merge_upstream_into(repos, "alice")
    repos.commit_upstream("main", base="main~3")
    repos.git(repos.workdir("alice"), "fetch", "-q", "origin")

    assert repos.changed("alice") == ["mine.txt"]


@pytest.mark.parametrize("rewrite", ["force-push", "shallow-cut"])
def test_changes_keep_the_recorded_start_after_the_agent_switches_branch(
    repos: Repos, rewrite: str
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        repos.commit_upstream_file("main", name)
    repos.start("alice")
    repos.git(repos.workdir("alice"), "checkout", "-q", "-b", "my-feature")
    repos.commit_in("alice", "mine.txt")
    if rewrite == "force-push":
        repos.commit_upstream("main", base="main~3")
        repos.git(repos.workdir("alice"), "fetch", "-q", "origin")
    else:
        repos.shallow_cut()

    assert repos.changed("alice") == ["mine.txt"]


def test_changes_of_a_session_without_a_record_start_where_its_branch_was_created(
    repos: Repos,
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        repos.commit_upstream_file("main", name)
    repos.start("alice")
    repos.record("alice").unlink()
    repos.commit_in("alice", "mine.txt")
    repos.commit_upstream("main", base="main~3")
    repos.git(repos.workdir("alice"), "fetch", "-q", "origin")

    assert repos.changed("alice") == ["mine.txt"]


def test_changes_of_a_duplicate_start_at_its_snapshot(repos: Repos) -> None:
    repos.start("alice")
    snapshot = repos.commit_in("alice", "mine.txt")
    repos.start("dup", start_commit=snapshot)
    repos.commit_in("dup", "dup.txt")

    assert repos.record("dup").read_text() == f"{snapshot}\n{snapshot}\n"
    assert repos.changed("dup") == ["dup.txt"]


# --- The lock is not held past the script by something git left running ---


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock")
def test_nothing_git_leaves_running_keeps_holding_the_lock(repos: Repos) -> None:
    """`git fetch` can leave a detached `gc --auto` behind. Holding the lock's
    descriptor, it made the next session wait out its whole 100 seconds."""
    bin_dir = repos.tmp / "bin"
    bin_dir.mkdir()
    pids = repos.tmp / "pids"
    real_git = shutil.which("git")
    wrapper = bin_dir / "git"
    # Stands in for gc --auto: detached from the output, every other fd inherited.
    wrapper.write_text(
        "#!/bin/sh\n"
        f"sleep 30 </dev/null >/dev/null 2>&1 &\necho $! >> {pids}\n"
        f'exec {real_git} "$@"\n'
    )
    wrapper.chmod(0o755)
    env = {**repos.env, "PATH": f"{bin_dir}:{repos.env['PATH']}"}
    script = workspaces.worktree_script(
        str(repos.workdir("alice")),
        workspaces.branch_for("alice"),
        repo_root=str(repos.workspace),
    )
    try:
        result = subprocess.run(
            ["bash", "-c", workspaces.wrap_for_login_shell(script)],
            env=env, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr

        lock = repos.workspace / ".git" / "moonphase-worktree.lock"
        probe = subprocess.run(["flock", "-n", str(lock), "true"], capture_output=True)

        assert probe.returncode == 0, "the lock is still held after the script exited"
    finally:
        for pid in pids.read_text().split() if pids.exists() else []:
            try:
                os.kill(int(pid), 9)
            except ProcessLookupError:
                pass


# --- A branch origin deleted (a merged pull request, say) ---


def test_a_plain_fetch_still_works_after_a_started_branch_is_deleted_upstream(
    repos: Repos,
) -> None:
    """A refspec persisted for the branch made every plain `git fetch` in the
    project fail with "couldn't find remote ref" once origin deleted it."""
    repos.commit_upstream("feature", base="main")
    repos.start("alice", start_branch="feature")
    repos.git(repos.seed, "push", "-q", "origin", ":feature")

    repos.git(repos.workspace, "fetch", "-q", "origin")
    repos.git(repos.workdir("alice"), "fetch", "-q", "origin")


def test_a_branch_deleted_on_origin_starts_from_the_local_copy_and_says_so(
    repos: Repos,
) -> None:
    repos.commit_upstream("feature", base="main")
    repos.git(repos.workspace, "fetch", "-q", "origin", "feature:refs/remotes/origin/feature")
    repos.git(repos.workspace, "branch", "feature", "refs/remotes/origin/feature")
    local = repos.git(repos.workspace, "rev-parse", "refs/heads/feature")
    repos.git(repos.seed, "push", "-q", "origin", ":feature")

    _, base, note = repos.start("alice", start_branch="feature")

    assert repos.head("alice") == local
    assert base == "refs/heads/feature"
    assert note == "feature no longer exists on origin, so this session starts from the local copy."
    assert not repos.has_ref("refs/remotes/origin/feature")


def test_a_branch_deleted_on_origin_is_not_started_from_its_leftover_copy(
    repos: Repos,
) -> None:
    repos.commit_upstream("feature", base="main")
    repos.start("alice", start_branch="feature")
    repos.git(repos.seed, "push", "-q", "origin", ":feature")

    result, _, _ = repos.start("bob", ok=False, start_branch="feature")

    assert result.returncode != 0
    assert "No branch called 'feature' was found locally or on origin." in result.stderr
    assert not repos.has_ref("refs/remotes/origin/feature")


# --- A branch that simply is not on origin is no failure worth a note ---


def test_a_local_only_branch_starts_without_a_note(repos: Repos) -> None:
    local = repos.commit_local()
    repos.git(repos.workspace, "branch", "local-only", local)

    _, base, note = repos.start("alice", start_branch="local-only")

    assert repos.head("alice") == local
    assert base == "refs/heads/local-only"
    assert note is None


def test_a_project_cloned_from_an_empty_repository_starts_without_a_note(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home)
    upstream = tmp_path / "upstream.git"
    repo = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(upstream)], env=env, check=True
    )
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "clone", "-q", str(upstream), str(repo)],
        env=env, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "Moonphase: initial commit"],
        cwd=repo, env=env, check=True,
    )
    script = workspaces.worktree_script(
        str(tmp_path / "sessions" / "alice" / "work"), "moonphase/alice", repo_root=str(repo)
    )

    result = subprocess.run(
        ["bash", "-c", workspaces.wrap_for_login_shell(script)],
        env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    base, note = workspaces.read_worktree_output(result.stdout)
    assert base == "refs/heads/main"
    assert note is None


def test_a_failed_transport_still_says_the_copy_may_be_behind(repos: Repos) -> None:
    repos.commit_upstream("feature", base="main")
    repos.start("alice", start_branch="feature")
    repos.git(
        repos.workspace, "remote", "set-url", "origin", str(repos.tmp / "gone.git")
    )

    _, base, note = repos.start("bob", start_branch="feature")

    assert base == "refs/remotes/origin/feature"
    assert note and "Could not refresh feature from origin" in note
