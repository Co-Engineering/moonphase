"""The docker_access migration adds what the rest of this feature assumes."""

from __future__ import annotations

from pathlib import Path


def _migration_text() -> str:
    return (
        Path(__file__).resolve().parents[3]
        / "supabase/migrations/20260828121500_project_docker_access.sql"
    ).read_text()


def test_the_migration_adds_a_not_null_column_defaulting_to_off() -> None:
    text = _migration_text()

    assert "add column if not exists docker_access boolean not null default false" in text


def test_the_column_is_documented() -> None:
    text = _migration_text()

    assert "comment on column public.projects.docker_access" in text
