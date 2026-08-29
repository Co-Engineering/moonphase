"""Composing org, project and session environment variables.

Unlike the Claude-specific fields (settings/CLAUDE.md/MCP/skills), env vars
apply the same way regardless of which harness a project runs, so they are
combined directly in runtime.py rather than through a harness's own
compose_project_layers — this is what pins that down.
"""

from __future__ import annotations

from moonphase.profile import WorkspaceProfile
from moonphase.runtime import _with_env_layers


def _profile(**env: str) -> WorkspaceProfile:
    return WorkspaceProfile(org_id="org", env_vars=env)


def test_no_project_or_session_env_is_a_true_no_op() -> None:
    profile = _profile(A="org-a")
    assert _with_env_layers(profile, None, None) is profile
    empty = {"env_vars": {}}
    assert _with_env_layers(profile, empty, empty) is profile


def test_session_wins_over_project_wins_over_org() -> None:
    profile = _profile(A="org-a", B="org-b")
    merged = _with_env_layers(
        profile,
        {"env_vars": {"B": "project-b", "C": "project-c"}},
        {"env_vars": {"C": "session-c"}},
    )
    assert merged.env_vars == {"A": "org-a", "B": "project-b", "C": "session-c"}


def test_project_only_still_layers_without_a_session() -> None:
    profile = _profile(A="org-a")
    merged = _with_env_layers(profile, {"env_vars": {"B": "project-b"}}, None)
    assert merged.env_vars == {"A": "org-a", "B": "project-b"}


def test_string_and_dict_shaped_env_vars_both_parse() -> None:
    """jsonb comes back as a dict from some query paths and as text from
    others — the parser has to accept whichever it is handed."""
    import json

    profile = _profile()
    as_dict = {"env_vars": {"A": "from-dict"}}
    as_text = {"env_vars": json.dumps({"A": "from-text"})}

    assert _with_env_layers(profile, as_dict, None).env_vars == {"A": "from-dict"}
    assert _with_env_layers(profile, as_text, None).env_vars == {"A": "from-text"}
