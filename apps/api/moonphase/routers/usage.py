"""What you have spent, framed by how you pay.

The same rows answer two different questions, and putting the wrong one first
makes the screen useless. On a subscription the limit is a window that opened
when you started working, so what matters is how much of it has gone and when
it comes back. On an API key the limit is money, so what matters is the bill.
Which is which is decided by the credential the person actually connected, not
by a preference they have to set.

The one number transcripts cannot give is the size of a subscription's
allowance, so here it is something you tell it once, and the percentage
appears after you do. The provider's own reading of each account's windows is
`/accounts` below — see `plan_usage` for why that one does ask Anthropic.
Everything else — when the window opened, when it resets, what went into
it — comes from data already collected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import plan_usage, profile, queries, runtime, ssh, usage
from ..auth import Principal, current_principal
from ..db import service_session, user_session
from ..harness import SessionSpace
from ..harness import get as get_harness
from ..schemas import (
    AccountUsageOut,
    LiveWindowOut,
    ModelPriceIn,
    ModelPriceOut,
    UsageLimitsIn,
    UsageLimitsOut,
    UsageOut,
    UsageProjectOut,
    UsageSliceOut,
    UsageWindowOut,
)
from ..ssh import SSHError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _overrides(rows: list[dict]) -> dict[str, usage.Price]:
    """Rates someone set here, which win over the built-in table."""
    return {
        str(row["model"]): usage.tiered(
            float(row["input_per_m"]), float(row["output_per_m"])
        )
        for row in rows
    }


def _totals(
    rows: list[dict], overrides: dict[str, usage.Price] | None = None
) -> tuple[usage.Totals, list[UsageSliceOut], float | None]:
    """Sum rows, price each model, and report cost only where a rate is known."""
    overall = usage.Totals()
    slices: list[UsageSliceOut] = []
    # No rows is not the same as no rate: nothing was used, so nothing was
    # spent, and a period with no activity should read as zero rather than as
    # "we could not work it out".
    cost: float | None = 0.0 if not rows else None

    for row in rows:
        model_totals = usage.Totals(
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_write_5m_tokens=int(row["cache_write_5m_tokens"]),
            cache_write_1h_tokens=int(row["cache_write_1h_tokens"]),
            thinking_tokens=int(row.get("thinking_tokens") or 0),
        )
        overall.add(model_totals)
        model_cost = usage.cost_of(model_totals, str(row["model"]), overrides)
        if model_cost is not None:
            cost = (cost or 0.0) + model_cost
        slices.append(
            UsageSliceOut(
                model=str(row["model"]),
                tokens=model_totals.total,
                input_tokens=model_totals.input_tokens,
                output_tokens=model_totals.output_tokens,
                cache_read_tokens=model_totals.cache_read_tokens,
                cache_write_tokens=(
                    model_totals.cache_write_5m_tokens
                    + model_totals.cache_write_1h_tokens
                ),
                thinking_tokens=model_totals.thinking_tokens,
                # Null rather than zero: "no rate for this model" and "free"
                # are different facts, and showing $0.00 for the first is a
                # confident lie about someone's bill.
                cost=model_cost,
                priced=model_cost is not None,
            )
        )

    slices.sort(key=lambda item: item.tokens, reverse=True)
    return overall, slices, cost


async def _window(
    conn,
    *,
    length: timedelta,
    now: datetime,
    limit: int | None,
    overrides: dict[str, usage.Price],
    label: str,
) -> UsageWindowOut:
    """One limit period: when it opened, what has gone into it, when it returns.

    Anchored to the first message that opened it rather than measured backwards
    from now. A trailing sum answers a question nobody asked and disagrees with
    what the harness itself reports.
    """
    times = await queries.usage_times(conn, now - length)
    window = usage.current_window(times, length, now)
    if window is None:
        # Nothing has opened a window, so nothing is being consumed. The next
        # message starts the clock.
        return UsageWindowOut(label=label, hours=int(length.total_seconds() // 3600))

    rows = await queries.usage_between(conn, window.started_at, window.resets_at)
    totals, _, cost = _totals(rows, overrides)
    return UsageWindowOut(
        label=label,
        hours=int(length.total_seconds() // 3600),
        started_at=window.started_at,
        resets_at=window.resets_at,
        tokens=totals.total,
        cost=cost,
        limit_tokens=limit,
        # Only when an allowance is actually known. A bar drawn against a
        # number nobody supplied is decoration pretending to be information.
        percent=(
            min(100.0, round(totals.total / limit * 100, 1))
            if limit and limit > 0
            else None
        ),
    )


@router.get("", response_model=UsageOut)
async def read_usage(
    hours: int = Query(default=24 * 7, ge=1, le=24 * 90),
    principal: Principal = Depends(current_principal),
) -> UsageOut:
    """Consumption over a span, plus the windows a limit is expressed in."""
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)

    async with user_session(principal.claims) as conn:
        overrides = _overrides(await queries.list_model_prices(conn))
        limits = await queries.get_usage_limits(conn) or {}
        rows = await queries.usage_since(conn, since)
        project_rows = await queries.usage_by_project(conn, since)
        buckets = await queries.usage_buckets(
            conn, since, "day" if hours > 48 else "hour"
        )
        session_window = await _window(
            conn,
            length=usage.SESSION_WINDOW,
            now=now,
            limit=limits.get("session_tokens"),
            overrides=overrides,
            label="Current session",
        )
        week_window = await _window(
            conn,
            length=usage.WEEK_WINDOW,
            now=now,
            limit=limits.get("weekly_tokens"),
            overrides=overrides,
            label="Current week",
        )

    overall, slices, cost = _totals(rows, overrides)

    # Which framing to lead with is a property of how they pay, so it is read
    # from the credential rather than asked for.
    async with service_session() as conn:
        credential = await queries.first_harness_credential_privileged(
            conn, str(principal.user_id)
        )
    billing = str(credential or "unknown")

    projects: dict[str, UsageProjectOut] = {}
    for row in project_rows:
        name = str(row["project_name"])
        entry = projects.setdefault(
            name, UsageProjectOut(project_id=row.get("project_id"), project_name=name)
        )
        model_totals = usage.Totals(
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_write_5m_tokens=int(row["cache_write_5m_tokens"]),
            cache_write_1h_tokens=int(row["cache_write_1h_tokens"]),
        )
        entry.tokens += model_totals.total
        model_cost = usage.cost_of(model_totals, str(row["model"]), overrides)
        if model_cost is not None:
            entry.cost = (entry.cost or 0.0) + model_cost

    merged: dict[str, int] = {}
    for row in buckets:
        key = row["bucket"].isoformat()
        merged[key] = merged.get(key, 0) + (
            int(row["input_tokens"])
            + int(row["output_tokens"])
            + int(row["cache_read_tokens"])
            + int(row["cache_write_5m_tokens"])
            + int(row["cache_write_1h_tokens"])
        )

    return UsageOut(
        billing=billing,
        hours=hours,
        tokens=overall.total,
        cost=cost,
        session_window=session_window,
        week_window=week_window,
        models=slices,
        projects=sorted(projects.values(), key=lambda p: p.tokens, reverse=True),
        series=[{"at": at, "tokens": tokens} for at, tokens in sorted(merged.items())],
    )


@router.get("/limits", response_model=UsageLimitsOut)
async def read_limits(
    principal: Principal = Depends(current_principal),
) -> UsageLimitsOut:
    async with user_session(principal.claims) as conn:
        row = await queries.get_usage_limits(conn) or {}
    return UsageLimitsOut(
        session_tokens=row.get("session_tokens"),
        weekly_tokens=row.get("weekly_tokens"),
        alert_percent=row.get("alert_percent"),
    )


@router.put("/limits", response_model=UsageLimitsOut)
async def write_limits(
    payload: UsageLimitsIn, principal: Principal = Depends(current_principal)
) -> UsageLimitsOut:
    """Tell Moonphase what your plan allows, so a percentage means something."""
    async with user_session(principal.claims) as conn:
        try:
            row = await queries.set_usage_limits(
                conn,
                user_id=principal.user_id,
                session_tokens=payload.session_tokens,
                weekly_tokens=payload.weekly_tokens,
                alert_percent=payload.alert_percent,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return UsageLimitsOut(
        session_tokens=row.get("session_tokens"),
        weekly_tokens=row.get("weekly_tokens"),
        alert_percent=row.get("alert_percent"),
    )


@router.get("/prices", response_model=list[ModelPriceOut])
async def list_prices(
    principal: Principal = Depends(current_principal),
) -> list[ModelPriceOut]:
    """Rates set here, alongside the ones that ship with Moonphase."""
    async with user_session(principal.claims) as conn:
        rows = await queries.list_model_prices(conn)
    theirs = [
        ModelPriceOut(
            model=str(row["model"]),
            input_per_m=float(row["input_per_m"]),
            output_per_m=float(row["output_per_m"]),
            builtin=False,
        )
        for row in rows
    ]
    known = {price.model for price in theirs}
    builtin = [
        ModelPriceOut(
            model=model,
            input_per_m=price.input,
            output_per_m=price.output,
            builtin=True,
        )
        for model, price in usage.DEFAULT_PRICES.items()
        if model not in known
    ]
    return sorted([*theirs, *builtin], key=lambda p: p.model)


@router.put("/prices", response_model=ModelPriceOut)
async def set_price(
    payload: ModelPriceIn, principal: Principal = Depends(current_principal)
) -> ModelPriceOut:
    """Teach Moonphase what a model costs.

    Cache rates are derived rather than asked for: the multipliers are the
    provider's and are the same for every model, so making someone enter five
    numbers where two will do is four chances to get it wrong.
    """
    async with user_session(principal.claims) as conn:
        try:
            org_id = await queries.resolve_org(conn, payload.org_id)
            row = await queries.upsert_model_price(
                conn,
                org_id=org_id,
                model=payload.model,
                input_per_m=payload.input_per_m,
                output_per_m=payload.output_per_m,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ModelPriceOut(
        model=str(row["model"]),
        input_per_m=float(row["input_per_m"]),
        output_per_m=float(row["output_per_m"]),
        builtin=False,
    )


@router.delete("/prices/{model}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_price(
    model: str, principal: Principal = Depends(current_principal)
) -> None:
    async with user_session(principal.claims) as conn:
        org_id = await queries.resolve_org(conn, None)
        await queries.delete_model_price(conn, org_id, model)


# How many of an account's running sessions to look in for a current token
# before giving up. Each one costs an SSH round trip.
_BORROW_ATTEMPTS = 3


async def _borrow_tokens(
    principal: Principal, sessions: list[tuple[dict[str, Any], dict[str, Any]]], now: float
) -> list[plan_usage.AccountToken]:
    """Current access tokens from your running sessions on an account.

    Read, never refreshed: Claude refreshes its own, and doing it here would
    spend the refresh token that session relies on. An idle session's token
    can have expired too — Claude only refreshes when it next needs it — so
    those are skipped.
    """
    found: list[plan_usage.AccountToken] = []
    harness = get_harness("claude_code")
    for session, project in sessions[:_BORROW_ATTEMPTS]:
        try:
            ctx = await runtime.load_project_context(
                principal.claims, project["id"], require=runtime.CAN_OBSERVE
            )
            conn_ssh = await ssh.pool.get(ctx.target)
            path = harness.credentials_merge_target(
                SessionSpace(home=str(session["home_dir"]))
            )
            if path is None:
                return found
            contents = await profile.read_file(conn_ssh, ctx.container, path)
        except (SSHError, runtime.NotFound, runtime.Forbidden) as exc:
            log.debug("could not read a token from %s: %s", session.get("tmux_session"), exc)
            continue
        token = plan_usage.token_from_credentials_file(contents)
        if token is not None and token.valid(now):
            found.append(token)
    return found


def _windows_out(windows: list[plan_usage.Window]) -> list[LiveWindowOut]:
    return [
        LiveWindowOut(key=w.key, label=w.label, percent=w.percent, resets_at=w.resets_at)
        for w in windows
    ]


async def _account_usage(
    principal: Principal,
    account: dict[str, Any],
    material: dict[str, Any] | None,
    sessions: list[tuple[dict[str, Any], dict[str, Any]]],
    now: float,
) -> AccountUsageOut:
    account_id = str(account["id"])
    out = AccountUsageOut(
        account_id=account["id"],
        label=account.get("label"),
        auth_mode=account["auth_mode"],
        is_default=bool(account.get("is_default")),
        status="ok",
    )
    if account["auth_mode"] == "api_key":
        out.status = "api_key"
        out.detail = "Billed per token, so there are no plan limits to show."
        return out

    async with plan_usage.lock_for(account_id):
        return await _read_account(principal, out, account_id, material, sessions, now)


def _fill_from_last(out: AccountUsageOut, account_id: str) -> None:
    last = plan_usage.last_known(account_id)
    if last is not None:
        out.windows = _windows_out(last.windows)
        out.fetched_at = datetime.fromtimestamp(last.fetched_at, tz=UTC)
        out.stale = True


def _slow_down(out: AccountUsageOut, account_id: str, until: float) -> None:
    at = datetime.fromtimestamp(until, tz=UTC)
    out.status = "rate_limited"
    out.detail = out.detail or (
        "Anthropic refused to report usage — usually because this account has hit a limit."
    )
    out.retry_at = at
    _fill_from_last(out, account_id)


async def _read_account(
    principal: Principal,
    out: AccountUsageOut,
    account_id: str,
    material: dict[str, Any] | None,
    sessions: list[tuple[dict[str, Any], dict[str, Any]]],
    now: float,
) -> AccountUsageOut:
    hit = plan_usage.cached(account_id, now)
    if hit is not None:
        out.windows = _windows_out(hit.windows)
        out.fetched_at = datetime.fromtimestamp(hit.fetched_at, tz=UTC)
        return out

    # Refused recently: asking again before the time it gave only keeps the
    # account pinned at its limit.
    waiting = plan_usage.backing_off(account_id, now)
    if waiting is not None:
        out.detail = waiting[1]
        _slow_down(out, account_id, waiting[0])
        return out

    # Every token there is for this account, best first. A refusal of one
    # (expired, or a kind not allowed to read usage) moves on to the next.
    candidates: list[tuple[str, plan_usage.AccountToken]] = []
    stored = plan_usage.token_from_credentials_file((material or {}).get("oauth_blob"))
    if stored is not None and stored.valid(now):
        candidates.append(("stored", stored))
    if not candidates:
        candidates += [("session", t) for t in await _borrow_tokens(principal, sessions, now)]
    long_lived = (material or {}).get("oauth_token")
    if long_lived:
        candidates.append(("setup_token", plan_usage.AccountToken(str(long_lived), None)))

    if not candidates:
        out.status = "expired"
        out.detail = (
            "The sign-in Moonphase stored has expired, and none of your running "
            "sessions on this account has a current one. Use a session on it, or "
            "sign it in again."
        )
        _fill_from_last(out, account_id)
        return out

    last: plan_usage.UsageUnavailable | None = None
    for source, token in candidates:
        try:
            windows = await plan_usage.fetch(token.token)
        except plan_usage.UsageUnavailable as exc:
            last = exc
            if exc.reason in ("expired", "unsupported"):
                continue
            break
        reading = plan_usage.remember(account_id, windows)
        out.windows = _windows_out(reading.windows)
        out.fetched_at = datetime.fromtimestamp(reading.fetched_at, tz=UTC)
        out.source = source
        return out

    assert last is not None
    if last.reason == "rate_limited":
        out.detail = last.detail
        _slow_down(
            out,
            account_id,
            plan_usage.refused(account_id, now, last.retry_after, last.detail),
        )
        return out
    out.status = last.reason
    out.detail = last.detail
    if last.reason == "unsupported" and candidates[-1][0] == "setup_token":
        out.detail = (
            "This account was signed in with a long-lived token (claude setup-token), "
            "which Anthropic does not let read plan usage, and none of your running "
            "sessions on it has a current interactive sign-in." + _quote(last.detail)
        )
    _fill_from_last(out, account_id)
    return out


def _quote(detail: str) -> str:
    marker = " Anthropic said:"
    return detail[detail.index(marker):] if marker in detail else ""


@router.get("/accounts", response_model=list[AccountUsageOut])
async def read_account_usage(
    principal: Principal = Depends(current_principal),
) -> list[AccountUsageOut]:
    """Every one of your Claude accounts' limits, as `/usage` shows them."""
    now = time.time()
    async with user_session(principal.claims) as conn:
        org_id = await queries.personal_org_id(conn)
        if org_id is None:
            return []
        session_rows = await queries.list_all_sessions(conn)
        project_rows = await queries.list_projects(conn)

    async with service_session() as conn:
        accounts = await queries.list_harness_accounts_privileged(conn, org_id, "claude_code")
        materials: dict[Any, dict[str, Any] | None] = {}
        for account in accounts:
            row = await queries.resolve_harness_credential_privileged(
                conn,
                org_id=org_id,
                project_id=org_id,
                harness="claude_code",
                account_id=account["id"],
            )
            materials[account["id"]] = row if row and row["id"] == account["id"] else None

    # Which of your running sessions runs on which account: the session's own
    # choice, else its project's (when the project is yours), else the default.
    default_id = next((a["id"] for a in accounts if a.get("is_default")), None)
    projects = {p["id"]: p for p in project_rows}
    by_account: dict[Any, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for session in session_rows:
        if not session.get("is_mine") or session.get("state") != "running":
            continue
        project = projects.get(session["project_id"])
        if project is None:
            continue
        own_project = project.get("org_id") == org_id
        account = (
            session.get("harness_credential_id")
            or (project.get("harness_credential_id") if own_project else None)
            or default_id
        )
        by_account.setdefault(account, []).append((session, project))

    return list(
        await asyncio.gather(
            *(
                _account_usage(
                    principal, a, materials.get(a["id"]), by_account.get(a["id"], []), now
                )
                for a in accounts
            )
        )
    )
