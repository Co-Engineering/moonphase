"""Moonphase API entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import preview, ssh
from .config import get_settings
from .db import dispose_engine
from .monitor import monitor
from .routers import meta, notifications, projects, servers, terminal
from .routers import preview as preview_router
from .routers import profile as profile_router
from .runtime import NotFound
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
    app.include_router(projects.router)
    app.include_router(profile_router.router)
    app.include_router(preview_router.router)
    app.include_router(notifications.router)
    app.include_router(terminal.router)

    return app


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
