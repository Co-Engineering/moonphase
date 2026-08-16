"""Database engine and the two flavours of session the API uses.

`user_session` runs as the Postgres `authenticated` role with the caller's JWT
claims installed, so every policy in the init migration applies. `service_session`
bypasses RLS and exists for exactly two jobs: reading the `private` schema to
hand a credential to an SSH connection, and background reconciliation where
there is no caller.

Preferring the former by default means an authorization bug in a route handler
degrades to "empty result set" rather than "someone else's servers".
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .config import get_settings

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=10,
            max_overflow=10,
            pool_pre_ping=True,
            # Statement caching interacts badly with pgbouncer-style poolers and
            # with SET ROLE churn; the cost here is negligible.
            connect_args={"statement_cache_size": 0},
        )
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def service_session() -> AsyncGenerator[AsyncConnection, None]:
    """Privileged connection. RLS does not apply — use sparingly and on purpose."""
    async with get_engine().begin() as conn:
        yield conn


@asynccontextmanager
async def user_session(claims: dict[str, Any]) -> AsyncGenerator[AsyncConnection, None]:
    """Connection scoped to a caller, with RLS enforced by Postgres.

    `SET LOCAL` is transaction-scoped, so both the role and the claims are
    discarded when the transaction ends and cannot leak to the next borrower of
    this pooled connection.
    """
    async with get_engine().begin() as conn:
        await conn.execute(
            text("select set_config('request.jwt.claims', :claims, true)"),
            {"claims": json.dumps(claims)},
        )
        await conn.execute(
            text("select set_config('request.jwt.claim.sub', :sub, true)"),
            {"sub": str(claims.get("sub", ""))},
        )
        await conn.execute(text("set local role authenticated"))
        yield conn
