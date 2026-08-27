"""Moonphase API entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import preflight, preview, ssh
from .config import get_settings
from .db import dispose_engine
from .monitor import monitor
from .routers import (
    claude_config,
    feed,
    feedsocket,
    mcp_oauth,
    meta,
    notifications,
    people,
    previewsocket,
    projects,
    review,
    servers,
    setup,
    shares,
    terminal,
    usage,
)
from .routers import preview as preview_router
from .routers import profile as profile_router
from .runtime import Forbidden, NotFound
from .ssh import HostKeyMismatch, SSHError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("moonphase")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    log.info("moonphase api starting (runtime image: %s)", settings.moonphase_runtime_image)
    # Every misconfiguration this project has documented is knowable now and
    # otherwise surfaces much later wearing a disguise. Fatal findings stop the
    # process, because a container that exits with a reason in its logs is far
    # easier to diagnose than one that serves 500s.
    await preflight.run()
    # Notifications only mean anything if something is watching while no
    # client is open, which is exactly when they matter.
    monitor.start()
    yield
    await monitor.stop()
    log.info("shutting down: closing preview tunnels and SSH connections")
    await preview.registry.close_all()
    await ssh.pool.close_all()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Moonphase",
        version="0.1.0",
        description="Self-hosted control plane for remote AI coding sessions.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        del request
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden) -> JSONResponse:
        del request
        # 403 rather than 404: the caller can already see this resource, so
        # hiding it now would only be confusing.
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(HostKeyMismatch)
    async def _host_key(request: Request, exc: HostKeyMismatch) -> JSONResponse:
        del request
        # 409 rather than 502: nothing is broken, the operator must decide.
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SSHError)
    async def _ssh_error(request: Request, exc: SSHError) -> JSONResponse:
        del request
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    app.include_router(meta.router)
    app.include_router(servers.router)
    app.include_router(setup.router)
    app.include_router(projects.router)
    app.include_router(claude_config.router)
    app.include_router(mcp_oauth.router)
    app.include_router(mcp_oauth.profile_router)
    app.include_router(profile_router.router)
    app.include_router(preview_router.router)
    app.include_router(previewsocket.router)
    app.include_router(people.router)
    app.include_router(notifications.router)
    app.include_router(feed.router)
    app.include_router(feedsocket.router)
    app.include_router(shares.router)
    app.include_router(usage.router)
    app.include_router(review.router)
    app.include_router(terminal.router)

    _serve_web_app(app)

    return app


def _serve_web_app(app: FastAPI) -> None:
    """Serve the built frontend from the API, when there is one.

    One address for the whole thing is what makes installing it on a phone
    work: you point the browser at your host, get the app, and it talks to the
    API it came from — same origin, so no CORS to configure and no second URL
    to remember. It also puts the service worker at the root scope, which is
    what push requires.

    Absent in development, where Vite serves the app with hot reload. Its
    absence is not an error: the API is perfectly usable on its own.
    """
    # moonphase/main.py -> apps/api/moonphase -> apps/api -> apps
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not (dist / "index.html").is_file():
        log.info("no built frontend at %s; serving the API only", dist)
        return

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def _manifest() -> FileResponse:
        return FileResponse(dist / "manifest.webmanifest")

    @app.get("/sw.js", include_in_schema=False)
    async def _service_worker() -> FileResponse:
        # Never cached: a stale worker is one that cannot be updated, and this
        # one is the only thing awake when a notification arrives.
        return FileResponse(
            dist / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def _spa(path: str) -> FileResponse:
        """Static file if there is one, otherwise the app shell.

        The client routes on the query string rather than the path, but a
        home-screen launch can arrive at any URL the manifest was installed
        from, and returning the shell is what makes that land somewhere.
        """
        candidate = (dist / path).resolve()
        if path and dist in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    log.info("serving the frontend from %s", dist)


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "moonphase.main:app",
        host=settings.moonphase_api_host,
        port=settings.moonphase_api_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
