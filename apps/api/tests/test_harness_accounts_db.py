"""Several Claude accounts per person, against a real database.

What matters is which account a session ends up running on, and that is
decided in SQL: the session's choice, then the project's, then the default.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from moonphase import queries
from moonphase.db import service_session


async def _org(conn) -> uuid.UUID:
    org_id = uuid.uuid4()
    await conn.execute(
        text(
            "insert into public.organizations (id, name, slug, is_personal) "
            "values (:id, :name, :slug, false)"
        ),
        {"id": org_id, "name": f"accounts {org_id.hex[:6]}", "slug": f"acc-{org_id.hex[:8]}"},
    )
    return org_id


async def _account(conn, org_id: uuid.UUID, label: str, key: str) -> uuid.UUID:
    row = await queries.add_harness_account_privileged(
        conn,
        org_id=org_id,
        harness="claude_code",
        auth_mode="api_key",
        label=label,
        api_key=key,
        oauth_token=None,
        oauth_blob=None,
        created_by=None,  # type: ignore[arg-type]
    )
    return row["id"]


async def _resolve(conn, org_id, project_id, account_id=None) -> str | None:
    row = await queries.resolve_harness_credential_privileged(
        conn, org_id=org_id, project_id=project_id, harness="claude_code",
        account_id=account_id,
    )
    return row["api_key"] if row else None


async def _cleanup(conn, *org_ids) -> None:
    for org_id in org_ids:
        await conn.execute(
            text("delete from public.organizations where id = :id"), {"id": org_id}
        )


@pytest.mark.asyncio
async def test_the_first_account_is_the_default_and_later_ones_are_not() -> None:
    async with service_session() as conn:
        org_id = await _org(conn)
        try:
            await _account(conn, org_id, "Personal", "sk-ant-personal")
            await _account(conn, org_id, "Work", "sk-ant-work")
            accounts = await queries.list_harness_accounts_privileged(
                conn, org_id, "claude_code"
            )
            assert [(a["label"], a["is_default"]) for a in accounts] == [
                ("Personal", True),
                ("Work", False),
            ]
            assert await _resolve(conn, org_id, org_id) == "sk-ant-personal"
        finally:
            await _cleanup(conn, org_id)


@pytest.mark.asyncio
async def test_a_session_runs_on_the_account_it_names() -> None:
    async with service_session() as conn:
        org_id = await _org(conn)
        try:
            await _account(conn, org_id, "Personal", "sk-ant-personal")
            work = await _account(conn, org_id, "Work", "sk-ant-work")
            assert await _resolve(conn, org_id, org_id, work) == "sk-ant-work"
        finally:
            await _cleanup(conn, org_id)


@pytest.mark.asyncio
async def test_someone_elses_account_is_never_used() -> None:
    async with service_session() as conn:
        mine = await _org(conn)
        theirs = await _org(conn)
        try:
            await _account(conn, mine, "Personal", "sk-ant-mine")
            stolen = await _account(conn, theirs, "Theirs", "sk-ant-theirs")
            assert await _resolve(conn, mine, mine, stolen) == "sk-ant-mine"
        finally:
            await _cleanup(conn, mine, theirs)


@pytest.mark.asyncio
async def test_switching_the_default_and_deleting_it() -> None:
    async with service_session() as conn:
        org_id = await _org(conn)
        try:
            personal = await _account(conn, org_id, "Personal", "sk-ant-personal")
            work = await _account(conn, org_id, "Work", "sk-ant-work")

            assert await queries.set_default_harness_account_privileged(conn, org_id, work)
            assert await _resolve(conn, org_id, org_id) == "sk-ant-work"

            # Removing the default leaves the remaining one in charge.
            assert await queries.delete_harness_account_privileged(conn, org_id, work)
            accounts = await queries.list_harness_accounts_privileged(
                conn, org_id, "claude_code"
            )
            assert [(a["id"], a["is_default"]) for a in accounts] == [(personal, True)]
        finally:
            await _cleanup(conn, org_id)


@pytest.mark.asyncio
async def test_a_project_runs_on_its_account_unless_the_session_names_another() -> None:
    async with service_session() as conn:
        org_id = await _org(conn)
        try:
            await _account(conn, org_id, "Personal", "sk-ant-personal")
            client = await _account(conn, org_id, "Client", "sk-ant-client")
            work = await _account(conn, org_id, "Work", "sk-ant-work")

            server_id = uuid.uuid4()
            await conn.execute(
                text(
                    "insert into public.servers (id, org_id, name, host, ssh_user, ssh_auth_mode) "
                    "values (:id, :org, 'acc-test', '127.0.0.1', 'dev', 'provided_key')"
                ),
                {"id": server_id, "org": org_id},
            )
            project_id = uuid.uuid4()
            await conn.execute(
                text(
                    "insert into public.projects (id, org_id, server_id, name, slug) "
                    "values (:id, :org, :server, 'acc test', 'acc-test')"
                ),
                {"id": project_id, "org": org_id, "server": server_id},
            )
            await queries.set_project_harness_account(conn, project_id, client)

            assert await _resolve(conn, org_id, project_id) == "sk-ant-client"
            assert await _resolve(conn, org_id, project_id, work) == "sk-ant-work"

            # The account going away puts the project back on the default.
            await queries.delete_harness_account_privileged(conn, org_id, client)
            assert await _resolve(conn, org_id, project_id) == "sk-ant-personal"
        finally:
            await _cleanup(conn, org_id)


@pytest.mark.asyncio
async def test_signing_in_without_naming_an_account_replaces_the_default() -> None:
    async with service_session() as conn:
        org_id = await _org(conn)
        try:
            await _account(conn, org_id, "Personal", "sk-ant-old")
            await _account(conn, org_id, "Work", "sk-ant-work")
            await queries.upsert_harness_credential_privileged(
                conn,
                org_id=org_id,
                project_id=None,
                harness="claude_code",
                auth_mode="api_key",
                label=None,
                api_key="sk-ant-new",
                oauth_blob=None,
                created_by=None,  # type: ignore[arg-type]
            )
            accounts = await queries.list_harness_accounts_privileged(
                conn, org_id, "claude_code"
            )
            assert len(accounts) == 2
            assert accounts[0]["label"] == "Personal"
            assert await _resolve(conn, org_id, org_id) == "sk-ant-new"
        finally:
            await _cleanup(conn, org_id)


# --- each account's plan limits, kept (see account_usage) ----------------------


def _windows(kept: dict) -> list:
    # jsonb comes back as text on this driver; account_usage.from_json takes both.
    import json

    raw = kept["windows"]
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.mark.asyncio
async def test_a_kept_reading_is_only_ever_replaced_by_a_newer_one() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    windows = [{"key": "five_hour", "label": "Current session", "percent": 40.0,
                "resets_at": (now + timedelta(hours=2)).timestamp()}]
    async with service_session() as conn:
        org_id = await _org(conn)
        try:
            account = await _account(conn, org_id, "Personal", "sk-ant-personal")
            assert await queries.get_account_usage_privileged(conn, [account]) == {}

            await queries.save_account_reading_privileged(
                conn, account_id=account, windows=windows, read_at=now, source="statusline"
            )
            older = [{**windows[0], "percent": 5.0}]
            await queries.save_account_reading_privileged(
                conn, account_id=account, windows=older,
                read_at=now - timedelta(minutes=10), source="statusline",
            )
            kept = (await queries.get_account_usage_privileged(conn, [account]))[account]
            assert _windows(kept)[0]["percent"] == 40.0
            assert kept["read_at"] == now and kept["problem"] is None

            # A failure keeps the reading and says why, until a newer reading.
            await queries.save_account_problem_privileged(
                conn, account_id=account, problem="rate_limited", detail="No.",
                retry_at=now + timedelta(minutes=5), refusals=1,
            )
            kept = (await queries.get_account_usage_privileged(conn, [account]))[account]
            assert kept["problem"] == "rate_limited" and _windows(kept)[0]["percent"] == 40.0
            await queries.save_account_reading_privileged(
                conn, account_id=account, windows=windows,
                read_at=now + timedelta(minutes=1), source="session",
            )
            kept = (await queries.get_account_usage_privileged(conn, [account]))[account]
            assert kept["problem"] is None and kept["refusals"] == 0
            assert kept["source"] == "session"
        finally:
            # Removing the account removes what was kept for it.
            await _cleanup(conn, org_id)
            left = await conn.execute(
                text("select count(*) from private.harness_account_usage where account_id = :a"),
                {"a": account},
            )
            assert left.scalar() == 0


@pytest.mark.asyncio
async def test_only_signed_in_claude_subscriptions_are_offered_for_asking() -> None:
    async with service_session() as conn:
        org_id = await _org(conn)
        try:
            await _account(conn, org_id, "Key", "sk-ant-key")
            row = await queries.add_harness_account_privileged(
                conn, org_id=org_id, harness="claude_code", auth_mode="oauth",
                label="Sub", api_key=None, oauth_token=None,
                oauth_blob='{"claudeAiOauth": {"accessToken": "t"}}',
                created_by=None,  # type: ignore[arg-type]
            )
            offered = {
                a["id"]: a for a in await queries.claude_accounts_for_usage_privileged(conn)
            }
            assert row["id"] in offered
            assert "accessToken" in (offered[row["id"]]["oauth_blob"] or "")
            assert offered[row["id"]]["read_at"] is None
        finally:
            await _cleanup(conn, org_id)
