"""FastAPI application factory and ASGI entrypoint."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.db.session import engine
from app.services.lead_search_sweeper import run_sweeper

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown hooks."""
    configure_logging()
    log.info("app.startup", environment=settings.ENVIRONMENT)

    # The watchdog that closes lead-search runs the workflow never finished.
    # One task per process; with several workers each sweeps, which is safe
    # because every close is a conditional UPDATE that only one can win.
    stop = asyncio.Event()
    sweeper: asyncio.Task[None] | None = None
    if settings.LEAD_SEARCH_SWEEP_INTERVAL_SECONDS > 0:
        sweeper = asyncio.create_task(run_sweeper(stop), name="lead-search-sweeper")

    yield

    if sweeper is not None:
        stop.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(sweeper, timeout=5)

    await engine.dispose()
    log.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        debug=settings.DEBUG,
        lifespan=lifespan,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    if settings.BACKEND_CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(o).rstrip("/") for o in settings.BACKEND_CORS_ORIGINS],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            # Browsers hide every response header from cross-origin JS unless
            # it is named here. The file exports read the download's filename
            # off Content-Disposition, so without this they silently fall back
            # to a generic name.
            expose_headers=["Content-Disposition"],
        )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
