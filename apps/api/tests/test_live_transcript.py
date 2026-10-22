"""Following the live transcript the way the feed does, through a real shell.

What is tested here is the part no parser sees: which file is live, what
counts as a different file, and where a cursor resumes. It runs for real — the
same `sh` snippets the container would run, against a temporary HOME laid out
the way each harness lays out its own — with only the `docker exec` hop taken
out. A quoting slip or a wrong glob therefore fails here, rather than showing
up as a feed that silently stays empty. (The HOME has a space in its path for
the same reason.)

Antigravity is the case that needed it. Every one of its conversations writes
a file called `transcript_full.jsonl`, one directory per conversation, and
which conversation is current is recorded by agy itself in
`cache/last_conversations.json`, keyed by working directory. The records below
are real: written by the agy 1.4.3 binary in a probe session (against a mock
model, so no account was involved) and reproduced byte for byte; the three
used here contain no absolute paths, so nothing needed replacing. How each record
is parsed is the harness's own business and is tested with it; here the
expected events are whatever the harness makes of the same records, so these
tests pin the tailing, not the parser.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from moonphase import docker_remote, ssh, transcript
from moonphase.harness import SessionSpace
from moonphase.harness import get as get_harness
from moonphase.routers import feedsocket
from moonphase.transcript import Cursor

AGY = get_harness("antigravity")
CLAUDE = get_harness("claude_code")

needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None, reason="agy's live-transcript lookup reads its map with jq"
)

# Only ever keys in agy's map; nothing is created at either path.
WORKDIR = "/home/dev/sessions/alice/work"
OTHER_WORKDIR = "/home/dev/sessions/bob/work"

# --- real agy 1.4.3 transcripts ---------------------------------------------
#
# Three conversations from one probe HOME: a two-step one, a six-step one with
# a command and its result, and one started in a different directory.

SHORT = "e3a1bc3a-9c6c-41f5-ade7-f678f66ba7cf"
SHORT_STEPS = [
    {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-10-20T15:47:00Z",
        "content": (
            "<USER_REQUEST>\nsay hi\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe"
            " current local time is:"
            " 2026-10-20T15:47:00Z.\n</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nThe"
            " user changed setting `Model Selection` from None to Gemini 3.1 Pro"
            " (Low). No need to comment on this change if the user doesn't ask"
            " about it. If reporting what model you are, please use a human"
            " readable name instead of the exact string.\n</USER_SETTINGS_CHANGE>"
        ),
    },
    {
        "step_index": 1,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-10-20T15:47:01Z",
        "input_tokens": 100,
        "cache_read_tokens": 0,
        "output_tokens": 10,
        "content": "Print mode hello.",
        "thinking": "Considering the request carefully.",
    },
]

LONG = "6af7e856-2879-4143-b263-70e95e2243d7"
LONG_STEPS = [
    {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-10-20T15:45:05Z",
        "content": (
            "<USER_REQUEST>\nsay hello\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe"
            " current local time is:"
            " 2026-10-20T15:45:05Z.\n</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nThe"
            " user changed setting `Model Selection` from None to Gemini 3.1 Pro"
            " (Low). No need to comment on this change if the user doesn't ask"
            " about it. If reporting what model you are, please use a human"
            " readable name instead of the exact string.\n</USER_SETTINGS_CHANGE>"
        ),
    },
    {
        "step_index": 1,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-10-20T15:45:05Z",
        "input_tokens": 100,
        "cache_read_tokens": 0,
        "output_tokens": 10,
        "content": "Hello from the mock model. All done.",
        "thinking": "Considering the request carefully.",
    },
    {
        "step_index": 2,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-10-20T15:45:54Z",
        "content": (
            "<USER_REQUEST>\ncreate a probe"
            " file\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time"
            " is: 2026-10-20T15:45:54Z.\n</ADDITIONAL_METADATA>"
        ),
    },
    {
        "step_index": 3,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-10-20T15:45:54Z",
        "input_tokens": 100,
        "cache_read_tokens": 0,
        "output_tokens": 10,
        "tool_calls": [
            {
                "name": "run_command",
                "args": {
                    "CommandLine": "touch probe-file.txt",
                    "WaitMsBeforeAsync": 5000,
                    "toolSummary": "Probe file creation",
                },
            },
        ],
    },
    {
        "step_index": 4,
        "source": "MODEL",
        "type": "GENERIC",
        "status": "DONE",
        "created_at": "2026-10-20T15:45:54Z",
        "content": (
            "Created At: 2026-10-20T15:45:54Z\nCompleted At:"
            " 2026-10-20T15:46:27Z\nCommand exited with code 0.\n\nNo output from"
            " the command."
        ),
    },
    {
        "step_index": 5,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-10-20T15:46:27Z",
        "input_tokens": 100,
        "cache_read_tokens": 0,
        "output_tokens": 10,
        "content": "Created the file.",
    },
]

ELSEWHERE = "987464b9-a51e-4821-844c-4e5e844a8260"
ELSEWHERE_STEPS = [
    {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-10-20T15:59:54Z",
        "content": (
            "<USER_REQUEST>\nhi\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe"
            " current local time is:"
            " 2026-10-20T15:59:54Z.\n</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nThe"
            " user changed setting `Model Selection` from None to Gemini 3.1 Pro"
            " (Low). No need to comment on this change if the user doesn't ask"
            " about it. If reporting what model you are, please use a human"
            " readable name instead of the exact string.\n</USER_SETTINGS_CHANGE>"
        ),
    },
    {
        "step_index": 1,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-10-20T15:59:54Z",
        "input_tokens": 100,
        "cache_read_tokens": 0,
        "output_tokens": 10,
        "content": "Untrusted print.",
    },
]


# --- the shell, minus the container -------------------------------------------


async def _run_here(_conn: Any, _container: str, command: list[str], **_: Any):
    """`docker_remote.exec_capture` without the `docker exec`: run it here."""
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await process.communicate()
    except asyncio.CancelledError:
        # The feed socket is stopped by cancelling it, which can land while
        # it is asking which file is live. Reap the shell here, on this loop.
        process.kill()
        await process.communicate()
        raise
    return ssh.CommandResult(
        exit_status=process.returncode if process.returncode is not None else -1,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )


@pytest.fixture
def here(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_remote, "exec_capture", _run_here)


def _line(step: dict[str, Any]) -> str:
    # Exactly how agy writes a step: compact, key order kept, UTF-8 as is.
    # Checked against the probe's own files, which these reproduce exactly.
    return json.dumps(step, separators=(",", ":"), ensure_ascii=False) + "\n"


def _identity(conversation: str) -> str:
    return f"{conversation}/.system_generated/logs/transcript_full.jsonl"


def _parsed(conversation: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the harness makes of these steps when told which file they are in."""
    events = [
        event.to_dict()
        for step in steps
        for event in AGY.parse_transcript_record(
            json.loads(_line(step)), source=_identity(conversation)
        )
    ]
    # Steps that parse to nothing would let every comparison below pass on
    # an empty feed.
    assert events
    return events


def _dicts(events: list[transcript.TranscriptEvent]) -> list[dict[str, Any]]:
    return [event.to_dict() for event in events]


class _AgyHome:
    """A session HOME laid out the way agy 1.4.3 lays out its own."""

    def __init__(self, root: Path) -> None:
        self.home = root / "a home"
        self.app = self.home / ".gemini" / "antigravity-cli"
        self.space = SessionSpace(home=str(self.home), workdir=WORKDIR)

    def transcript(self, conversation: str) -> Path:
        return self.app / "brain" / _identity(conversation)

    def size(self, conversation: str) -> int:
        return self.transcript(conversation).stat().st_size

    def touch_later(self, conversation: str) -> None:
        """Make this the newest file on disk, whatever the clock's resolution."""
        later = time.time() + 60
        os.utime(self.transcript(conversation), (later, later))

    def write(self, conversation: str, steps: list[dict[str, Any]]) -> None:
        self.write_raw(conversation, "".join(_line(step) for step in steps))

    def write_raw(self, conversation: str, text: str) -> None:
        path = self.transcript(conversation)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def remember(self, workdir: str, conversation: str) -> None:
        """What agy does on a conversation's first message in `workdir`."""
        cache = self.app / "cache" / "last_conversations.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        known = json.loads(cache.read_text()) if cache.exists() else {}
        known[workdir] = conversation
        # Pretty-printed, as agy writes it.
        cache.write_text(json.dumps(known, indent=2))

    async def read(self, cursor: str | None = None) -> transcript.TranscriptPage:
        return await transcript.read(None, "container", AGY, cursor=cursor, space=self.space)


# --- Antigravity --------------------------------------------------------------


@needs_jq
async def test_nothing_is_followed_until_agy_records_a_conversation_here(
    here: None, tmp_path: Path
) -> None:
    """agy records a working directory's conversation on its first message.
    Before that this session has nothing to show, and the conversations that
    do exist are not its own: guessing "the newest file" could put another
    worktree's conversation, or a sub-agent's, in this session's feed."""
    agy = _AgyHome(tmp_path)

    # A HOME agy has never run in.
    assert (await agy.read()).available is False

    # Conversations on disk, recorded for nowhere.
    agy.write(LONG, LONG_STEPS)
    agy.write(ELSEWHERE, ELSEWHERE_STEPS)
    assert (await agy.read()).available is False

    # Recorded, but for a different working directory.
    agy.remember(OTHER_WORKDIR, ELSEWHERE)
    page = await agy.read()
    assert page.available is False
    assert page.events == []
    assert page.cursor == ""


@needs_jq
async def test_the_conversation_agy_recorded_for_this_worktree_is_the_one_read(
    here: None, tmp_path: Path
) -> None:
    """The newest file on disk is another directory's conversation; the map is
    what says which one is this session's."""
    agy = _AgyHome(tmp_path)
    agy.write(LONG, LONG_STEPS)
    agy.remember(WORKDIR, LONG)
    agy.write(ELSEWHERE, ELSEWHERE_STEPS)
    agy.remember(OTHER_WORKDIR, ELSEWHERE)
    agy.touch_later(ELSEWHERE)

    page = await agy.read()

    assert page.available is True
    assert _dicts(page.events) == _parsed(LONG, LONG_STEPS)
    assert page.cursor == f"{_identity(LONG)}:{agy.size(LONG)}"


@needs_jq
async def test_a_cursor_carries_on_through_the_same_conversation(
    here: None, tmp_path: Path
) -> None:
    """Each poll ships only what is new, and a step agy is half-way through
    writing waits for the rest rather than being dropped or misread."""
    agy = _AgyHome(tmp_path)
    agy.write(LONG, LONG_STEPS[:3])
    agy.remember(WORKDIR, LONG)

    first = await agy.read()
    assert _dicts(first.events) == _parsed(LONG, LONG_STEPS[:3])

    again = await agy.read(first.cursor)
    assert again.events == []
    assert again.cursor == first.cursor

    agy.write(LONG, LONG_STEPS[3:4])
    step = await agy.read(again.cursor)
    assert _dicts(step.events) == _parsed(LONG, LONG_STEPS[3:4])

    # Split a genuine line, so the halves really do make a record again.
    whole = _line(LONG_STEPS[4])
    agy.write_raw(LONG, whole[:40])
    partial = await agy.read(step.cursor)
    assert partial.events == []
    assert partial.cursor == step.cursor

    agy.write_raw(LONG, whole[40:])
    agy.write(LONG, LONG_STEPS[5:])
    rest = await agy.read(partial.cursor)
    assert _dicts(rest.events) == _parsed(LONG, LONG_STEPS[4:])
    assert rest.cursor == f"{_identity(LONG)}:{agy.size(LONG)}"


@needs_jq
async def test_a_new_conversation_is_a_new_file_though_its_name_is_the_same(
    here: None, tmp_path: Path
) -> None:
    """Every agy transcript is called transcript_full.jsonl. Told apart by
    name, a new conversation would look like the old one carrying on, and the
    old file's offset would be applied to the new one: reading from the middle
    of a line and dropping everything before it. Set up so that the size
    cannot give it away either — the new file is longer than the old offset —
    leaving the file's path as the only way to notice."""
    agy = _AgyHome(tmp_path)
    agy.write(SHORT, SHORT_STEPS)
    agy.remember(WORKDIR, SHORT)
    before = await agy.read()

    agy.write(LONG, LONG_STEPS)
    agy.remember(WORKDIR, LONG)
    assert agy.transcript(SHORT).name == agy.transcript(LONG).name
    assert Cursor.decode(before.cursor).offset < agy.size(LONG)

    after = await agy.read(before.cursor)

    # The new conversation from its first step, nothing skipped.
    assert _dicts(after.events) == _parsed(LONG, LONG_STEPS)
    assert Cursor.decode(after.cursor).filename == _identity(LONG)

    # And going back to the earlier one (agy's /resume) is a switch too.
    agy.remember(WORKDIR, SHORT)
    back = await agy.read(after.cursor)
    assert _dicts(back.events) == _parsed(SHORT, SHORT_STEPS)
    assert Cursor.decode(back.cursor).filename == _identity(SHORT)


@needs_jq
async def test_event_ids_stay_unique_across_conversations(
    here: None, tmp_path: Path
) -> None:
    """Both conversations number their steps from 0, and the web client keeps
    the first event it saw for an id and drops any later one with the same id.
    Ids made from the step alone would make the new conversation's messages
    vanish behind the old one's; the file each came from keeps them apart."""
    agy = _AgyHome(tmp_path)
    agy.write(SHORT, SHORT_STEPS)
    agy.remember(WORKDIR, SHORT)
    first = await agy.read()

    agy.write(LONG, LONG_STEPS)
    agy.remember(WORKDIR, LONG)
    second = await agy.read(first.cursor)

    assert first.events and second.events
    ids = [event.id for event in first.events + second.events]
    assert len(ids) == len(set(ids))


# --- the feed socket ------------------------------------------------------------


class _Socket:
    """Collects what the stream pushes."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def events(self) -> list[dict[str, Any]]:
        pushed = [message for message in self.sent if message["type"] == "events"]
        return [event for message in pushed for event in message["events"]]


class _Tail:
    """`docker exec -i <container> tail -c +N -f <path>`, run here instead."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.stdout = process.stdout

    def close(self) -> None:
        if self.process.returncode is None:
            self.process.kill()


@pytest.fixture
async def tails(monkeypatch: pytest.MonkeyPatch):
    """Every `tail -f` the stream starts, as (argv, process)."""
    started: list[tuple[list[str], asyncio.subprocess.Process]] = []

    async def create_process(_target: Any, command: str, **_: Any) -> _Tail:
        argv = shlex.split(command)
        assert argv[:3] == ["docker", "exec", "-i"], command
        process = await asyncio.create_subprocess_exec(
            *argv[4:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        started.append((argv[4:], process))
        return _Tail(process)

    monkeypatch.setattr(ssh.pool, "create_process", create_process)
    yield started
    for _argv, process in started:
        if process.returncode is None:
            process.kill()
        # Drained as well as reaped, so its pipe is closed on this loop rather
        # than by the garbage collector after the loop has gone.
        await process.communicate()


async def _until(condition: Callable[[], bool], what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


@needs_jq
async def test_the_feed_socket_follows_agy_into_a_new_conversation(
    here: None,
    tails: list[tuple[list[str], asyncio.subprocess.Process]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tail -f` on a file that is still there never ends, so the socket only
    leaves a conversation when it sees that the live file is a different one.
    By name it never would: this is the same switch as above, made while the
    socket is following, and it must pick up from the page it was handed
    without sending that page again."""
    monkeypatch.setattr(feedsocket, "ROTATION_INTERVAL_SECONDS", 0.05)
    agy = _AgyHome(tmp_path)
    agy.write(LONG, LONG_STEPS[:4])
    agy.remember(WORKDIR, LONG)
    page = await agy.read()

    socket = _Socket()
    stream = asyncio.create_task(
        feedsocket._stream_transcript(
            socket, None, "container", AGY, page.cursor, agy.space, None
        )
    )
    try:
        # Carries on from the page: the rest of the conversation, once.
        agy.write(LONG, LONG_STEPS[4:])
        expected = _parsed(LONG, LONG_STEPS[4:])
        await _until(lambda: len(socket.events()) >= len(expected), "the rest of it")
        assert socket.events() == expected
        argv, _process = tails[0]
        assert argv == [
            "tail", "-c", f"+{Cursor.decode(page.cursor).offset + 1}", "-f",
            str(agy.transcript(LONG)),
        ]

        # A new conversation, under the same file name: followed from its start.
        agy.write(SHORT, SHORT_STEPS)
        agy.remember(WORKDIR, SHORT)
        expected += _parsed(SHORT, SHORT_STEPS)
        await _until(lambda: len(socket.events()) >= len(expected), "the new conversation")
        assert socket.events() == expected
        assert tails[-1][0] == ["tail", "-c", "+1", "-f", str(agy.transcript(SHORT))]

        ids = [event["id"] for event in socket.events()]
        assert len(ids) == len(set(ids))
    finally:
        stream.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stream


# --- Claude Code: unchanged -------------------------------------------------------


def _claude_line(uid: str, text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uid,
            "timestamp": "2026-08-17T10:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    ) + "\n"


class _ClaudeHome:
    """A session HOME with Claude Code's flat directory of transcripts."""

    def __init__(self, root: Path) -> None:
        self.space = SessionSpace(home=str(root / "a home"), workdir="/workspace")
        self.directory = Path(CLAUDE.transcript_dir(self.space))
        self.directory.mkdir(parents=True)
        self._clock = time.time() - 600

    def write(self, name: str, uid: str, text: str) -> int:
        """Append a line to `name`, make it the newest file, return its size."""
        path = self.directory / name
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_claude_line(uid, text))
        # One file newer than the last, a minute apart: `ls -t` sorts by
        # mtime, and files written within one clock tick would tie.
        self._clock += 60
        os.utime(path, (self._clock, self._clock))
        return path.stat().st_size

    async def read(self, cursor: str | None = None) -> transcript.TranscriptPage:
        return await transcript.read(
            None, "container", CLAUDE, cursor=cursor, space=self.space
        )


async def test_claude_code_still_follows_its_newest_file_by_name(
    here: None, tmp_path: Path
) -> None:
    """Claude Code writes one `<session-id>.jsonl` per conversation into one
    directory, and the newest is the live one. Its cursor is still the bare
    file name and an offset — byte for byte what it was before transcripts
    could be nested — so a cursor a phone is holding across an upgrade
    carries on rather than replaying the tail."""
    claude = _ClaudeHome(tmp_path)
    claude.write("older-session.jsonl", "o0", "from the older session")
    size = claude.write("newer-session.jsonl", "n0", "newer 0")

    page = await claude.read()
    assert [e.text for e in page.events] == ["newer 0"]
    assert page.cursor == f"newer-session.jsonl:{size}"

    # A cursor exactly as the previous version minted it.
    legacy = Cursor(filename="newer-session.jsonl", offset=size).encode()
    claude.write("newer-session.jsonl", "n1", "newer 1")
    step = await claude.read(legacy)
    assert [e.text for e in step.events] == ["newer 1"]

    # A newer file is still a new conversation, read from its start.
    size = claude.write("third-session.jsonl", "t0", "third 0")
    rotated = await claude.read(step.cursor)
    assert [e.text for e in rotated.events] == ["third 0"]
    assert rotated.cursor == f"third-session.jsonl:{size}"
