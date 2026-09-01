"""Writing an uploaded image into a session's container.

No SSH connection or Docker daemon involved: `write_upload` only needs
something that looks like `asyncssh.SSHClientConnection.run`, so a fake
records what it was asked to run rather than actually running it.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from moonphase import sessions


@dataclass
class _FakeConn:
    """Stands in for `asyncssh.SSHClientConnection`, recording each command."""

    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def run(self, command: str, check: bool = False, input: str | None = None):
        self.calls.append((command, input))
        return _FakeResult()


@dataclass
class _FakeResult:
    exit_status: int = 0
    stdout: str = ""
    stderr: str = ""


async def test_write_upload_base64_encodes_bytes_over_the_channel() -> None:
    conn = _FakeConn()
    data = bytes(range(256))  # exercises every byte value, not just ASCII text

    await sessions.write_upload(conn, "proj-container", "/home/dev/sessions/x/uploads/a.png", data)

    assert len(conn.calls) == 1
    command, stdin = conn.calls[0]
    assert "docker exec -i -u dev proj-container sh -c" in command
    assert "base64 -d" in command
    assert "mkdir -p" in command
    assert "chmod 644" in command
    # What actually crosses the wire must decode back to the original bytes.
    assert base64.b64decode(stdin) == data


async def test_write_upload_quotes_a_path_with_spaces() -> None:
    conn = _FakeConn()

    await sessions.write_upload(conn, "c", "/home/dev/sessions/a b/uploads/f.png", b"x")

    command, _stdin = conn.calls[0]
    assert "'/home/dev/sessions/a b/uploads/f.png'" in command
    assert "'/home/dev/sessions/a b/uploads'" in command


# --- sanitise_upload_name -----------------------------------------------------
#
# Whatever this returns becomes one path segment inside the container, so the
# two things that matter are: no directory component survives, and the two
# names that stay dangerous even alone ('.', '..') never come out unchanged.


def test_sanitise_upload_name_takes_the_basename_only() -> None:
    assert sessions.sanitise_upload_name("notes.txt") == "notes.txt"
    assert sessions.sanitise_upload_name("../../etc/passwd") == "passwd"
    assert sessions.sanitise_upload_name("a/b/c.py") == "c.py"
    assert sessions.sanitise_upload_name("C:\\Users\\me\\report.pdf") == "report.pdf"


def test_sanitise_upload_name_refuses_dot_and_dotdot() -> None:
    assert sessions.sanitise_upload_name(".") == "upload"
    assert sessions.sanitise_upload_name("..") == "upload"
    assert sessions.sanitise_upload_name("../..") == "upload"
    assert sessions.sanitise_upload_name("") == "upload"


def test_sanitise_upload_name_keeps_a_leading_dot_for_a_real_dotfile() -> None:
    # Not dangerous on its own — only a bare '.' or '..' is — and someone may
    # genuinely want to hand the agent a .env.example or similar.
    assert sessions.sanitise_upload_name(".env.example") == ".env.example"


def test_sanitise_upload_name_strips_control_characters() -> None:
    assert sessions.sanitise_upload_name("bad\x00name\n.txt") == "bad_name_.txt"


def test_sanitise_upload_name_caps_length() -> None:
    huge = "x" * 500 + ".txt"
    assert len(sessions.sanitise_upload_name(huge)) == 200


# --- unique_filename -----------------------------------------------------------


def test_unique_filename_is_unchanged_when_free() -> None:
    assert sessions.unique_filename([], "notes.txt") == "notes.txt"
    assert sessions.unique_filename(["other.txt"], "notes.txt") == "notes.txt"


def test_unique_filename_numbers_a_collision() -> None:
    assert sessions.unique_filename(["notes.txt"], "notes.txt") == "notes (1).txt"


def test_unique_filename_keeps_counting_past_the_first_collision() -> None:
    existing = ["notes.txt", "notes (1).txt", "notes (2).txt"]
    assert sessions.unique_filename(existing, "notes.txt") == "notes (3).txt"


def test_unique_filename_handles_a_name_with_no_extension() -> None:
    assert sessions.unique_filename(["Dockerfile"], "Dockerfile") == "Dockerfile (1)"


def test_unique_filename_does_not_treat_a_leading_dot_as_an_extension() -> None:
    # ".env" must not become the empty stem "" plus extension ".env" — the
    # numbered variant should still look like a dotfile, not "( 1).env".
    assert sessions.unique_filename([".env"], ".env") == ".env (1)"


# --- list_directory ------------------------------------------------------------


@dataclass
class _FakeExecConn:
    """Stands in for the SSH connection `docker_remote.exec_capture` runs
    against — same shape as `_FakeConn` above, reused here under its real
    call path rather than mocking `exec_capture` itself."""

    stdout: str = ""
    exit_status: int = 0
    calls: list[str] = field(default_factory=list)

    async def run(self, command: str, check: bool = False, input: str | None = None):
        self.calls.append(command)
        return _FakeResult(exit_status=self.exit_status, stdout=self.stdout)


async def test_list_directory_splits_ls_output_into_names() -> None:
    conn = _FakeExecConn(stdout="a.txt\nb.png\n")

    names = await sessions.list_directory(conn, "c", "/workspace")

    assert names == ["a.txt", "b.png"]
    assert "ls -1 -A /workspace" in conn.calls[0]


async def test_list_directory_reports_empty_for_a_directory_that_does_not_exist_yet() -> None:
    conn = _FakeExecConn(stdout="", exit_status=2)

    assert await sessions.list_directory(conn, "c", "/workspace") == []
