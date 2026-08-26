"""Composing org, project and session Claude Code config into one profile.

No SSH or Docker here — `compose_project_layers` is a pure function over rows
that could have come straight from the database, so it is worth pinning down
on its own rather than only exercising it end to end.
"""

from __future__ import annotations

import json

from moonphase.harness import get as get_harness
from moonphase.profile import WorkspaceProfile

HARNESS = get_harness("claude_code")


def _profile(**kwargs) -> WorkspaceProfile:
    base = dict(
        org_id="org", claude_settings_json=None, claude_md=None, mcp_json=None,
        skills={},
    )
    base.update(kwargs)
    return WorkspaceProfile(**base)


def test_no_project_or_session_config_is_a_true_no_op() -> None:
    """The common case — nobody has touched project/session config — must be
    byte-for-byte the org profile, not a reformatted copy of it."""
    profile = _profile(claude_md="# My rules", claude_settings_json='{"model":"x"}')

    assert HARNESS.compose_project_layers(profile, None, None) is profile
    empty_row = {
        "claude_settings_json": None, "claude_md": None, "mcp_json": None,
        "skills_json": {},
    }
    assert HARNESS.compose_project_layers(profile, empty_row, empty_row) is profile


def test_claude_md_concatenates_broadest_first() -> None:
    profile = _profile(claude_md="Org rules")
    merged = HARNESS.compose_project_layers(
        profile,
        {"claude_md": "Project rules"},
        {"claude_md": "Session rules"},
    )
    md = merged.claude_md
    assert md.index("Org rules") < md.index("Project rules") < md.index("Session rules")


def test_mcp_servers_merge_by_name_session_wins_collisions() -> None:
    profile = _profile(
        mcp_json=json.dumps({"mcpServers": {"fs": {"command": "org-fs"}}})
    )
    merged = HARNESS.compose_project_layers(
        profile,
        {"mcp_json": json.dumps({"mcpServers": {"pg": {"command": "project-pg"}}})},
        {"mcp_json": json.dumps({"mcpServers": {"fs": {"command": "session-fs"}}})},
    )
    servers = json.loads(merged.mcp_json)["mcpServers"]
    assert servers["pg"]["command"] == "project-pg"
    assert servers["fs"]["command"] == "session-fs", "the more specific scope should win"


def test_skills_merge_by_name_session_wins_collisions() -> None:
    profile = _profile(skills={"reviewer": "org body"})
    merged = HARNESS.compose_project_layers(
        profile,
        {"skills_json": json.dumps({"db": "project body"})},
        {"skills_json": json.dumps({"reviewer": "session body"})},
    )
    assert merged.skills == {"reviewer": "session body", "db": "project body"}


def test_a_project_wide_deny_cannot_be_reopened_by_a_session() -> None:
    """The safety-critical case: permissions union with the strictest decision
    winning, regardless of which layer set it."""
    profile = _profile(
        claude_settings_json=json.dumps(
            {"permissions": {"deny": ["Bash(rm -rf /)"]}}
        )
    )
    merged = HARNESS.compose_project_layers(
        profile,
        None,
        {
            "claude_settings_json": json.dumps(
                {"permissions": {"allow": ["Bash(rm -rf /)"]}}
            )
        },
    )
    permissions = json.loads(merged.claude_settings_json)["permissions"]
    assert "Bash(rm -rf /)" in permissions["deny"]
    assert "Bash(rm -rf /)" not in permissions.get("allow", [])


def test_scalar_settings_use_the_most_specific_layer() -> None:
    profile = _profile(claude_settings_json=json.dumps({"model": "org-model"}))
    merged = HARNESS.compose_project_layers(
        profile,
        {"claude_settings_json": json.dumps({"model": "project-model"})},
        {"claude_settings_json": json.dumps({"model": "session-model"})},
    )
    assert json.loads(merged.claude_settings_json)["model"] == "session-model"


def test_a_harness_with_no_concept_of_this_is_unaffected() -> None:
    """OpenCode has no `compose_project_layers` override; the default no-op
    must not blow up when handed real rows."""
    opencode = get_harness("opencode")
    profile = _profile(claude_md="whatever")
    row = {"claude_md": "should be ignored"}
    assert opencode.compose_project_layers(profile, row, row) is profile
