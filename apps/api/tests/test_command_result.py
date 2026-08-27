"""`CommandResult.check()` must never raise an error that says nothing.

A command killed by a signal (an OOM kill, most often) produces no exit
status asyncssh can report and usually no captured output either — `run()`
reports that case as exit -1. The old callers built their own message from
`(stderr or stdout)` with no fallback, so that case surfaced as "Could not
create a working directory for session 'x': " with nothing after the colon,
which is what this pins down.
"""

from __future__ import annotations

import pytest

from moonphase.ssh import CommandResult, SSHError


def test_a_normal_failure_includes_the_command_s_own_output() -> None:
    result = CommandResult(exit_status=1, stdout="", stderr="fatal: no such branch")
    with pytest.raises(SSHError, match=r"failed \(exit 1\): fatal: no such branch"):
        result.check("Doing the thing")


def test_no_output_at_all_still_says_something() -> None:
    result = CommandResult(exit_status=2, stdout="", stderr="")
    with pytest.raises(SSHError, match=r"failed \(exit 2\): \(no output\)"):
        result.check("Doing the thing")


def test_exit_status_negative_one_is_named_as_a_likely_kill() -> None:
    """-1 is what `ssh.run()` reports when asyncssh saw no exit status —
    i.e. the process was killed by a signal rather than exiting."""
    result = CommandResult(exit_status=-1, stdout="", stderr="")
    with pytest.raises(SSHError, match="killed rather than exiting"):
        result.check("Doing the thing")
    with pytest.raises(SSHError, match="out of memory"):
        result.check("Doing the thing")


def test_a_success_does_not_raise() -> None:
    CommandResult(exit_status=0, stdout="ok", stderr="").check("Doing the thing")
