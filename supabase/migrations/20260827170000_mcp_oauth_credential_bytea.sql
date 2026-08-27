-- ===========================================================================
-- The encrypted MCP credential is bytes, so its column is bytea
--
-- `encrypt()` returns Fernet ciphertext as bytes, and every other encrypted
-- column in this schema is bytea for that reason — private_key_enc,
-- passphrase_enc, token_enc, oauth_blob_enc, all of them. This one was
-- declared text, so writing to it failed at the driver with "expected str,
-- got bytes" and the API answered 500. A connection could be authorised and
-- then never stored.
--
-- Conditional because a database created after the original migration was
-- corrected already has bytea, and this must be a no-op there rather than an
-- error. Nothing is lost converting: no row was ever written, since every
-- write is what failed.
-- ===========================================================================

do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'private'
      and table_name = 'mcp_oauth_credentials'
      and column_name = 'credential_json_enc'
      and data_type = 'text'
  ) then
    alter table private.mcp_oauth_credentials
      alter column credential_json_enc type bytea
      using credential_json_enc::bytea;
  end if;
end $$;
