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
