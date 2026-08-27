"""The encrypted credential has to survive a real database round trip.

`encrypt()` returns bytes. Every other encrypted column in this schema is
bytea for that reason; this one was declared text, so the write failed at the
driver with "expected str, got bytes" and the API answered 500 — a connection
could be authorised and then never stored, and the browser saw only a JSON
parse error from an HTML error page.

Checked against a real Postgres, because that is the only place the mismatch
appears: encrypting and decrypting in memory works perfectly either way.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from moonphase import queries
from moonphase.db import service_session

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "supabase/migrations/20260827142100_mcp_oauth_credentials.sql"
).read_text()


def test_the_column_is_bytea_like_every_other_encrypted_one() -> None:
    """Read from the migration, so a fresh install is right from the start
    rather than relying on the follow-up that repairs an existing one."""
    assert "credential_json_enc bytea" in MIGRATION


@pytest.mark.asyncio
async def test_a_credential_survives_being_stored_and_read_back() -> None:
    org_id = uuid.uuid4()
    server = f"portal-{uuid.uuid4().hex[:8]}"
    payload = json.dumps({f"{server}|abc123": {"access_token": "not-a-real-token"}})

    async with service_session() as conn:
        await conn.execute(
            text(
                "insert into public.organizations (id, name, slug, is_personal) "
                "values (:id, :name, :slug, false) on conflict do nothing"
            ),
            {
                "id": org_id,
                "name": f"round-trip {org_id.hex[:6]}",
                "slug": f"round-trip-{org_id.hex[:8]}",
            },
        )
        try:
            await queries.upsert_mcp_oauth_credential_privileged(
                conn,
                org_id=org_id,
                server_name=server,
                credential_json=payload,
                created_by=None,
            )
            stored = await queries.get_mcp_oauth_credentials_privileged(conn, org_id)

            # Back out exactly as it went in, having been ciphertext in between.
            assert stored[server] == payload

            raw = await conn.execute(
                text(
                    "select credential_json_enc from private.mcp_oauth_credentials "
                    "where org_id = :org_id and server_name = :name"
                ),
                {"org_id": org_id, "name": server},
            )
            on_disk = bytes(raw.scalar_one())
            # Fernet ciphertext, not the plaintext anyone could read.
            assert b"not-a-real-token" not in on_disk
            assert on_disk.startswith(b"gAAAAA")
        finally:
            await conn.execute(
                text("delete from public.organizations where id = :id"), {"id": org_id}
            )
