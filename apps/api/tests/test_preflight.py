"""Startup checks, and the sentences they produce.

What is worth testing here is not that a check notices — it is that what it
says is usable. Every one of these corresponds to a real troubleshooting entry
whose symptom, without this, arrives much later wearing a disguise: a wrong
SUPABASE_URL as "Invalid token" on every request, an unmigrated database as a
500 on the first page someone opens.

So each test asserts the finding names the variable at fault and says what to
do about it.
"""

from __future__ import annotations

import base64

import pytest

from moonphase import preflight
from moonphase.config import get_settings


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Settings are cached, and every test here changes them."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set(monkeypatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


# --- the encryption key -------------------------------------------------------


def test_a_missing_encryption_key_is_refused_before_settings_even_load(
    monkeypatch,
) -> None:
    """Earlier than preflight, and with the command to generate one — which is
    why preflight only checks the shape of a key that is present."""
    monkeypatch.setenv("MOONPHASE_SECRET_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(Exception) as caught:
        get_settings()

    assert "MOONPHASE_SECRET_KEY is required" in str(caught.value)
    assert "Fernet.generate_key" in str(caught.value)


def test_a_malformed_encryption_key_is_caught_before_it_is_used(monkeypatch) -> None:
    _set(monkeypatch, MOONPHASE_SECRET_KEY="obviously-not-a-fernet-key")
    finding = preflight.check_secret_key()

    assert finding is not None
    assert finding.fatal is True
    # The dangerous fix is to generate a new one, so the message warns first.
    assert "unreadable" in finding.fix


def test_a_valid_key_produces_no_finding(monkeypatch) -> None:
    key = base64.urlsafe_b64encode(b"x" * 32).decode()
    _set(monkeypatch, MOONPHASE_SECRET_KEY=key)

    assert preflight.check_secret_key() is None


# --- cors -----------------------------------------------------------------------


def test_a_wildcard_cors_origin_is_refused(monkeypatch) -> None:
    """Credentials are always on, so `*` would be silently reflected back for
    every caller rather than rejected — a footgun, not a valid config."""
    _set(monkeypatch, MOONPHASE_CORS_ORIGINS="*")
    finding = preflight.check_cors()

    assert finding is not None
    assert finding.fatal is True
    assert "MOONPHASE_CORS_ORIGINS" in finding.summary
    assert "*" in finding.summary


def test_a_wildcard_among_other_origins_is_still_refused(monkeypatch) -> None:
    _set(monkeypatch, MOONPHASE_CORS_ORIGINS="https://example.com,*")
    assert preflight.check_cors() is not None


def test_explicit_origins_produce_no_finding(monkeypatch) -> None:
    _set(monkeypatch, MOONPHASE_CORS_ORIGINS="https://example.com,https://app.example.com")
    assert preflight.check_cors() is None


# --- things that degrade rather than break ------------------------------------


def test_missing_push_keys_are_a_warning_not_a_failure(monkeypatch) -> None:
    """A smaller product, not a broken one."""
    _set(
        monkeypatch,
        MOONPHASE_VAPID_PUBLIC_KEY="",
        MOONPHASE_VAPID_PRIVATE_KEY="",
    )
    finding = preflight.check_push()

    assert finding is not None
    assert finding.fatal is False
    assert "gen_vapid" in finding.fix


def test_configured_push_produces_no_finding(monkeypatch) -> None:
    _set(
        monkeypatch,
        MOONPHASE_VAPID_PUBLIC_KEY="public",
        MOONPHASE_VAPID_PRIVATE_KEY="private",
    )

    assert preflight.check_push() is None


def test_a_disabled_monitor_says_what_it_costs(monkeypatch) -> None:
    """Turning it off is legitimate; not knowing what it turns off is not."""
    _set(monkeypatch, MOONPHASE_MONITOR_INTERVAL="0")
    finding = preflight.check_monitor()

    assert finding is not None
    assert finding.fatal is False
    assert "notifications" in finding.fix.lower()


def test_a_running_monitor_produces_no_finding(monkeypatch) -> None:
    _set(monkeypatch, MOONPHASE_MONITOR_INTERVAL="20")

    assert preflight.check_monitor() is None


# --- auth ---------------------------------------------------------------------


async def test_an_unset_supabase_url_stops_the_process(monkeypatch) -> None:
    _set(monkeypatch, SUPABASE_URL="")
    finding = await preflight.check_auth()

    assert finding is not None
    assert finding.fatal is True
    assert "SUPABASE_URL" in finding.summary


async def test_an_unreachable_auth_service_is_not_reported_when_a_secret_is_set(
    monkeypatch,
) -> None:
    """The address is the browser's, and the proxy serving it sits in front of
    this process rather than behind it — so it is normally unreachable from
    here, and with a shared secret nothing needs to reach it.

    Warning anyway fired on every correct install, which is worse than not
    checking: it teaches people to ignore warnings.
    """
    _set(
        monkeypatch,
        SUPABASE_URL="http://127.0.0.1:1",
        SUPABASE_JWT_SECRET="a-shared-secret-at-least-32-characters",
    )

    assert await preflight.check_auth() is None


async def test_an_unreachable_auth_service_is_a_warning_without_a_secret(
    monkeypatch,
) -> None:
    """Then tokens can only be verified against the JWKS published there, so
    every request really will be rejected."""
    _set(monkeypatch, SUPABASE_URL="http://127.0.0.1:1", SUPABASE_JWT_SECRET="")
    finding = await preflight.check_auth()

    assert finding is not None
    assert finding.fatal is False
    assert "invalid" in finding.fix.lower()
    assert "JWKS" in finding.fix


# --- reporting ----------------------------------------------------------------


def test_a_finding_reads_as_a_diagnosis_and_a_fix() -> None:
    finding = preflight.Finding(fatal=True, summary="Something is wrong.", fix="Do this.")

    assert finding.summary
    assert finding.fix


async def _no_database(monkeypatch) -> None:
    """The database check is exercised against a real one elsewhere. These two
    are about how findings are reported, and should not take twenty seconds of
    connection retries to say so."""
    async def ok() -> None:
        return None

    monkeypatch.setattr(preflight, "check_database", ok)


async def test_fatal_findings_stop_startup(monkeypatch) -> None:
    """A container that exits with a reason in its logs is far easier to
    diagnose than one that comes up and serves 500s."""
    await _no_database(monkeypatch)
    _set(monkeypatch, MOONPHASE_SECRET_KEY="present-but-not-a-key", SUPABASE_URL="")

    with pytest.raises(preflight.PreflightFailed) as caught:
        await preflight.run()

    # The exception carries the fixes, because it is what gets printed.
    assert "MOONPHASE_SECRET_KEY" in str(caught.value)
    assert "SUPABASE_URL" in str(caught.value)


async def test_warnings_alone_do_not_stop_startup(monkeypatch) -> None:
    await _no_database(monkeypatch)
    key = base64.urlsafe_b64encode(b"x" * 32).decode()
    _set(
        monkeypatch,
        MOONPHASE_SECRET_KEY=key,
        MOONPHASE_MONITOR_INTERVAL="0",
        MOONPHASE_VAPID_PUBLIC_KEY="",
        MOONPHASE_VAPID_PRIVATE_KEY="",
        SUPABASE_URL="http://127.0.0.1:1",
    )

    findings = await preflight.run()

    assert findings
    assert all(not finding.fatal for finding in findings)
