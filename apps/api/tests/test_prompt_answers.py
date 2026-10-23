"""Answering a prompt from the phone without approving the next one.

Every answer used to be typed and then followed by Enter. Antigravity's picker
does not wait for the Enter: pressing an option's digit picks and confirms it
at once (checked against the shipped 1.4.3 binary), so the Enter lands on
whatever it draws next. That is usually the empty input box, where it was seen
to do nothing — but if the next thing is another permission prompt, the Enter
confirms that prompt's highlighted "1. Yes" instead.

No container here: a fake stands in for `docker exec` and records each tmux
command, which is exactly what is under test — whether an Enter is sent. The
panes are transcribed from captures of agy 1.4.3 running in a real tmux pane
(rules shortened).
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from moonphase import activity, docker_remote, queries, runtime, sessions, ssh
from moonphase.activity import Prompt, offers, parse_prompt
from moonphase.auth import Principal
from moonphase.harness import available
from moonphase.harness import get as get_harness
from moonphase.routers import feed, review
from moonphase.schemas import AnswerIn

AGY = get_harness("antigravity")
SESSION = sessions.DEFAULT_SESSION

# A command waiting for permission, as agy 1.4.3 draws it.
AGY_PERMISSION = """\
● Ran (touch probe-file.txt) (ctrl+o to expand)
Command
────────────────────────────────────────────────────────────────────────
Requesting permission for:
   touch probe-file.txt
Run this command?
> 1. Yes, run command
  2. Yes, and always allow in this conversation for commands that start with 'touch'
  3. Yes, and always allow for commands that start with 'touch' (Persist to settings.json)
  4. No, cancel
  ↑/↓ Navigate · tab Amend · ctrl+g edit/expand command
esc to cancel
"""

# The agent asking a question of its own (its ask_question tool).
AGY_QUESTION = """\
? Which color do you prefer?
Question
────
Question 1/1: Which color do you prefer?
> 1. (Recommended) Blue
  2. Green
  3. Write-in...
  ↑/↓ Navigate · enter Select · esc Skip
esc to cancel
"""

# Waiting for the next message, with the previous answer in the scrollback.
AGY_IDLE = """\
● Ran (touch digit-test.txt) (ctrl+o to expand)
  Digit done.
────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────
? for shortcuts                                       Gemini 3.1 Pro · low
"""

CLAUDE_PERMISSION = """
⏺ Edit(src/auth.py)

 Do you want to make this edit to auth.py?
 ❯ 1. Yes
   2. No, tell Claude what to do differently

 Enter to confirm · Esc to cancel
"""

CAPTURE = ["tmux", "capture-pane", "-p", "-t", SESSION, "-S", "-80"]
ENTER = ["tmux", "send-keys", "-t", SESSION, "Enter"]


def _typed(key: str) -> list[str]:
    return ["tmux", "send-keys", "-t", SESSION, "-l", key]


class _Container:
    """Stands in for `docker exec`, showing one pane and recording every
    command it is asked to run."""

    def __init__(self, pane: str) -> None:
        self.pane = pane
        self.calls: list[list[str]] = []

    async def exec_capture(
        self, _conn: Any, _container: str, command: list[str], **_: Any
    ) -> ssh.CommandResult:
        self.calls.append(list(command))
        stdout = self.pane if command[:2] == ["tmux", "capture-pane"] else ""
        return ssh.CommandResult(exit_status=0, stdout=stdout, stderr="")


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch):
    def showing(pane: str) -> _Container:
        fake = _Container(pane)
        monkeypatch.setattr(docker_remote, "exec_capture", fake.exec_capture)
        return fake

    return showing


# --- which keys answer on their own -------------------------------------------


def test_only_an_option_the_prompt_shows_is_one_it_offers() -> None:
    prompt = parse_prompt(AGY_PERMISSION, AGY.activity_signals())
    assert prompt is not None
    assert [option["key"] for option in prompt.options] == ["1", "2", "3", "4"]

    assert all(offers(prompt, key) for key in ["1", "2", "3", "4"])
    # Not an option here; not a bare number; not a number at all.
    for key in ["5", "0", "12", " 2", "2\n", "2.", "yes", "", "²"]:
        assert not offers(prompt, key), key
    assert not offers(None, "1")
    assert not offers(Prompt(question="Do you trust this folder?"), "1")


# --- Antigravity ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("pane", "key"),
    [(AGY_PERMISSION, key) for key in "1234"] + [(AGY_QUESTION, key) for key in "123"],
)
async def test_a_tapped_option_is_sent_without_enter_where_the_digit_confirms_it(
    container, pane: str, key: str
) -> None:
    """The digit is the whole answer. An Enter after it would be the next
    screen's to deal with, and on a second permission prompt that is an
    approval nobody saw."""
    fake = container(pane)

    await activity.send_answer(None, "c", AGY, key, session=SESSION)

    assert fake.calls == [CAPTURE, _typed(key)]


async def test_a_number_sent_while_a_prompt_shows_never_gets_an_enter(container) -> None:
    """Even one the parser did not read off the pane. agy acts on a digit
    whichever option it names, so whether the Enter is safe cannot depend on
    the parser having understood every line of the prompt — at a phone's
    width it once read only the last of four."""
    fake = container(AGY_PERMISSION)

    await activity.send_answer(None, "c", AGY, "7", session=SESSION)

    assert fake.calls == [CAPTURE, _typed("7")]


async def test_a_number_with_no_prompt_on_screen_is_a_message(container) -> None:
    """The previous prompt's options are still in the scrollback, but nothing
    is asking now: a "2" typed here is a message to the agent, and a message
    is not sent until Enter."""
    fake = container(AGY_IDLE)

    await activity.send_answer(None, "c", AGY, "2", session=SESSION)

    assert fake.calls == [CAPTURE, _typed("2"), ENTER]


async def test_a_typed_message_is_refused_while_a_choice_is_showing(container) -> None:
    """Its Enter would confirm whatever the picker highlights, and that starts
    out as "1. Yes" — approving a command nobody saw."""
    fake = container(AGY_PERMISSION)

    with pytest.raises(activity.NeedsTerminal, match="waiting on a choice"):
        await activity.send_answer(None, "c", AGY, "no, use pytest instead", session=SESSION)

    assert fake.calls == [CAPTURE]


# agy's folder-trust question, as it draws it on a session's first start.
AGY_TRUST = """\
Accessing workspace:

/home/dev/sessions/alice/work

Do you trust the contents of this project?

Antigravity CLI requires permission to read, edit, and execute files here.

> Yes, I trust this folder
  No, exit

  ↑/↓ Navigate · enter Confirm
"""


@pytest.mark.parametrize("key", ["no", "1", "yes please"])
async def test_nothing_is_typed_into_a_question_only_the_terminal_can_answer(
    container, key: str
) -> None:
    """Folder trust moves only on the arrow keys, and Enter takes whatever is
    highlighted — "Yes, I trust this folder" to begin with. A "no" sent from
    the feed would have arrived with that Enter and trusted the folder, so it
    is refused and nothing at all is typed."""
    fake = container(AGY_TRUST)

    with pytest.raises(activity.NeedsTerminal, match="trust the contents"):
        await activity.send_answer(None, "c", AGY, key, session=SESSION)

    assert fake.calls == [CAPTURE]


# --- every other harness: unchanged ----------------------------------------------


@pytest.mark.parametrize(
    "harness",
    [h for h in available() if not h.activity_signals().select_submits],
    ids=lambda h: str(h.kind),
)
async def test_an_option_is_followed_by_enter_where_the_digit_alone_does_not_confirm(
    container, harness
) -> None:
    """Exactly the keystrokes these harnesses have always been sent, and their
    pane is not read first: nothing about answering them has changed."""
    fake = container(CLAUDE_PERMISSION)

    await activity.send_answer(None, "c", harness, "1", session=SESSION)

    assert fake.calls == [_typed("1"), ENTER]


# --- through the two routes that answer ------------------------------------------


@dataclass
class _Context:
    harness: str
    container: str = "mp-answers"
    target: object = None


PRINCIPAL = Principal(user_id="u1", email=None, claims={"sub": "u1"})


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch):
    """A project of the given harness, reachable without a database or SSH."""

    def of(kind: str) -> None:
        async def load_project_context(_claims: Any, _project_id: Any, **_: Any) -> _Context:
            return _Context(harness=kind)

        async def load_session_space(
            _claims: Any, _project_id: Any, _name: str
        ) -> tuple[Any, dict[str, Any]]:
            return (None, {"harness": kind})

        async def get_pool(_target: Any) -> object:
            return object()

        async def get_sessions(_conn: Any, _project_id: Any) -> list[dict[str, Any]]:
            return [{"tmux_session": SESSION, "is_mine": True}]

        @contextlib.asynccontextmanager
        async def user_session(_claims: Any):
            yield None

        monkeypatch.setattr(runtime, "load_project_context", load_project_context)
        monkeypatch.setattr(runtime, "load_session_space", load_session_space)
        monkeypatch.setattr(ssh.pool, "get", get_pool)
        monkeypatch.setattr(queries, "get_sessions", get_sessions)
        monkeypatch.setattr(review, "user_session", user_session)

    return of


@pytest.mark.parametrize(
    ("kind", "pane", "expected"),
    [
        ("antigravity", AGY_PERMISSION, [CAPTURE, _typed("2")]),
        ("claude_code", CLAUDE_PERMISSION, [_typed("2"), ENTER]),
    ],
)
async def test_the_feed_answers_by_the_projects_harness(
    project, container, kind: str, pane: str, expected: list[list[str]]
) -> None:
    project(kind)
    fake = container(pane)

    await feed.answer(uuid.uuid4(), AnswerIn(key="2"), session=None, principal=PRINCIPAL)

    assert fake.calls == expected


@pytest.mark.parametrize(
    ("kind", "pane", "expected"),
    [
        ("antigravity", AGY_PERMISSION, [CAPTURE, _typed("2")]),
        ("claude_code", CLAUDE_PERMISSION, [_typed("2"), ENTER]),
    ],
)
async def test_answering_from_the_waiting_list_follows_the_same_rule(
    project, container, kind: str, pane: str, expected: list[list[str]]
) -> None:
    project(kind)
    fake = container(pane)

    await review.answer_from_anywhere(
        uuid.uuid4(), SESSION, {"key": " 2 "}, principal=PRINCIPAL
    )

    assert fake.calls == expected


async def test_a_typed_message_with_no_prompt_showing_is_sent_with_enter(container) -> None:
    fake = container(AGY_IDLE)

    await activity.send_answer(None, "c", AGY, "no, use pytest instead", session=SESSION)

    assert fake.calls == [CAPTURE, _typed("no, use pytest instead"), ENTER]


async def test_an_agent_quoting_a_terminal_only_question_does_not_lock_the_feed(
    container,
) -> None:
    """The arrow-key screens replace agy's input box; with the box showing,
    the phrase is scrollback."""
    pane = "Do you trust the contents of this project?\n" + AGY_IDLE
    fake = container(pane)

    await activity.send_answer(None, "c", AGY, "carry on", session=SESSION)

    assert fake.calls == [CAPTURE, _typed("carry on"), ENTER]
    assert activity.parse_prompt(pane, AGY.activity_signals()) is None
