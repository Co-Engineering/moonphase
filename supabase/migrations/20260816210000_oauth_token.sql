-- ===========================================================================
-- Store an OAuth *token* alongside an OAuth credential *file*.
--
-- Claude Code's `setup-token` mode yields a long-lived token intended to be
-- exported as CLAUDE_CODE_OAUTH_TOKEN, whereas an interactive login persists a
-- ~/.claude/.credentials.json. Both are "oauth" as far as the user is
-- concerned, and either is enough to authenticate, so the credential row has
-- to be able to carry whichever the flow actually produced.
--
-- Keeping them in separate columns rather than overloading api_key_enc means
-- the injection path stays unambiguous: a token becomes an environment
-- variable, a blob becomes a file, and an API key stays an API key.
-- ===========================================================================

alter table private.harness_credentials
  add column if not exists oauth_token_enc bytea;

comment on column private.harness_credentials.oauth_token_enc is
  'Long-lived OAuth token, exported to the harness as an environment variable.';

comment on column private.harness_credentials.oauth_blob_enc is
  'The harness''s own credential file, written verbatim into the container.';
