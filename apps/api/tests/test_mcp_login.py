"""Pure-logic pieces of the MCP OAuth relay.

`LOGIN_URL_PATTERN` is checked against real captured output from `claude mcp
login --no-browser` (against a live server, mcp.sentry.dev) rather than
invented text, since a plausible-looking regex is exactly the kind of thing
that quietly fails to match the harness's actual formatting.
"""

from __future__ import annotations

from moonphase.mcp_login import LOGIN_URL_PATTERN, McpLoginSession, _last_nonblank_line

# Captured verbatim from `claude mcp login sentry-test --no-browser` in a real
# pty, with the pane hard-wrapped the way `_prepare` rejoins before matching.
REAL_PANE = (
    'Starting authentication for "sentry-test"…\n'
    "Visit this URL to authorize:\n"
    "  https://mcp.sentry.dev/oauth/authorize?response_type=code&client_id="
    "https%3A%2F%2Fclaude.ai%2Foauth%2Fclaude-code-client-metadata&code_challenge="
    "lBWvxuZ0TkNWXjqcyk9tTVxkhq9_qsY1HzrckxPgqL0&code_challenge_method=S256&"
    "redirect_uri=http%3A%2F%2Flocalhost%3A49488%2Fcallback&state="
    "4jBWU5ITs09B5DcN7dGongPDCK9_ETWHdOia2dWLvZw&scope=org%3Aread+project%3Awrite"
    "+team%3Awrite+event%3Awrite&resource=https%3A%2F%2Fmcp.sentry.dev%2Fmcp\n"
    "\n"
    "Waiting for authorization… (^C to cancel)\n"
    "Or paste the redirect URL here: \n"
)

REAL_FAILURE_PANE = REAL_PANE + (
    "https://example.com/callback?code=bogus&state=bogus\n"
    "\n"
    'Couldn\'t complete authentication for "sentry-test": OAuth state mismatch '
    "- possible CSRF attack\n"
)


def test_finds_the_real_authorization_url() -> None:
    match = LOGIN_URL_PATTERN.search(REAL_PANE.replace("\n", ""))
    assert match is not None
    url = match.group(0).rstrip(")]},.")
    assert url.startswith("https://mcp.sentry.dev/oauth/authorize?")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A49488%2Fcallback" in url


def test_last_nonblank_line_is_the_actual_error() -> None:
    line = _last_nonblank_line(REAL_FAILURE_PANE)
    assert line is not None
    assert "state mismatch" in line.lower()


def test_last_nonblank_line_of_empty_pane_is_none() -> None:
    assert _last_nonblank_line("\n\n  \n") is None


def test_session_expiry() -> None:
    session = McpLoginSession(
        id="s1", org_id="o1", project_id="p1", session_name="mine",
        server_name="sentry-test", home="/home/dev/sessions/mine",
        container="c1", tmux_session="moonphase-mcp-login-abc",
    )
    assert not session.expired
    session.created_at -= 10_000  # far in the past
    assert session.expired
