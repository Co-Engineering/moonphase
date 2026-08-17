"""Warning someone before a limit stops them.

The failure that matters is not missing an alert — it is sending the same one
every two minutes for the rest of a five-hour window. A threshold crossed at
60% stays crossed, so "have I already said this" has to be answered by the
window itself rather than by a flag someone remembers to clear.

Runs against a local `supabase start`. Skipped when the database is unreachable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from test_rls import _database_reachable, _make_user

from moonphase.db import service_session
from moonphase.monitor import SessionMonitor


@pytest.fixture
async def user_with_usage():
    """A user, an allowance, and enough tokens to have crossed it."""
    if not await _database_reachable():
        pytest.skip("database not reachable")

    user_id = await _make_user(f"budget-{uuid.uuid4().hex[:8]}@example.test")
    now = datetime.now(UTC)
    async with service_session() as conn:
        await conn.execute(
            text(
                """
                insert into usage_events
                  (user_id, project_name, model, message_id, at,
                   input_tokens, output_tokens)
                values (cast(:u as uuid), 'p', 'claude-sonnet-5', :mid, :at, 800, 200)
                """
            ),
            {"u": user_id, "mid": uuid.uuid4().hex, "at": now - timedelta(minutes=5)},
        )
    yield user_id
    async with service_session() as conn:
        await conn.execute(
            text("delete from auth.users where id = cast(:u as uuid)"), {"u": user_id}
        )


async def _set_limits(user_id: str, **fields: object) -> None:
    async with service_session() as conn:
        await conn.execute(
            text(
                """
                insert into usage_limits
                  (user_id, session_tokens, weekly_tokens, alert_percent)
                values (cast(:u as uuid), :session_tokens, :weekly_tokens, :alert_percent)
                on conflict (user_id) do update set
                  session_tokens = excluded.session_tokens,
                  weekly_tokens  = excluded.weekly_tokens,
                  alert_percent  = excluded.alert_percent,
                  alerted_window = null,
                  alerted_week   = null
                """
            ),
            {
                "u": user_id,
                "session_tokens": fields.get("session_tokens"),
                "weekly_tokens": fields.get("weekly_tokens"),
                "alert_percent": fields.get("alert_percent"),
            },
        )


async def _anchor(user_id: str, column: str):
    async with service_session() as conn:
        row = (
            await conn.execute(
                text(
                    f"select {column} from usage_limits where user_id = cast(:u as uuid)"
                ),
                {"u": user_id},
            )
        ).first()
    return row[0] if row else None


async def test_an_alert_fires_once_per_window(user_with_usage) -> None:
    """1000 tokens used against a 1200 allowance is past a 50% threshold."""
    await _set_limits(user_with_usage, weekly_tokens=1200, alert_percent=50)
    monitor = SessionMonitor()

    assert await monitor.check_budgets() == 1
    # The threshold is still crossed, and will be for the rest of the window.
    assert await monitor.check_budgets() == 0
    assert await _anchor(user_with_usage, "alerted_week") is not None


async def test_nothing_fires_below_the_threshold(user_with_usage) -> None:
    await _set_limits(user_with_usage, weekly_tokens=1_000_000, alert_percent=90)
    assert await SessionMonitor().check_budgets() == 0


async def test_a_threshold_without_an_allowance_is_not_a_percentage(
    user_with_usage,
) -> None:
    """Nothing to be a share of, so there is nothing to warn about."""
    await _set_limits(user_with_usage, weekly_tokens=None, alert_percent=1)
    assert await SessionMonitor().check_budgets() == 0


async def test_a_new_window_rearms_the_alert(user_with_usage) -> None:
    """Comparing anchors rather than storing a flag means it rearms itself."""
    await _set_limits(user_with_usage, weekly_tokens=1200, alert_percent=50)
    monitor = SessionMonitor()
    assert await monitor.check_budgets() == 1

    # Pretend the recorded anchor belongs to a window that has since rolled.
    async with service_session() as conn:
        await conn.execute(
            text(
                "update usage_limits set alerted_week = :old "
                "where user_id = cast(:u as uuid)"
            ),
            {"old": datetime.now(UTC) - timedelta(days=30), "u": user_with_usage},
        )

    assert await monitor.check_budgets() == 1


async def test_both_windows_are_checked_independently(user_with_usage) -> None:
    await _set_limits(
        user_with_usage, session_tokens=1200, weekly_tokens=1200, alert_percent=50
    )

    # One for the five-hour window and one for the week.
    assert await SessionMonitor().check_budgets() == 2
