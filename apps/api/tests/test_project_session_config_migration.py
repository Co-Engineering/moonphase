"""The project_session_config split moves session-scope Claude config off
project_sessions into its own table with its own, narrower SELECT policy --
see the migration's own header for why. No live database here (there is none
in this sandbox); these pin the migration file's structure the way
test_project_docker_access_migration.py already does for its own migration.
"""

from __future__ import annotations

from pathlib import Path


def _migration_text() -> str:
    return (
        Path(__file__).resolve().parents[3]
        / "supabase/migrations/20260831120000_project_session_config_split.sql"
    ).read_text()


def test_the_five_columns_leave_project_sessions() -> None:
    text = _migration_text()

    for column in ("claude_settings_json", "claude_md", "mcp_json", "skills_json", "env_vars"):
        assert f"drop column {column}" in text


def test_existing_sessions_are_backfilled_before_the_columns_are_dropped() -> None:
    text = _migration_text()

    backfill = text.index("insert into public.project_session_config")
    drop = text.index("alter table public.project_sessions\n  drop column")
    assert backfill < drop, "backfill must run while the source columns still exist"


def test_a_trigger_gives_every_new_session_a_config_row() -> None:
    text = _migration_text()

    assert "security definer" in text
    assert "after insert on public.project_sessions" in text


def test_the_select_policy_is_owner_or_admin_only() -> None:
    text = _migration_text()

    policy = text[text.index("create policy project_session_config_select") :]
    assert "ps.user_id = auth.uid()" in policy
    assert "public.project_access(ps.project_id) = 'admin'" in policy
    # Not the three-way admin/write/read a plain observer would satisfy.
    assert "'read'" not in policy


def test_there_is_no_insert_policy_for_application_code() -> None:
    """Every row is created by the trigger; a client-facing INSERT policy
    would be a second, unaudited way to create one."""
    text = _migration_text()

    assert "for insert" not in text
