"""Session activity classification.

These rules decide when someone's phone buzzes, so the bar is that a false
positive is worse than a slow true positive: a notification that fires while
the agent is still working teaches people to mute Moonphase, after which no
notification works at all.

Pure functions, so the rules are exercised directly rather than through a
container.
"""

from __future__ import annotations

from moonphase.activity import (
    IDLE_AFTER_SECONDS,
    PANE_TAIL_LINES,
    ActivitySignals,
    ActivityState,
    classify,
    notification_for,
)
from moonphase.harness import get as get_harness

CLAUDE = get_harness("claude_code").activity_signals()

WORKING_PANE = """
⏺ Read(src/auth.py)
  ⎿ 240 lines

⏺ Now updating the token refresh logic…
"""

PERMISSION_PANE = """
⏺ Edit(src/auth.py)

 Do you want to make this edit to auth.py?
 ❯ 1. Yes
   2. No, tell Claude what to do differently

 Enter to confirm · Esc to cancel
"""

TRUST_PANE = """
 Quick safety check: Is this a project you created or one you trust?

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""

IDLE_PANE = """
⏺ Done. The refresh logic now retries once before failing.

╭──────────────────────────────────────────╮
│ >                                        │
╰──────────────────────────────────────────╯
"""


def test_a_changed_pane_is_working() -> None:
    snapshot = classify(
        WORKING_PANE, signals=CLAUDE, previous_digest="something-else", still_for_seconds=0
    )
    assert snapshot.state is ActivityState.WORKING


def test_the_same_pane_twice_is_not_working() -> None:
    first = classify(IDLE_PANE, signals=CLAUDE, previous_digest=None, still_for_seconds=0)
    second = classify(
        IDLE_PANE,
        signals=CLAUDE,
        previous_digest=first.digest,
        still_for_seconds=IDLE_AFTER_SECONDS + 1,
    )
    assert second.state is ActivityState.IDLE


def test_a_still_pane_is_not_idle_until_it_has_been_still_a_while() -> None:
    """A model composing a long answer can leave the terminal untouched.

    Calling that idle would notify mid-thought, which is precisely the false
    positive that gets notifications turned off.
    """
    first = classify(IDLE_PANE, signals=CLAUDE, previous_digest=None, still_for_seconds=0)
    soon = classify(
        IDLE_PANE, signals=CLAUDE, previous_digest=first.digest, still_for_seconds=5
    )
    assert soon.state is ActivityState.WORKING


def test_a_permission_prompt_is_awaiting_input_immediately() -> None:
    """No grace period: the agent is definitively blocked."""
    first = classify(
        PERMISSION_PANE, signals=CLAUDE, previous_digest=None, still_for_seconds=0
    )
    still = classify(
        PERMISSION_PANE, signals=CLAUDE, previous_digest=first.digest, still_for_seconds=1
    )
    assert still.state is ActivityState.AWAITING_INPUT
    assert still.detail and "Do you want to" in still.detail


def test_a_selection_prompt_is_recognised() -> None:
    first = classify(TRUST_PANE, signals=CLAUDE, previous_digest=None, still_for_seconds=0)
    still = classify(
        TRUST_PANE, signals=CLAUDE, previous_digest=first.digest, still_for_seconds=1
    )
    assert still.state is ActivityState.AWAITING_INPUT


def test_an_explicit_busy_marker_beats_stillness() -> None:
    signals = ActivitySignals(prompt_patterns=(r"\?",), busy_patterns=("esc to interrupt",))
    pane = "Thinking… (esc to interrupt)\nShall I continue?"
    first = classify(pane, signals=signals, previous_digest=None, still_for_seconds=0)
    still = classify(
        pane,
        signals=signals,
        previous_digest=first.digest,
        still_for_seconds=IDLE_AFTER_SECONDS + 60,
    )
    assert still.state is ActivityState.WORKING


def test_digest_ignores_scrollback_above_the_visible_tail() -> None:
    """Only the tail is hashed.

    Digesting the whole scrollback would change every time an old line scrolls
    away, so a finished session would look busy forever.
    """
    # Longer than the tail window, so the differing prefix is genuinely above it.
    tail = "\n".join(f"line {i}" for i in range(PANE_TAIL_LINES + 5))
    a = classify(
        "scrolled away\n" + tail, signals=CLAUDE, previous_digest=None, still_for_seconds=0
    )
    b = classify(
        "something else entirely\n" + tail,
        signals=CLAUDE,
        previous_digest=None,
        still_for_seconds=0,
    )
    assert a.digest == b.digest

    # But a change inside the visible tail must still register.
    changed = classify(
        "scrolled away\n" + tail + "\nnew output",
        signals=CLAUDE,
        previous_digest=a.digest,
        still_for_seconds=0,
    )
    assert changed.digest != a.digest


# --- what actually reaches the user ----------------------------------------


def test_notifies_when_work_stops_for_a_question() -> None:
    message = notification_for(
        ActivityState.WORKING, ActivityState.AWAITING_INPUT, "Do you want to edit?", "api"
    )
    assert message is not None
    title, body = message
    assert "api" in title
    assert "Do you want to edit?" in body


def test_notifies_when_work_finishes() -> None:
    message = notification_for(
        ActivityState.WORKING, ActivityState.IDLE, None, "landing-page"
    )
    assert message is not None
    assert "finished" in message[0]


def test_does_not_notify_when_the_user_starts_something() -> None:
    """Entering `working` is almost always the user typing.

    Notifying on it would buzz on every prompt they send from the terminal
    they are already looking at.
    """
    assert notification_for(ActivityState.IDLE, ActivityState.WORKING, None, "api") is None
    assert (
        notification_for(ActivityState.AWAITING_INPUT, ActivityState.WORKING, None, "api")
        is None
    )


def test_does_not_notify_on_repeated_idle() -> None:
    # Idle → idle is not a transition; only leaving `working` is.
    assert notification_for(ActivityState.IDLE, ActivityState.IDLE, None, "api") is None
    assert notification_for(ActivityState.UNKNOWN, ActivityState.IDLE, None, "api") is None


def test_does_not_notify_when_a_container_stops() -> None:
    """Stopping a project is something the user just did on purpose."""
    assert notification_for(ActivityState.WORKING, ActivityState.STOPPED, None, "api") is None
