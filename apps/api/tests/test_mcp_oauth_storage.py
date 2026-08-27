"""An MCP server's OAuth token is a credential, and is stored like one.

Everything else this system keeps — SSH keys and passphrases, the harness's
API key and OAuth blob, the GitHub token, the SMTP and provider secrets — is
encrypted before it reaches a row. This one arrived storing a live OAuth token
as plain text, under a comment claiming the same trust model as the tables
that encrypt.

The private schema and row-level security keep it away from a signed-in
caller, and neither does anything about a backup, a dump, or anyone who can
read the table by other means. Encryption is what covers that.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
QUERIES = (REPO / "apps/api/moonphase/queries.py").read_text()
MIGRATION = (
    REPO / "supabase/migrations/20260827142100_mcp_oauth_credentials.sql"
).read_text()


def _function(name: str) -> str:
    start = QUERIES.index(f"async def {name}(")
    rest = QUERIES[start + 10 :]
    end = rest.index("\nasync def ") if "\nasync def " in rest else len(rest)
    return rest[:end]


def test_the_column_says_it_is_encrypted() -> None:
    """The `_enc` suffix is how every other credential column here says so, and
    is the thing a reader checks before trusting a table with a token."""
    assert "credential_json_enc" in MIGRATION
    assert not re.search(r"\bcredential_json\s+text", MIGRATION)


def test_it_is_encrypted_on_the_way_in() -> None:
    body = _function("upsert_mcp_oauth_credential_privileged")

    assert "encrypt(credential_json)" in body
    # And never the bare value, which is what it did.
    assert not re.search(r'"cred":\s*credential_json\s*,', body)


def test_it_is_decrypted_on_the_way_out() -> None:
    """Encrypting without decrypting would replay ciphertext into a session's
    credentials file, which fails in a way nobody would connect to this."""
    body = _function("get_mcp_oauth_credentials_privileged")

    assert "decrypt(" in body
    assert "credential_json_enc" in body


def test_the_listing_never_returns_the_token() -> None:
    """The connected-servers list is metadata, and a token in it would travel
    to the browser for no reason at all."""
    body = _function("list_mcp_oauth_credentials_privileged")

    assert "credential_json" not in body
    assert "server_name" in body
