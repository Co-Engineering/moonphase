"""What the agents actually consumed, and what it costs.

Claude Code writes its own usage into the transcript it already keeps: the
model, input and output tokens, cache reads, and cache writes split by how long
they live. That is the authoritative figure — it is what the provider counted —
and it is in a file Moonphase already reads for the feed. Nothing needs to be
estimated, intercepted or asked for.

Two people want two different things from it, and they are not the same
question:

* On a subscription, the limit is a rolling window. What matters is how much of
  the current one has gone, so the useful number is consumption over the last
  five hours and the week.
* On an API key, the limit is money. What matters is the bill, which means
  tokens multiplied by a price per model.

Both are computed from the same rows. Which one is put first is decided by the
credential the person actually connected.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import asyncssh

from . import docker_remote

log = logging.getLogger(__name__)

# How much of a transcript to read in one pass. A busy session writes a few
# hundred kilobytes a day; this bounds a single read without ever losing data,
# because the offset only advances by what was actually consumed.
MAX_READ_BYTES = 2_000_000


@dataclass
class UsageEvent:
    """One assistant message, as the provider counted it."""

    message_id: str
    model: str
    at: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    thinking_tokens: int = 0


@dataclass
class Price:
    """Dollars per million tokens.

    Cache writes cost more than fresh input because the provider stores them,
    and a longer life costs more than a shorter one. Cache reads cost a
    fraction. Modelling that properly is the difference between a bill estimate
    and a guess: a long agent session is mostly cache reads, so treating them
    as input overstates the cost by an order of magnitude.
    """

    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float


def tiered(input_price: float, output_price: float) -> Price:
    """The standard Anthropic cache multipliers around a base rate."""
    return Price(
        input=input_price,
        output=output_price,
        cache_read=input_price * 0.1,
        cache_write_5m=input_price * 1.25,
        cache_write_1h=input_price * 2.0,
    )


# Published rates for models whose pricing is stable and well known. A model
# that is not here reports tokens and no cost — inventing a rate would produce
# a confident number that happens to be wrong, which is worse than a blank and
# a prompt to fill it in. Rates are overridable per organization.
DEFAULT_PRICES: dict[str, Price] = {
    "claude-opus-4": tiered(15.0, 75.0),
    "claude-opus-4-1": tiered(15.0, 75.0),
    "claude-sonnet-4": tiered(3.0, 15.0),
    "claude-sonnet-4-5": tiered(3.0, 15.0),
    "claude-haiku-4-5": tiered(1.0, 5.0),
    "claude-3-5-haiku": tiered(0.8, 4.0),
    "claude-3-5-sonnet": tiered(3.0, 15.0),
    "claude-3-opus": tiered(15.0, 75.0),
}


def price_for(model: str, overrides: dict[str, Price] | None = None) -> Price | None:
    """Longest matching prefix wins, so dated variants resolve to their family.

    `claude-sonnet-4-5-20260101` is a Sonnet 4.5 and should be priced as one
    without needing an entry per release date.
    """
    table = {**DEFAULT_PRICES, **(overrides or {})}
    best: Price | None = None
    best_len = -1
    for key, price in table.items():
        if model.startswith(key) and len(key) > best_len:
            best, best_len = price, len(key)
    return best


def cost_of(event_totals: Totals, model: str, overrides=None) -> float | None:
    price = price_for(model, overrides)
    if price is None:
        return None
    million = 1_000_000
    return (
        event_totals.input_tokens * price.input
        + event_totals.output_tokens * price.output
        + event_totals.cache_read_tokens * price.cache_read
        + event_totals.cache_write_5m_tokens * price.cache_write_5m
        + event_totals.cache_write_1h_tokens * price.cache_write_1h
    ) / million


@dataclass
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    thinking_tokens: int = 0

    def add(self, other: Totals | UsageEvent) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_5m_tokens += other.cache_write_5m_tokens
        self.cache_write_1h_tokens += other.cache_write_1h_tokens
        self.thinking_tokens += other.thinking_tokens

    @property
    def total(self) -> int:
        """Everything the provider counted, for a single headline number."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )


def parse_events(text: str) -> list[UsageEvent]:
    """Pull usage out of transcript lines, ignoring everything else.

    Tolerant by design. A partial last line is normal when reading a file that
    is being appended to, and one malformed record must not cost the rest of
    the batch.
    """
    events: list[UsageEvent] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue

        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        message_id = message.get("id")
        if not message_id:
            continue

        at = _parse_time(record.get("timestamp"))
        cache_creation = usage.get("cache_creation")
        if isinstance(cache_creation, dict):
            write_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
            write_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
        else:
            # Older records report one total without a breakdown. Attributing it
            # to the cheaper tier understates rather than overstates, which is
            # the right way to be wrong about someone's bill.
            write_5m = int(usage.get("cache_creation_input_tokens") or 0)
            write_1h = 0

        details = usage.get("output_tokens_details")
        thinking = 0
        if isinstance(details, dict):
            thinking = int(details.get("thinking_tokens") or 0)

        events.append(
            UsageEvent(
                message_id=str(message_id),
                model=str(message.get("model") or "unknown"),
                at=at,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_write_5m_tokens=write_5m,
                cache_write_1h_tokens=write_1h,
                thinking_tokens=thinking,
            )
        )
    return events


def _parse_time(value: object) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


@dataclass
class Collected:
    """What one pass over a session's transcripts produced."""

    events: list[UsageEvent] = field(default_factory=list)
    # Where to resume in each file next time. Replaces the stored map wholesale,
    # so a transcript that has been deleted stops being tracked rather than
    # accumulating forever.
    cursors: dict[str, int] = field(default_factory=dict)


def parse_listing(stdout: str) -> dict[str, int]:
    """Filenames and sizes, as `size<TAB>path` lines."""
    sizes: dict[str, int] = {}
    for line in stdout.splitlines():
        size, tab, name = line.partition("\t")
        if not tab or not name.strip():
            continue
        try:
            sizes[name.strip()] = int(size.strip())
        except ValueError:
            continue
    return sizes


def resume_at(known: dict[str, int], filename: str, size: int) -> int:
    """Where to start reading, given what we read last time.

    A file that has shrunk was replaced rather than appended to, so the stored
    offset points into different content and reading from the top is the only
    correct answer. Re-reading is safe — events are keyed by message id — so
    the failure mode is a little wasted work, not a double-counted bill.
    """
    offset = known.get(filename, 0)
    return offset if 0 <= offset <= size else 0


async def collect_session(
    conn: asyncssh.SSHClientConnection,
    container: str,
    transcript_dir: str,
    *,
    known: dict[str, int],
) -> Collected | None:
    """Read whatever a session has written since we last looked.

    Every transcript in the directory, not only the newest. Claude Code opens a
    new file per conversation, so following just the latest one abandons the
    tail of the previous conversation the instant someone starts another —
    quietly losing real messages exactly when a session gets busy.

    Byte offsets rather than re-reading: a transcript grows all day, and
    re-parsing every one of them each pass would make the cheapest question
    Moonphase asks into the most expensive one.
    """
    directory = shlex.quote(transcript_dir)
    listing = await docker_remote.exec_capture(
        conn,
        container,
        [
            "sh",
            "-c",
            f'for f in {directory}/*.jsonl; do [ -f "$f" ] && '
            'printf "%s\\t%s\\n" "$(stat -c %s "$f")" "$f"; done',
        ],
        timeout=30,
    )
    if not listing.ok:
        return None

    sizes = parse_listing(listing.stdout)
    if not sizes:
        # No transcript yet. A session that has never run the harness is the
        # normal case here, not a failure.
        return Collected(events=[], cursors={})

    events: list[UsageEvent] = []
    cursors: dict[str, int] = {}
    for filename, size in sorted(sizes.items()):
        start = resume_at(known, filename, size)
        if start >= size:
            cursors[filename] = size
            continue

        end = min(size, start + MAX_READ_BYTES)
        body = await docker_remote.exec_capture(
            conn,
            container,
            [
                "sh",
                "-c",
                f"tail -c +{start + 1} {shlex.quote(filename)} | head -c {end - start}",
            ],
            timeout=60,
        )
        if not body.ok:
            # Keep the old position for this file so the next pass retries it
            # rather than skipping over whatever could not be read.
            cursors[filename] = start
            continue

        text = body.stdout
        # Stop at the last complete line: the tail of the file may be
        # half-written, and advancing past it would lose the rest of that
        # record forever.
        cut = text.rfind("\n")
        consumed = (
            len(text[: cut + 1].encode("utf-8", errors="ignore")) if cut >= 0 else 0
        )
        events.extend(parse_events(text[: cut + 1] if cut >= 0 else ""))
        cursors[filename] = start + consumed

    return Collected(events=events, cursors=cursors)


# --- limit windows -----------------------------------------------------------

# A subscription's usage window opens with your first message and runs for a
# fixed span; it is not a trailing average. Treating it as "the last five
# hours" answers a question nobody asked and disagrees with what the harness
# itself reports.
SESSION_WINDOW = timedelta(hours=5)
WEEK_WINDOW = timedelta(days=7)


@dataclass
class Window:
    """One limit period, anchored to when it actually opened."""

    started_at: datetime
    resets_at: datetime

    def active_at(self, now: datetime) -> bool:
        return now < self.resets_at


def current_window(
    times: list[datetime], length: timedelta, now: datetime
) -> Window | None:
    """Where the open window began, given every message in the period.

    Walks forward: the first message opens a window, and the first message at
    or after that window's end opens the next one. What falls out is the
    anchor of the most recent window, which is the only thing that makes
    "resets at" a real time rather than five hours from whenever you looked.
    """
    if not times:
        return None
    ordered = sorted(times)
    start = ordered[0]
    for at in ordered:
        if at >= start + length:
            start = at
    window = Window(started_at=start, resets_at=start + length)
    if not window.active_at(now):
        # It lapsed. Nothing is consuming the limit until the next message, so
        # reporting the stale window as if it were current would be a lie in
        # the direction that matters.
        return None
    return window
