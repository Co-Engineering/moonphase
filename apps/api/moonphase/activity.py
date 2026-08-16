"""Is the agent working, blocked, or done?

Answering this is what lets someone actually walk away. The signal has to work
without a harness cooperating, because Moonphase does not control what the
agent prints.

So the primary signal is change: hash the terminal, and if it differs from last
time, something is happening. That needs no knowledge of any harness's UI and
cannot be broken by a wording change upstream.

Prompt patterns only refine a pane that has already gone still, distinguishing
"stopped because it finished" from "stopped because it is asking you
something". Getting a pattern wrong therefore degrades the label, never the
detection.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from enum import StrEnum

import asyncssh

from . import docker_remote, sessions
from .harness import Harness

log = logging.getLogger(__name__)

# How long a still pane must stay still before we call it idle rather than
# "thinking quietly". Long enough not to fire while a model streams slowly.
IDLE_AFTER_SECONDS = 45

# Only the tail matters: a scrollback-sized digest changes when old lines
# scroll off, which would read as activity forever.
PANE_TAIL_LINES = 40


class ActivityState(StrEnum):
    UNKNOWN = "unknown"
    WORKING = "working"
    AWAITING_INPUT = "awaiting_input"
    IDLE = "idle"
    STOPPED = "stopped"


@dataclass
class ActivitySignals:
    """Per-harness hints for interpreting a still terminal."""

    # A question the harness is blocked on.
    prompt_patterns: tuple[str, ...] = ()
    # Explicit evidence of work in progress, when a harness offers it.
    busy_patterns: tuple[str, ...] = ()


@dataclass
class Snapshot:
    state: ActivityState
    digest: str
    detail: str | None = None


def _digest(pane: str) -> str:
    tail = "\n".join(pane.rstrip().splitlines()[-PANE_TAIL_LINES:])
    return hashlib.sha256(tail.encode()).hexdigest()[:16]


def _match(pane: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        found = re.search(pattern, pane, re.IGNORECASE)
        if found:
            return found.group(0).strip()
    return None


def _question_excerpt(pane: str, matched: str) -> str:
    """The line the prompt appeared on, trimmed for a notification body."""
    for line in reversed(pane.splitlines()):
        if matched.lower() in line.lower():
            cleaned = " ".join(line.split())
            return cleaned[:140]
    return matched[:140]


def classify(
    pane: str,
    *,
    signals: ActivitySignals,
    previous_digest: str | None,
    still_for_seconds: float,
) -> Snapshot:
    """Turn a pane into a state. Pure, so the rules are testable directly."""
    digest = _digest(pane)

    if digest != previous_digest:
        return Snapshot(state=ActivityState.WORKING, digest=digest)

    # Still. An explicit busy marker outranks stillness — some harnesses idle
    # on a static "working…" frame.
    if _match(pane, signals.busy_patterns):
        return Snapshot(state=ActivityState.WORKING, digest=digest)

    matched = _match(pane, signals.prompt_patterns)
    if matched:
        return Snapshot(
            state=ActivityState.AWAITING_INPUT,
            digest=digest,
            detail=_question_excerpt(pane, matched),
        )

    if still_for_seconds >= IDLE_AFTER_SECONDS:
        return Snapshot(state=ActivityState.IDLE, digest=digest)

    # Recently still but not long enough to call it: the model may be composing
    # a long response without redrawing.
    return Snapshot(state=ActivityState.WORKING, digest=digest)


async def probe(
    conn: asyncssh.SSHClientConnection,
    container: str,
    harness: Harness,
    *,
    previous_digest: str | None,
    still_for_seconds: float,
    session: str = sessions.DEFAULT_SESSION,
) -> Snapshot:
    """Observe one project's session."""
    info = await docker_remote.inspect(conn, container)
    if info is None or info.state != "running":
        return Snapshot(state=ActivityState.STOPPED, digest="")

    if not await sessions.session_exists(conn, container, session):
        return Snapshot(state=ActivityState.STOPPED, digest="")

    pane = await sessions.capture_pane(conn, container, session=session, lines=80)
    if not pane.strip():
        return Snapshot(state=ActivityState.UNKNOWN, digest="")

    return classify(
        pane,
        signals=harness.activity_signals(),
        previous_digest=previous_digest,
        still_for_seconds=still_for_seconds,
    )


def notification_for(
    previous: ActivityState, current: ActivityState, detail: str | None, project: str
) -> tuple[str, str] | None:
    """The (title, body) worth interrupting someone for, or None.

    Only the working → stopped-working transitions qualify. Notifying on
    entering `working` would fire every time the user themselves typed
    something, which is the fastest way to get notifications muted.
    """
    if previous != ActivityState.WORKING:
        return None

    if current is ActivityState.AWAITING_INPUT:
        return (f"{project} needs you", detail or "Claude is waiting for an answer.")
    if current is ActivityState.IDLE:
        return (f"{project} finished", "Claude stopped working.")
    return None
