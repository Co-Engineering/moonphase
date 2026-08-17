"""Parsing what a container says back.

All three of these features are a shell command and a parser. The command runs
on a real machine in the end-to-end test; what is worth pinning here is the
parser, because the failure mode is silent — a diff that renders empty or a
search that finds nothing look exactly like "there was nothing there".
"""

from __future__ import annotations

from moonphase import changes, search

SECTION = "###MOONPHASE-"


def _sections(**parts: str) -> str:
    return "\n".join(f"{SECTION}{name}\n{body}" for name, body in parts.items())


def test_a_diff_reports_files_and_totals() -> None:
    out = changes.parse(
        _sections(
            BRANCH="moonphase/oliver-test",
            BASE="main",
            STAT="12\t3\tapps/api/moonphase/usage.py\n40\t0\ttests/test_usage.py",
            UNTRACKED="",
            PATCH="diff --git a/x b/x\n+hello",
        )
    )

    assert out.branch == "moonphase/oliver-test"
    assert out.base == "main"
    assert out.added == 52
    assert out.removed == 3
    assert [f.path for f in out.files] == [
        "tests/test_usage.py",
        "apps/api/moonphase/usage.py",
    ]


def test_untracked_files_are_part_of_what_changed() -> None:
    """An agent that has written twenty files and committed none has still
    changed twenty files, and a review screen showing nothing would be worse
    than no review screen."""
    out = changes.parse(
        _sections(BRANCH="b", BASE="main", STAT="", UNTRACKED="new.py\nother.py", PATCH="")
    )

    assert [(f.path, f.status) for f in out.files] == [
        ("new.py", "untracked"),
        ("other.py", "untracked"),
    ]


def test_untracked_files_sort_after_edited_ones() -> None:
    out = changes.parse(
        _sections(STAT="1\t1\tedited.py", UNTRACKED="new.py", BRANCH="b", BASE="m")
    )

    assert [f.path for f in out.files] == ["edited.py", "new.py"]


def test_a_binary_file_does_not_break_the_count() -> None:
    """git reports '-' rather than a number for binary content."""
    out = changes.parse(_sections(STAT="-\t-\timage.png", BRANCH="b", BASE="m"))

    assert out.files[0].status == "binary"
    assert out.added == 0


def test_a_directory_that_is_not_a_repository_is_a_state_not_a_crash() -> None:
    out = changes.parse(f"{SECTION}ERROR\nnot a git repository")

    assert out.detail == "not a git repository"
    assert out.files == []


def test_a_patch_at_the_cap_is_marked_truncated() -> None:
    out = changes.parse(f"{SECTION}PATCH\n" + "x" * changes.MAX_PATCH_BYTES)

    assert out.truncated is True


def test_a_short_patch_is_not_marked_truncated() -> None:
    out = changes.parse(f"{SECTION}PATCH\ndiff --git a/x b/x")

    assert out.truncated is False


# --- search ------------------------------------------------------------------


def test_grep_output_becomes_file_and_line_pairs() -> None:
    found = search.parse_locations(
        "/home/dev/a.jsonl:41\n/home/dev/b.jsonl:7\nnot-a-location\n"
    )

    assert found == [("/home/dev/a.jsonl", 41), ("/home/dev/b.jsonl", 7)]


def test_a_windows_style_path_still_parses() -> None:
    """The line number is the last colon-separated field, not the second."""
    assert search.parse_locations("/a/b:c/d.jsonl:12") == [("/a/b:c/d.jsonl", 12)]


def test_the_fetch_script_quotes_every_path() -> None:
    script = search.fetch_script([("/tmp/a b.jsonl", 3)])

    assert "'/tmp/a b.jsonl'" in script
    assert script.startswith("sed -n 3p")


def test_a_hit_carries_readable_text_not_a_json_blob() -> None:
    line = (
        '{"timestamp":"2026-08-17T10:00:00Z","message":{"role":"user",'
        '"content":"can you fix the rate limiter please"}}'
    )
    (hit,) = search.records_from(line, "rate limiter")

    assert hit["role"] == "user"
    assert "rate limiter" in hit["text"]
    assert hit["at"] == "2026-08-17T10:00:00Z"


def test_text_blocks_are_flattened_and_tool_calls_dropped() -> None:
    """Tool calls are the bulk of an agent transcript and are noise in a list."""
    line = (
        '{"message":{"role":"assistant","content":['
        '{"type":"text","text":"I will fix the rate limiter"},'
        '{"type":"tool_use","name":"Edit","input":{"x":1}}]}}'
    )
    (hit,) = search.records_from(line, "rate limiter")

    assert hit["text"] == "I will fix the rate limiter"
    assert "tool_use" not in hit["text"]


def test_a_match_only_inside_a_tool_call_is_not_reported() -> None:
    """It would look like a false positive: the reader never sees that text."""
    line = (
        '{"message":{"role":"assistant","content":['
        '{"type":"text","text":"done"},'
        '{"type":"tool_use","input":{"cmd":"grep rate limiter"}}]}}'
    )

    assert search.records_from(line, "rate limiter") == []


def test_a_long_message_is_windowed_around_the_match() -> None:
    body = "a" * 3000 + "needle" + "b" * 3000
    line = f'{{"message":{{"role":"user","content":"{body}"}}}}'
    (hit,) = search.records_from(line, "needle")

    assert "needle" in hit["text"]
    assert len(hit["text"]) <= search.SNIPPET + 2
    assert hit["text"].startswith("…")


def test_a_malformed_line_is_skipped_rather_than_shown_raw() -> None:
    assert search.records_from("{ not json\n", "x") == []


def test_grep_is_asked_to_print_the_filename() -> None:
    """Without -H, grep omits it when given exactly one file.

    A session with a single transcript is the common case, so the output was
    `line:content` and every location parsed to nothing — a search that always
    came back empty and looked like "no matches".
    """
    import inspect

    source = inspect.getsource(search.search_session)

    assert "-iFnH" in source


def test_the_base_branch_is_resolved_with_symbolic_ref() -> None:
    """`git rev-parse --abbrev-ref origin/HEAD` prints "origin/HEAD" on stdout
    while failing, so the base came back as the literal "HEAD" and every diff
    was empty against itself."""
    script = changes._script("/work", 100)

    assert "symbolic-ref" in script
    # The comment explaining this still names the old command, so match the
    # substitution rather than the text.
    assert "$(git rev-parse --abbrev-ref origin/HEAD" not in script
    assert '[ "$BASE" = "HEAD" ]' in script
