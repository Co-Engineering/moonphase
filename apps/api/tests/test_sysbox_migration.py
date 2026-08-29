"""The Sysbox migration actually adds what the rest of this feature assumes.

A thin regression guard: `queries.py`/`schemas.py` read and write
`servers.sysbox_version`/`sysbox_status_detail` on the assumption the columns
exist and are documented — this catches the migration file drifting from
that assumption.
"""

from __future__ import annotations

from pathlib import Path


def _migration_text() -> str:
    return (
        Path(__file__).resolve().parents[3]
        / "supabase/migrations/20260828120000_sysbox_support.sql"
    ).read_text()


def test_the_migration_adds_both_columns_as_nullable() -> None:
    text = _migration_text()

    assert "add column if not exists sysbox_version text" in text
    assert "add column if not exists sysbox_status_detail text" in text
    # Neither is `not null` — both start unset until a bootstrap probes them,
    # unlike project-level columns that default to a concrete value.
    assert "sysbox_version text not null" not in text
    assert "sysbox_status_detail text not null" not in text


def test_both_columns_are_documented() -> None:
    text = _migration_text()

    assert "comment on column public.servers.sysbox_version" in text
    assert "comment on column public.servers.sysbox_status_detail" in text
