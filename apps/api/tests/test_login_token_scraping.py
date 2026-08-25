"""`_scrape_token` against a realistic captured pane, not a synthetic one-liner.

`_TOKEN_PATTERNS` (login.py) pulls a token out of whatever a `claude
setup-token` run printed to the PTY. The harness's own docstring admits the
prefix format "has changed before", and a miss falls through silently to
other harvest strategies — so a regression here would not fail loudly, it
would just stop working. This pins the patterns against a full pane capture
shaped the way `tmux capture-pane -p` actually renders one: banner, the
authorization URL, the code prompt, and the printed token — rather than a
single isolated line, so surrounding banner/prompt text can't accidentally
shadow the match.

The transcript below is synthetic-but-format-accurate (redacted, not a real
account's token) rather than a captured session, since a real one is
necessarily tied to a live account.
"""

from __future__ import annotations

from moonphase.login import _scrape_token

REAL_TOKEN = (
    "sk-ant-oat01-9v8u7t6s5r4q3p2o1n0m-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
    "mnopqrstuvwxyz0123456789_-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)

# Shaped like a real `claude setup-token` run: a banner, the authorization
# URL on its own line, the pasted-code prompt echoed back, then the printed
# token wrapped in the export line a user is told to copy. tmux's pane is 200
# columns wide (docker_remote's login container), so a token this length
# still fits on one line rather than wrapping.
SETUP_TOKEN_TRANSCRIPT = f"""
Claude Code

Setup Token

To grant Claude Code access to your account, open this URL:

  https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference&state=xyz789

Paste the authorization code here (including the state): AbCdEfGh01234567890#xyz789

Exchanging authorization code for tokens...

Login successful!

Your Claude Code OAuth token (store this securely, it will not be shown again):

CLAUDE_CODE_OAUTH_TOKEN={REAL_TOKEN}

Set this as an environment variable to authenticate non-interactively:

  export CLAUDE_CODE_OAUTH_TOKEN={REAL_TOKEN}
"""


def test_extracts_the_token_from_a_full_pane_capture() -> None:
    assert _scrape_token(SETUP_TOKEN_TRANSCRIPT) == REAL_TOKEN


def test_extracts_a_bare_sk_ant_token_with_no_env_assignment() -> None:
    """Some harness versions just print the token, with no CLAUDE_CODE_OAUTH_TOKEN=
    prefix at all — the first pattern has to stand on its own."""
    pane = f"Login successful. Your token:\n\n  {REAL_TOKEN}\n\nStore it somewhere safe.\n"
    assert _scrape_token(pane) == REAL_TOKEN


def test_extracts_the_token_when_only_the_env_assignment_form_is_present() -> None:
    """A prefix the first pattern would not recognise still has to be caught by
    the CLAUDE_CODE_OAUTH_TOKEN= fallback."""
    token = "cc-oat-a-differently-shaped-prefix-1234567890abcdef"
    pane = f"export CLAUDE_CODE_OAUTH_TOKEN='{token}'\n"
    assert _scrape_token(pane) == token


def test_no_token_in_the_pane_falls_through_quietly() -> None:
    pane = "Paste the authorization code here (including the state): "
    assert _scrape_token(pane) is None
