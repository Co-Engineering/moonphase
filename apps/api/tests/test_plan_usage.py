"""Each account's limits, as Claude Code's `/usage` shows them.

Read from the provider with the account's own sign-in. The parts worth
pinning: what the answer becomes, which token is used — never refreshed,
borrowed from a running session once the stored one has expired — and what
the page is told when it cannot be read.
"""

from __future__ import annotations

import json

import httpx
import pytest

from moonphase import plan_usage

NOW = 1_800_000_000.0


def creds(token: str, expires_in_s: float) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": "never-used",
                "expiresAt": int((NOW + expires_in_s) * 1000),
            }
        }
    )


SAMPLE = {
    "five_hour": {"utilization": 42.0, "resets_at": "2027-01-15T14:00:00Z"},
    "seven_day": {"utilization": 13, "resets_at": "2027-01-19T08:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 5.5, "resets_at": None},
    "extra_thing": {"utilization": 250, "resets_at": "garbage"},
}


def test_the_answer_becomes_windows_in_a_sensible_order() -> None:
    windows = plan_usage.parse_windows(SAMPLE)
    assert [(w.key, w.label) for w in windows] == [
        ("five_hour", "Current session"),
        ("seven_day", "Current week (all models)"),
        ("seven_day_sonnet", "Current week (Sonnet)"),
        ("extra_thing", "Extra thing"),
    ]
    assert windows[0].percent == 42.0
    assert windows[0].resets_at is not None and windows[0].resets_at.hour == 14
    # Clamped, and a reset time that is not a time is left out rather than guessed.
    assert windows[3].percent == 100.0 and windows[3].resets_at is None


def test_an_unexpected_answer_is_no_windows_rather_than_an_error() -> None:
    assert plan_usage.parse_windows(["not", "a", "dict"]) == []
    assert plan_usage.parse_windows({"five_hour": {"utilization": None}}) == []


def test_the_token_is_read_from_a_credentials_file() -> None:
    token = plan_usage.token_from_credentials_file(creds("abc", 3600))
    assert token is not None and token.token == "abc" and token.valid(NOW)
    assert plan_usage.token_from_credentials_file("not json") is None
    assert plan_usage.token_from_credentials_file(json.dumps({"mcpOAuth": {}})) is None


def test_the_freshest_valid_token_wins_and_expired_ones_never_do() -> None:
    expired = plan_usage.token_from_credentials_file(creds("old", -10))
    short = plan_usage.token_from_credentials_file(creds("short", 600))
    long = plan_usage.token_from_credentials_file(creds("long", 7200))
    assert plan_usage.freshest([expired, short, long, None], NOW).token == "long"  # type: ignore[union-attr]
    assert plan_usage.freshest([expired, None], NOW) is None


def _client(status: int, body: object) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["anthropic-beta"] == plan_usage.OAUTH_BETA
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fetch_returns_the_windows() -> None:
    async with _client(200, SAMPLE) as client:
        windows = await plan_usage.fetch("tok", client)
    assert windows[0].percent == 42.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "expired"), (403, "unsupported"), (429, "rate_limited"), (500, "error")],
)
async def test_fetch_says_why_it_could_not(status: int, reason: str) -> None:
    async with _client(status, {}) as client:
        with pytest.raises(plan_usage.UsageUnavailable) as err:
            await plan_usage.fetch("tok", client)
    assert err.value.reason == reason


# --- the route ---------------------------------------------------------------


def test_retry_after_is_read_as_seconds_or_a_date() -> None:
    assert plan_usage.retry_after_seconds("120", NOW) == 120
    assert plan_usage.retry_after_seconds(None, NOW) is None
    assert plan_usage.retry_after_seconds("not a time", NOW) is None
    assert plan_usage.retry_after_seconds("Thu, 01 Jan 1970 00:00:00 GMT", NOW) == 0


@pytest.mark.asyncio
async def test_fetch_passes_on_how_long_to_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "90"}, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(plan_usage.UsageUnavailable) as err:
            await plan_usage.fetch("tok", client)
    assert err.value.reason == "rate_limited" and err.value.retry_after == 90


@pytest.mark.asyncio
async def test_anthropics_own_message_is_passed_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"type": "permission_error", "message": "missing scope"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(plan_usage.UsageUnavailable) as err:
            await plan_usage.fetch("tok", client)
    assert "missing scope" in err.value.detail


# --- readings a session's status line saved ----------------------------------

STATUSLINE_DOC = {
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 41.2, "resets_at": NOW + 86400},
        "spend_limit": {"used_percentage": 5, "resets_at": NOW + 99},
    }
}


def test_a_status_line_reading_becomes_windows() -> None:
    windows = plan_usage.parse_statusline(STATUSLINE_DOC, NOW)
    assert [(w.key, w.percent) for w in windows] == [("five_hour", 23.5), ("seven_day", 41.2)]
    assert windows[0].resets_at is not None


def test_a_window_whose_reset_passed_is_empty_again() -> None:
    doc = {"rate_limits": {"five_hour": {"used_percentage": 90, "resets_at": NOW - 1}}}
    [w] = plan_usage.parse_statusline(doc, NOW)
    assert w.percent == 0 and w.resets_at is None


def test_an_odd_status_line_file_is_no_windows() -> None:
    assert plan_usage.parse_statusline({"model": {}}, NOW) == []
    assert plan_usage.parse_statusline([1], NOW) == []


