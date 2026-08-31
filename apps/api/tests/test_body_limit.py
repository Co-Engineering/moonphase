"""A request body over the limit must never reach a handler.

The feed upload endpoint's own 15MB check runs only after Starlette's
multipart parser has already spooled a file part to disk in full -- this
tests the ASGI-level guard main.py adds (Starlette's own
RequestBodyLimitMiddleware) that refuses an oversized body before any
parsing happens, using the exact class and wiring main.py uses rather than
the real app -- which needs a running Postgres/SSH stack this sandbox
doesn't have -- so the middleware mechanism itself is what's under test,
independent of any one endpoint's auth or DB dependencies.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

MAX_BODY_BYTES = 20 * 1024 * 1024


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES)

    @app.post("/echo-length")
    async def echo_length(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"received": len(body)}

    return app


client = TestClient(_make_app())


def test_a_body_over_the_limit_is_refused_before_the_handler_reads_it() -> None:
    oversized = b"x" * (MAX_BODY_BYTES + 1)
    response = client.post("/echo-length", content=oversized)

    assert response.status_code == 413


def test_a_body_at_or_under_the_limit_reaches_the_handler() -> None:
    response = client.post("/echo-length", content=b"x" * 1024)

    assert response.status_code == 200
    assert response.json() == {"received": 1024}
