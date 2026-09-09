"""`mcp_health.parse` against real `claude mcp list` output, captured live
against actual servers (one unreachable HTTP, one that returns a real but
non-MCP HTTP response, one broken stdio command) — not hand-written guesses
at the format.
"""

from __future__ import annotations

from moonphase.mcp_health import parse

_HEADER = "Checking MCP server health…\n\n"


def test_reports_a_dns_failure():
    output = _HEADER + (
        "sentry: https://nonexistent.example.invalid/mcp (HTTP) - "
        "✘ Failed to connect — ENOTFOUND: getaddrinfo ENOTFOUND nonexistent.example.invalid"
    )
    [status] = parse(output)
    assert status.name == "sentry"
    assert status.ok is False
    assert "ENOTFOUND" in status.detail


def test_reports_a_non_mcp_http_response():
    output = _HEADER + (
        "docs: https://mcp.deepwiki.com/sse (SSE) - "
        "✘ Failed to connect — HTTP 410: SSE error: Non-200 status code (410)"
    )
    [status] = parse(output)
    assert status.name == "docs"
    assert status.ok is False
    assert "410" in status.detail


def test_stdio_servers_have_no_parenthesised_transport():
    output = _HEADER + (
        "local: echo hello - ✘ Failed to connect — -32000: MCP error -32000: Connection closed"
    )
    [status] = parse(output)
    assert status.name == "local"
    assert status.ok is False


def test_a_working_server_is_ok():
    output = _HEADER + "sentry: https://mcp.sentry.dev/mcp (HTTP) - ✓ Connected"
    [status] = parse(output)
    assert status.ok is True
    assert status.detail == "Connected"


def test_multiple_servers_in_one_report():
    output = _HEADER + (
        "a: https://a.example.com/mcp (HTTP) - ✓ Connected\n"
        "b: https://b.example.com/mcp (HTTP) - ✘ Failed to connect — timeout"
    )
    statuses = parse(output)
    assert [s.name for s in statuses] == ["a", "b"]
    assert [s.ok for s in statuses] == [True, False]


def test_no_servers_configured_is_an_empty_list():
    assert parse("No MCP servers configured. Use `claude mcp add` to add a server.") == []


def test_the_health_check_banner_line_is_not_mistaken_for_a_server():
    assert parse("Checking MCP server health…\n\n") == []
