"""Counting tokens, and refusing to guess at what they cost.

The numbers here end up in front of someone deciding whether to keep working
this month, so the two things worth testing hardest are that nothing is
double-counted and that an unknown model produces a blank rather than a
plausible-looking figure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from moonphase import usage


def _record(
    message_id: str = "msg_1",
    model: str = "claude-sonnet-4-5",
    **fields: object,
) -> str:
    payload = {
        "type": "assistant",
        "timestamp": "2026-08-17T10:00:00.000Z",
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 1000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 500,
                    "ephemeral_1h_input_tokens": 250,
                },
                **fields,
            },
        },
    }
    return json.dumps(payload)


def test_parses_the_cache_tiers_separately() -> None:
    """A five-minute write and an hour write are not the same price."""
    (event,) = usage.parse_events(_record())

    assert event.model == "claude-sonnet-4-5"
    assert event.input_tokens == 10
    assert event.output_tokens == 20
    assert event.cache_read_tokens == 1000
    assert event.cache_write_5m_tokens == 500
    assert event.cache_write_1h_tokens == 250


def test_older_records_without_a_breakdown_are_charged_at_the_lower_tier() -> None:
    line = json.dumps(
        {
            "timestamp": "2026-08-17T10:00:00Z",
            "message": {
                "id": "msg_old",
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 900,
                },
            },
        }
    )
    (event,) = usage.parse_events(line)

    # Understating is the right way to be wrong about someone's bill.
    assert event.cache_write_5m_tokens == 900
    assert event.cache_write_1h_tokens == 0


def test_a_malformed_line_does_not_cost_the_rest_of_the_batch() -> None:
    text = "\n".join([_record("a"), "{ not json", "", _record("b")])
    events = usage.parse_events(text)

    assert [event.message_id for event in events] == ["a", "b"]


def test_records_without_usage_are_ignored() -> None:
    """User turns, tool results and summaries all share the transcript."""
    text = "\n".join(
        [
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
            json.dumps({"type": "summary", "summary": "…"}),
            _record("counted"),
        ]
    )

    assert [event.message_id for event in usage.parse_events(text)] == ["counted"]


def test_a_dated_model_is_priced_as_its_family() -> None:
    price = usage.price_for("claude-sonnet-4-5-20260101")

    assert price is not None
    assert price.input == 3.0


def test_the_longest_matching_prefix_wins() -> None:
    """`claude-opus-4-1` must not be priced by the `claude-opus-4` entry."""
    assert usage.price_for("claude-opus-4-1-20260101") is usage.DEFAULT_PRICES[
        "claude-opus-4-1"
    ]


def test_an_unknown_model_has_no_cost_rather_than_a_zero() -> None:
    totals = usage.Totals(input_tokens=1_000_000)

    assert usage.cost_of(totals, "some-model-we-do-not-know") is None


def test_an_override_beats_the_built_in_rate() -> None:
    totals = usage.Totals(input_tokens=1_000_000)
    overrides = {"claude-sonnet-4-5": usage.tiered(99.0, 1.0)}

    assert usage.cost_of(totals, "claude-sonnet-4-5") == 3.0
    assert usage.cost_of(totals, "claude-sonnet-4-5", overrides) == 99.0


def test_an_override_can_price_a_model_that_ships_with_no_rate() -> None:
    totals = usage.Totals(output_tokens=1_000_000)
    overrides = {"claude-sonnet-5": usage.tiered(3.0, 15.0)}

    assert usage.cost_of(totals, "claude-sonnet-5-20260801", overrides) == 15.0


def test_cache_reads_are_a_tenth_of_input() -> None:
    """The difference between an estimate and a guess.

    A long agent session is mostly cache reads. Pricing them as fresh input
    overstates the bill by roughly an order of magnitude, which is the failure
    that would make this whole screen untrustworthy.
    """
    million = usage.Totals(cache_read_tokens=1_000_000)
    as_input = usage.Totals(input_tokens=1_000_000)

    assert usage.cost_of(million, "claude-sonnet-4-5") == pytest.approx(0.3)
    assert usage.cost_of(as_input, "claude-sonnet-4-5") == pytest.approx(3.0)


def test_totals_sum_every_tier_once() -> None:
    totals = usage.Totals()
    for event in usage.parse_events("\n".join([_record("a"), _record("b")])):
        totals.add(event)

    assert totals.input_tokens == 20
    assert totals.output_tokens == 40
    assert totals.cache_read_tokens == 2000
    assert totals.total == 20 + 40 + 2000 + 1000 + 500


def test_thinking_tokens_are_reported_but_not_double_counted() -> None:
    """Thinking is already inside `output_tokens`.

    Adding it to the total again would inflate every figure on the screen for
    exactly the models people use most.
    """
    line = _record(output_tokens_details={"thinking_tokens": 15})
    (event,) = usage.parse_events(line)
    totals = usage.Totals()
    totals.add(event)

    assert event.thinking_tokens == 15
    assert totals.total == 10 + 20 + 1000 + 500 + 250


def test_a_listing_is_read_as_sizes_by_path() -> None:
    listing = "1024\t/home/dev/.claude/projects/x/a.jsonl\n7\t/tmp/b.jsonl\n"

    assert usage.parse_listing(listing) == {
        "/home/dev/.claude/projects/x/a.jsonl": 1024,
        "/tmp/b.jsonl": 7,
    }


def test_a_listing_survives_a_line_it_cannot_read() -> None:
    assert usage.parse_listing("oops\t/a.jsonl\n12\t/b.jsonl\nnonsense\n") == {
        "/b.jsonl": 12
    }


def test_a_file_resumes_where_it_was_left() -> None:
    assert usage.resume_at({"/a.jsonl": 400}, "/a.jsonl", 900) == 400


def test_an_unseen_file_starts_at_the_top() -> None:
    """The bug this replaced: a new conversation abandoned the previous file.

    Claude Code opens a transcript per conversation, so a session that has been
    used twice has two files. Tracking one meant the older one's final messages
    were never read.
    """
    assert usage.resume_at({"/old.jsonl": 400}, "/new.jsonl", 900) == 0


def test_a_shrunken_file_is_read_from_the_top() -> None:
    """A smaller file was replaced, so the stored offset points at other
    content. Re-reading is safe because events are keyed by message id."""
    assert usage.resume_at({"/a.jsonl": 900}, "/a.jsonl", 100) == 0


def test_a_negative_offset_is_not_trusted() -> None:
    assert usage.resume_at({"/a.jsonl": -5}, "/a.jsonl", 100) == 0


# --- limit windows -----------------------------------------------------------


def _at(hour: float) -> datetime:
    return datetime(2026, 8, 17, tzinfo=UTC) + timedelta(hours=hour)


def test_a_window_is_anchored_to_its_first_message() -> None:
    """Not a trailing sum.

    The limit period opens when you start working and resets at a fixed time.
    Measuring backwards from now answers a question nobody asked and disagrees
    with what the harness itself reports.
    """
    window = usage.current_window([_at(1), _at(2), _at(3)], usage.SESSION_WINDOW, _at(4))

    assert window is not None
    assert window.started_at == _at(1)
    assert window.resets_at == _at(6)


def test_a_message_after_the_window_ends_opens_the_next_one() -> None:
    times = [_at(0), _at(1), _at(7), _at(8)]
    window = usage.current_window(times, usage.SESSION_WINDOW, _at(9))

    assert window is not None
    assert window.started_at == _at(7)
    assert window.resets_at == _at(12)


def test_a_message_exactly_on_the_boundary_starts_a_new_window() -> None:
    """Five hours after the anchor is outside it, not the last moment in it."""
    window = usage.current_window([_at(0), _at(5)], usage.SESSION_WINDOW, _at(6))

    assert window is not None
    assert window.started_at == _at(5)


def test_a_lapsed_window_is_reported_as_nothing_running() -> None:
    """Nothing is consuming the limit, so there is no percentage to show.

    Reporting the stale window as current would overstate how much of the
    allowance is gone, which is the wrong direction to be wrong in.
    """
    assert usage.current_window([_at(0)], usage.SESSION_WINDOW, _at(6)) is None


def test_no_messages_means_no_window() -> None:
    assert usage.current_window([], usage.SESSION_WINDOW, _at(1)) is None


def test_unordered_timestamps_still_anchor_correctly() -> None:
    """Transcripts are read per file, so arrival order is not time order."""
    window = usage.current_window([_at(8), _at(1), _at(7), _at(0)], usage.SESSION_WINDOW, _at(9))

    assert window is not None
    assert window.started_at == _at(7)


def test_the_weekly_window_uses_the_same_rule() -> None:
    times = [_at(0), _at(24 * 3)]
    window = usage.current_window(times, usage.WEEK_WINDOW, _at(24 * 4))

    assert window is not None
    assert window.started_at == _at(0)
    assert window.resets_at == _at(24 * 7)
