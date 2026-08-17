"""What you have spent, framed by how you pay.

The same rows answer two different questions, and putting the wrong one first
makes the screen useless. On a subscription the limit is a window that opened
when you started working, so what matters is how much of it has gone and when
it comes back. On an API key the limit is money, so what matters is the bill.
Which is which is decided by the credential the person actually connected, not
by a preference they have to set.

The one number Moonphase cannot derive is the size of a subscription's
allowance: it is not published, and reading it from the provider would mean
using the session's own OAuth token to call an endpoint on the person's behalf.
So the allowance is something you tell it once, and the percentage appears
after you do. Everything else — when the window opened, when it resets, what
went into it — comes from data already collected.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import queries, usage
from ..auth import Principal, current_principal
from ..db import service_session, user_session
from ..schemas import (
    ModelPriceIn,
    ModelPriceOut,
    UsageLimitsIn,
    UsageLimitsOut,
    UsageOut,
    UsageProjectOut,
    UsageSliceOut,
    UsageWindowOut,
)

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
