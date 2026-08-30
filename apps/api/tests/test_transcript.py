"""Transcript parsing and tailing.

The phone client renders whatever comes out of here, so the two things that
matter are that a real transcript turns into something a person would want to
read, and that tailing never loses or duplicates a line — a feed that silently
drops the message you were waiting for is worse than no feed.
"""

from __future__ import annotations

import json

from moonphase.activity import parse_prompt
from moonphase.harness import get as get_harness
from moonphase.transcript import MAX_DIFF_LINES, Cursor, build_diff

CLAUDE = get_harness("claude_code")


def record(**kwargs) -> dict:
    base = {"type": "assistant", "uuid": "u1", "timestamp": "2026-08-17T10:00:00Z"}
    base.update(kwargs)
    return base


# --- parsing ----------------------------------------------------------------


def test_a_typed_prompt_becomes_a_user_event() -> None:
    events = CLAUDE.parse_transcript_record(
        record(type="user", message={"role": "user", "content": "refactor the auth module"})
    )
    assert [(e.kind, e.text) for e in events] == [
        ("user", "refactor the auth module")
    ]


def test_assistant_text_and_tools_become_separate_events() -> None:
    events = CLAUDE.parse_transcript_record(
        record(
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Reading the file first."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "/workspace/auth.py"},
                    },
                ],
            }
        )
    )
    assert [e.kind for e in events] == ["assistant", "tool"]
    assert events[1].tool == "Read"
    # The summary is the bit a reader actually wants: which file.
    assert events[1].text == "/workspace/auth.py"


def test_tool_summaries_pick_the_useful_field() -> None:
    cases = [
        ("Bash", {"command": "npm test", "description": "run tests"}, "npm test"),
        ("Grep", {"pattern": "TODO", "path": "/x"}, "TODO"),
        ("Write", {"file_path": "/a/b.py", "content": "x" * 5000}, "/a/b.py"),
    ]
    for name, tool_input, expected in cases:
        events = CLAUDE.parse_transcript_record(
            record(
                message={
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "t", "name": name, "input": tool_input}
                    ],
                }
            )
        )
        assert events[0].text == expected, name


def test_an_unknown_tool_still_summarises_readably() -> None:
    events = CLAUDE.parse_transcript_record(
        record(
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": "SomeFutureTool",
                        "input": {"target": "the thing"},
                    }
                ],
            }
        )
    )
    assert events[0].tool == "SomeFutureTool"
    assert events[0].text == "the thing"


def test_failed_tool_results_are_kept_and_marked() -> None:
    events = CLAUDE.parse_transcript_record(
        record(
            type="user",
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": "No such file or directory",
                    }
                ],
            },
        )
    )
    assert len(events) == 1
    assert events[0].kind == "result"
    assert events[0].ok is False
    assert "No such file" in events[0].text


def test_a_screenshot_tool_result_carries_its_image() -> None:
    """A browser MCP server's screenshot comes back as a base64 image block —
    the same thing the model itself is shown — and that is reason enough on
    its own to keep the result, even though nothing failed and there is no
    text to excerpt."""
    events = CLAUDE.parse_transcript_record(
        record(
            type="user",
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": False,
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "aGVsbG8=",
                                },
                            }
                        ],
                    }
                ],
            },
        )
    )
    assert len(events) == 1
    assert events[0].kind == "result"
    assert events[0].ok is True
    assert events[0].image_media_type == "image/png"
    assert events[0].image_data == "aGVsbG8="


def test_a_successful_result_with_no_image_and_no_text_is_dropped() -> None:
    events = CLAUDE.parse_transcript_record(
        record(
            type="user",
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": False,
                        "content": "",
                    }
                ],
            },
        )
    )
    assert events == []


def test_bookkeeping_records_are_ignored() -> None:
    """Transcripts carry a lot that nobody wants to read."""
    for kind in (
        "file-history-snapshot", "file-history-delta", "mode",
        "permission-mode", "ai-title", "last-prompt", "queue-operation",
    ):
        assert CLAUDE.parse_transcript_record({"type": kind, "uuid": "x"}) == []


def test_subagent_traffic_is_flagged_not_dropped() -> None:
    events = CLAUDE.parse_transcript_record(
        record(
            isSidechain=True,
            message={"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        )
    )
    assert events[0].sidechain is True


def test_block_ids_are_unique_within_a_record() -> None:
    """Blocks share the record's uuid; the client keys on these."""
    events = CLAUDE.parse_transcript_record(
        record(
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "one"},
                    {"type": "text", "text": "two"},
                ],
            }
        )
    )
    assert len({e.id for e in events}) == 2


def test_malformed_records_do_not_raise() -> None:
    # A transcript is written concurrently; robustness beats strictness.
    for bad in [None, [], "text", {"type": "assistant"}, {"type": "assistant", "message": 3}]:
        assert CLAUDE.parse_transcript_record(bad) == []


# --- diffs ------------------------------------------------------------------


def test_an_edit_carries_its_diff() -> None:
    """A file path alone cannot answer "do you want to make this edit?"."""
    events = CLAUDE.parse_transcript_record(
        record(
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use", "id": "t", "name": "Edit",
                        "input": {
                            "file_path": "/workspace/a.py",
                            "old_string": "x = 1\ny = 2\n",
                            "new_string": "x = 1\ny = 3\nz = 4\n",
                        },
                    }
                ],
            }
        )
    )
    event = events[0]
    assert event.added == 2
    assert event.removed == 1
    assert event.diff is not None
    signs = {line.sign for line in event.diff}
    assert "+" in signs and "-" in signs


def test_a_write_reads_as_all_additions() -> None:
    events = CLAUDE.parse_transcript_record(
        record(
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use", "id": "t", "name": "Write",
                        "input": {"file_path": "/w/new.py", "content": "one\ntwo\n"},
                    }
                ],
            }
        )
    )
    assert events[0].added == 2
    assert events[0].removed == 0


def test_tools_without_a_change_carry_no_diff() -> None:
    events = CLAUDE.parse_transcript_record(
        record(
            message={
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t", "name": "Read",
                     "input": {"file_path": "/w/a.py"}}
                ],
            }
        )
    )
    assert events[0].diff is None


def test_a_huge_diff_is_truncated_but_counts_the_whole_change() -> None:
    """Truncating the lines is fine; truncating the counts would mislead.

    "+800" next to a short diff is honest. A short diff reported as "+120" is
    not, and it is the number someone approves on.
    """
    before = ""
    after = "\n".join(f"line {i}" for i in range(800)) + "\n"
    lines, added, removed, truncated = build_diff(before, after)
    assert added == 800
    assert removed == 0
    assert truncated is True
    assert len(lines) == MAX_DIFF_LINES


def test_long_lines_are_clipped() -> None:
    lines, _, _, _ = build_diff("", "x" * 5000 + "\n")
    assert all(len(line.text) <= 200 for line in lines)


# --- cursors ----------------------------------------------------------------


def test_cursor_round_trips() -> None:
    encoded = Cursor(filename="abc-123.jsonl", offset=4096).encode()
    decoded = Cursor.decode(encoded)
    assert decoded.filename == "abc-123.jsonl"
    assert decoded.offset == 4096


def test_a_garbage_cursor_reads_as_a_cold_start() -> None:
    # Better to replay the tail than to decode from a nonsense offset, which
    # would slice a line in half and drop it.
    for bad in [None, "", "nonsense", "file:notanumber"]:
        assert Cursor.decode(bad).offset == 0


# --- prompts ----------------------------------------------------------------


def test_prompt_options_are_tappable() -> None:
    pane = (
        "⏺ Edit(auth.py)\n\n"
        " Do you want to make this edit to auth.py?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again\n"
        "   3. No, tell Claude what to do differently\n"
    )
    prompt = parse_prompt(pane, CLAUDE.activity_signals())
    assert prompt is not None
    assert prompt.question.endswith("?")
    assert [o["key"] for o in prompt.options] == ["1", "2", "3"]
    assert prompt.options[0]["label"] == "Yes"


def test_only_the_most_recent_question_is_offered() -> None:
    """Scrollback holds old prompts; answering one of those would be wrong."""
    pane = (
        " Do you want to proceed?\n"
        " 1. Old yes\n"
        " 2. Old no\n"
        "⏺ Read(x.py)\n"
        " Do you want to make this edit?\n"
        " ❯ 1. New yes\n"
        "   2. New no\n"
    )
    prompt = parse_prompt(pane, CLAUDE.activity_signals())
    assert prompt is not None
    assert [o["label"] for o in prompt.options] == ["New yes", "New no"]


def test_the_question_is_found_above_chrome() -> None:
    """The nearest line above the options is often not the question.

    Claude Code puts a "Security guide" link between its trust question and the
    choices, so taking the nearest line verbatim shows the wrong text on the
    one screen where the text is all the user has.
    """
    pane = (
        " Quick safety check: Is this a project you created or one you trust?"
        " (Like your own code, or work from your team).\n"
        " If not, take a moment to review what's in this folder first.\n"
        "\n"
        " Security guide\n"
        "\n"
        " ❯ 1. Yes, I trust this folder\n"
        "   2. No, exit\n"
    )
    prompt = parse_prompt(pane, CLAUDE.activity_signals())
    assert prompt is not None
    assert prompt.question == (
        "Quick safety check: Is this a project you created or one you trust?"
    )
    assert [o["label"] for o in prompt.options] == ["Yes, I trust this folder", "No, exit"]


def test_a_prompt_not_phrased_as_a_question_still_gets_a_label() -> None:
    pane = " Select a theme\n\n ❯ 1. Dark mode\n   2. Light mode\n"
    prompt = parse_prompt(pane, CLAUDE.activity_signals())
    assert prompt is not None
    assert prompt.question == "Select a theme"


def test_a_wrapped_option_label_does_not_swallow_earlier_options() -> None:
    """A narrow phone pane wraps long labels onto an indented continuation line.

    That continuation line must not be mistaken for chrome ending the block —
    doing so previously discarded every option above it.
    """
    pane = (
        " Do you want to make this edit to auth.py?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and re-run the whole test suite again before\n"
        "      continuing to the next step\n"
        "   3. No, tell Claude what to do differently\n"
    )
    prompt = parse_prompt(pane, CLAUDE.activity_signals())
    assert prompt is not None
    assert [o["key"] for o in prompt.options] == ["1", "2", "3"]
    assert prompt.options[1]["label"] == (
        "Yes, and re-run the whole test suite again before continuing to the next step"
    )


def test_a_separator_before_chat_about_this_does_not_swallow_the_real_options() -> None:
    """AskUserQuestion draws a rule between its own numbered options and the
    "Chat about this" escape hatch tacked on after them (and, in this case,
    per-option descriptions on their own indented line below each choice).

    That rule sits flush at column 0 — indistinguishable, by indentation
    alone, from real chrome ending the block — and previously stopped the
    scan right there, keeping only the option below it: reported as "only
    the last option shows up" on a phone with a six-option question.
    """
    pane = (
        "What's your favorite animal?\n\n"
        "❯ 1. Ape\n"
        "     Our closest living relatives.\n"
        "  2. Dog\n"
        "     Loyal and always happy to see you.\n"
        "  3. Cat\n"
        "     Independent and a bit mysterious.\n"
        "  4. Hippo\n"
        "     Surprisingly fast, and surprisingly dangerous.\n"
        "  5. Type something.\n"
        "─────────────────────────────────────────────\n"
        "6. Chat about this\n"
        "\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )
    prompt = parse_prompt(pane, CLAUDE.activity_signals())
    assert prompt is not None
    assert [o["key"] for o in prompt.options] == ["1", "2", "3", "4", "5", "6"]
    assert prompt.options[5]["label"] == "Chat about this"


def test_no_prompt_when_the_agent_is_working() -> None:
    assert parse_prompt("⏺ Read(x.py)\n  240 lines\n", CLAUDE.activity_signals()) is None


# --- against a real transcript ---------------------------------------------


def test_parses_a_realistic_transcript_into_a_readable_feed() -> None:
    """A short but real-shaped session, end to end."""
    lines = [
        {"type": "mode", "mode": "default", "sessionId": "s"},
        {
            "type": "user", "uuid": "a", "timestamp": "t",
            "message": {"role": "user", "content": "add a health endpoint"},
        },
        {
            "type": "assistant", "uuid": "b", "timestamp": "t",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I should look at the router first"},
                    {"type": "text", "text": "I'll add it to the meta router."},
                    {
                        "type": "tool_use", "id": "t1", "name": "Read",
                        "input": {"file_path": "/workspace/routers/meta.py"},
                    },
                ],
            },
        },
        {
            "type": "user", "uuid": "c", "timestamp": "t",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "120 lines"}
                ],
            },
        },
    ]

    events = []
    for line in lines:
        events.extend(CLAUDE.parse_transcript_record(json.loads(json.dumps(line))))

    kinds = [e.kind for e in events]
    assert kinds == ["user", "thinking", "assistant", "tool", "result"]
    assert events[0].text == "add a health endpoint"
    assert events[3].tool == "Read"
    assert events[4].ok is True
