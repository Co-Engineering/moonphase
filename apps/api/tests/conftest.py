"""Shared test configuration.

Sets a fixed, obviously-fake encryption key before any moonphase module is
imported, so tests exercise the real crypto path without depending on a
developer's .env.
"""

from __future__ import annotations

import base64
import os

# Exactly 32 bytes, so it is a structurally valid Fernet key.
_TEST_KEY_MATERIAL = b"moonphase-test-key-32-bytes-xxxx"
assert len(_TEST_KEY_MATERIAL) == 32

os.environ.setdefault(
    "MOONPHASE_SECRET_KEY", base64.urlsafe_b64encode(_TEST_KEY_MATERIAL).decode()
)
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres"
)
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret-at-least-32-characters-long")

import pytest  # noqa: E402

from moonphase.db import dispose_engine  # noqa: E402


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test():
    """Dispose the shared engine after every test.

    The engine is a module-level singleton, which is right in production (one
    process, one event loop) but wrong under pytest-asyncio, where each test
    gets a fresh loop. A pooled connection created on a previous test's loop
    fails when reused on the current one — and because it fails inside a
    broad `except`, it surfaces as a confusing "database unreachable" skip
    rather than an error. Disposing between tests keeps every connection on
    the loop that created it.
    """
    yield
    await dispose_engine()
