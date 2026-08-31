"""The features whose whole point is that their reader cannot check them.

Someone who does not read diffs is trusting a summary, and someone who cannot
run `git reflog` is trusting that "go back" did not lose their afternoon. Both
have to be right for reasons the person on the other side has no way to verify,
which is exactly why they are worth testing hard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from moonphase import checkpoints, digest


def _tool(name: str, **fields: object) -> str:
    return json.dumps(
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": name, "input": fields}],
            }
        }
    )


def _said(text: str) -> str:
    return json.dumps({"message": {"role": "assistant", "content": text}})


# --- digest -------------------------------------------------------------------


def test_files_written_are_counted_as_made() -> None:
    out = digest.summarise(
        "\n".join([_tool("Write", file_path="a.py"), _tool("Write", file_path="b.py")])
    )

    assert out.created == ["a.py", "b.py"]
    assert out.edited == []


def test_a_file_written_twice_is_one_file() -> None:
    """Otherwise "made 40 files" describes an agent that made four."""
    out = digest.summarise("\n".join([_tool("Write", file_path="a.py")] * 40))

    assert out.created == ["a.py"]


def test_a_file_created_here_is_not_also_listed_as_changed() -> None:
    """ "Made 1 file and changed 1 file" about the same file reads as two."""
    out = digest.summarise(
        "\n".join([_tool("Write", file_path="a.py"), _tool("Edit", file_path="a.py")])
    )

    assert out.created == ["a.py"]
    assert out.edited == []


def test_editing_something_that_already_existed_counts_as_a_change() -> None:
    out = digest.summarise(_tool("Edit", file_path="existing.py"))

    assert out.edited == ["existing.py"]
    assert out.created == []


def test_installs_are_recognised_across_package_managers() -> None:
    lines = [
        _tool("Bash", command="npm install react"),
        _tool("Bash", command="pip install fastapi"),
        _tool("Bash", command="uv add httpx"),
        _tool("Bash", command="apt-get install -y curl"),
    ]
    out = digest.summarise("\n".join(lines))

    assert out.installs == 4
    assert out.commands == 4


def test_a_command_that_merely_mentions_install_is_not_an_install() -> None:
    out = digest.summarise(_tool("Bash", command="echo 'how to install this'"))

    assert out.installs == 0
    assert out.commands == 1


def test_test_runs_are_recognised() -> None:
    lines = [
        _tool("Bash", command="pytest -q"),
        _tool("Bash", command="npm run test"),
        _tool("Bash", command="ls"),
    ]
    out = digest.summarise("\n".join(lines))

    assert out.tests == 2
    assert out.commands == 3


def test_the_closing_sentence_is_the_last_thing_it_said() -> None:
    out = digest.summarise(
        "\n".join([_said("starting now"), _tool("Bash", command="ls"), _said("all done")])
    )

    assert out.last_said == "all done"


def test_a_half_written_first_line_costs_nothing() -> None:
    """Reading a tail always starts mid-record."""
    text = '"content":[{"type":"text"}]}}\n' + _tool("Write", file_path="a.py")
    out = digest.summarise(text)

    assert out.created == ["a.py"]


def test_an_empty_transcript_is_empty_rather_than_wrong() -> None:
    assert digest.summarise("").empty is True


def test_a_session_that_only_read_things_is_not_empty() -> None:
    out = digest.summarise(_tool("Bash", command="ls"))

    assert out.empty is False
    assert out.commands == 1


# --- save points --------------------------------------------------------------


def test_the_log_becomes_a_list_of_points() -> None:
    out = checkpoints.parse_board(
        "###LOG\n"
        "abc1234\t2026-08-17T10:00:00Z\tBefore the redesign\n"
        "def5678\t2026-08-17T09:00:00Z\tWorking login\n"
        "###DIRTY\n"
    )

    assert [p.label for p in out.points] == ["Before the redesign", "Working login"]
    assert out.unsaved == 0
    # Nothing has changed since, so the newest point is where the files are.
    assert out.points[0].current is True


def test_unsaved_work_means_no_point_is_current() -> None:
    """Saying "you are here" while two files differ would be a lie about the
    one thing this panel exists to tell you."""
    out = checkpoints.parse_board(
        "###LOG\nabc1234\t2026-08-17T10:00:00Z\tWorking login\n###DIRTY\n M app.py\n?? new.py\n"
    )

    assert out.unsaved == 2
    assert out.points[0].current is False


def test_an_automatic_point_is_marked_as_one() -> None:
    out = checkpoints.parse_board(
        "###LOG\nabc1234\t2026-08-17T10:00:00Z\tBefore going back\n###DIRTY\n"
    )

    assert out.points[0].automatic is True


def test_a_missing_repository_is_a_message_not_a_crash() -> None:
    out = checkpoints.parse_board("###ERROR\nThat project folder is missing.")

    assert out.detail == "That project folder is missing."
    assert out.points == []


def test_saving_takes_files_git_has_never_seen() -> None:
    """The person clicked save because they want *this*, and a brand new file
    is very much part of this."""
    script = checkpoints.save_script("/work", "My save", "Ada Lovelace", "ada@example.com")

    assert "git add -A" in script


def test_save_commits_as_the_configured_identity() -> None:
    """Regression guard: this used to read GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL,
    env vars nothing ever set, so every save point was authored "Moonphase"
    no matter what a person configured. The identity must be passed in and
    actually land in the script, not read from the shell environment."""
    script = checkpoints.save_script("/work", "My save", "Ada Lovelace", "ada@example.com")

    assert "user.name='Ada Lovelace'" in script
    assert "ada@example.com" in script
    assert "GIT_AUTHOR_NAME" not in script
    assert "GIT_AUTHOR_EMAIL" not in script


def test_restoring_saves_the_current_state_first() -> None:
    """The rule that makes this safe to hand to someone who cannot inspect it:
    going back never destroys anything, so the undo has an undo."""
    script = checkpoints.restore_script(
        "/work", "abc1234", "Went back", "Ada Lovelace", "ada@example.com"
    )

    before = script.index("Before going back")
    restore = script.index("git restore --source=")
    assert before < restore


def test_restoring_does_not_remove_ignored_files() -> None:
    """`-fdx` would delete node_modules and a virtualenv, turning an undo into
    twenty minutes of reinstalling."""
    script = checkpoints.restore_script(
        "/work", "abc1234", "Went back", "Ada Lovelace", "ada@example.com"
    )

    assert "git clean -fdq" in script
    assert "-fdx" not in script


def test_a_label_with_quotes_cannot_escape_the_command() -> None:
    script = checkpoints.save_script(
        "/work", 'evil"; rm -rf /; echo "', "Ada Lovelace", "ada@example.com"
    )

    # Quoted as one shell word, so the semicolons are text rather than syntax.
    assert "rm -rf /; echo" in script
    assert script.count("LABEL=") == 1
    assert "\nrm -rf /" not in script


def test_a_default_label_says_when() -> None:
    label = checkpoints.default_label(datetime(2026, 8, 17, 14, 30, tzinfo=UTC))

    assert "17 Aug" in label
    assert "14:30" in label


def test_success_and_failure_are_told_apart() -> None:
    assert checkpoints.parse_result("###OK\nabc123")[0] is True

    ok, message = checkpoints.parse_result(
        "###ERROR\nNothing has changed since your last save point."
    )
    assert ok is False
    assert message == "Nothing has changed since your last save point."


def test_an_unexplained_failure_still_says_something_useful() -> None:
    ok, message = checkpoints.parse_result("")

    assert ok is False
    assert message


def test_paths_are_shown_as_the_person_thinks_of_them() -> None:
    """The transcript records absolute container paths, and four directories of
    Moonphase plumbing in front of `README.md` is not a file list anyone
    wants."""
    root = "/home/dev/sessions/oliver-test/work"
    out = digest.summarise(_tool("Write", file_path=f"{root}/frontend/App.jsx"), root)

    assert out.created == ["frontend/App.jsx"]


def test_a_path_outside_the_project_is_left_alone() -> None:
    out = digest.summarise(_tool("Write", file_path="/etc/hosts"), "/home/dev/work")

    assert out.created == ["/etc/hosts"]


def test_unsaved_work_counts_files_not_directories() -> None:
    """`git status --porcelain` collapses a new directory to one line, which
    reported 4 changes where 27 files had changed."""
    assert "-uall" in checkpoints.board_script("/work")
