"""Resuming, which is what "it survived the reboot" has to mean.

A container comes back on its own because of its restart policy. Everything
inside it does not: tmux is gone, and with it the agent and the conversation.
Bringing the session back at an empty prompt in the right directory would be
technically a restart and practically a loss.
"""

from __future__ import annotations

from moonphase.harness import get as get_harness


def test_a_fresh_session_starts_a_new_conversation() -> None:
    spec = get_harness("claude_code").launch_spec()
    assert spec.command == ["claude"]


def test_resuming_reopens_the_previous_one() -> None:
    spec = get_harness("claude_code").launch_spec(resume=True)
    assert spec.command == ["claude", "--continue"], (
        "a resumed session must pick the conversation back up, not open a blank one"
    )


def test_the_workdir_is_unchanged_by_resuming() -> None:
    # `--continue` reopens the most recent conversation *in the working
    # directory*, so the two are not independent: resuming somewhere else would
    # silently start fresh.
    assert (
        get_harness("claude_code").launch_spec(resume=True).workdir
        == get_harness("claude_code").launch_spec().workdir
    )
