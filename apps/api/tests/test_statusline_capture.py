"""The status line Moonphase gives Claude Code keeps its plan-limit reading.

Run for real with sh and jq: it must save `rate_limits` when Claude Code
passes them, leave the last reading alone when it does not, and still show
whatever status line the person configured themselves.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from moonphase.harness import claude_code
from moonphase.harness import get as get_harness
from moonphase.harness.base import SessionSpace

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")

LIMITS = {"five_hour": {"used_percentage": 12, "resets_at": 1900000000}}


def _run(home: Path, payload: dict, project: Path | None = None) -> subprocess.CompletedProcess:
    script = home / claude_code.STATUSLINE_SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(claude_code._STATUSLINE)
    if project is not None:
        payload = {**payload, "workspace": {"project_dir": str(project)}}
    return subprocess.run(
        ["sh", str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        timeout=20,
    )


def _saved(home: Path):
    f = home / claude_code.USAGE_FILE
    return json.loads(f.read_text()) if f.exists() else None


def test_rate_limits_are_saved(tmp_path: Path) -> None:
    result = _run(tmp_path, {"model": {"id": "x"}, "rate_limits": LIMITS})
    assert result.returncode == 0
    assert _saved(tmp_path) == {"rate_limits": LIMITS}


def test_a_message_without_rate_limits_keeps_the_last_reading(tmp_path: Path) -> None:
    _run(tmp_path, {"rate_limits": LIMITS})
    _run(tmp_path, {"model": {"id": "x"}})
    assert _saved(tmp_path) == {"rate_limits": LIMITS}


def test_nothing_is_shown_without_a_status_line_of_ones_own(tmp_path: Path) -> None:
    result = _run(tmp_path, {"rate_limits": LIMITS})
    assert result.stdout == ""


def test_the_persons_own_status_line_still_runs_and_sees_the_same_input(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "jq -r .model.id"}})
    )
    result = _run(tmp_path, {"model": {"id": "claude-x"}, "rate_limits": LIMITS})
    assert result.stdout.strip() == "claude-x"
    assert _saved(tmp_path) == {"rate_limits": LIMITS}


def test_a_projects_status_line_wins_over_the_users(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "repo"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "echo user"}})
    )
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "echo project"}})
    )
    assert _run(home, {"rate_limits": LIMITS}, project).stdout.strip() == "project"


def test_claude_is_launched_with_the_status_line() -> None:
    for resume in (False, True):
        command = get_harness("claude_code").launch_spec(resume=resume).command
        i = command.index("--settings")
        settings = json.loads(command[i + 1])
        assert claude_code.STATUSLINE_SCRIPT in settings["statusLine"]["command"]
        assert settings["statusLine"]["type"] == "command"


def test_the_script_is_written_into_every_sessions_home() -> None:
    files = get_harness("claude_code").profile_files(
        type("P", (), {"claude_settings_json": None, "claude_md": None})(),
        SessionSpace(home="/home/dev/sessions/s"),
    )
    assert files["/home/dev/sessions/s/.moonphase/statusline.sh"] == claude_code._STATUSLINE
