"""ASGI application factory.

Run with: ``uvicorn incident_intel.main:create_app --factory``
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from incident_intel import __version__
from incident_intel.api import health
from incident_intel.core.config import Settings, get_settings
from incident_intel.core.errors import install_exception_handlers
from incident_intel.core.logging import configure_logging
from incident_intel.core.middleware import RequestContextMiddleware
from incident_intel.db.migrations import head_revision
from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.tenancy.router import router as tenancy_router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)

    expose_docs = settings.environment != "production"
    app = FastAPI(
        title="Incident Intelligence API",
        version=__version__,
        lifespan=_lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if expose_docs else None,
    )

    # The engine connects lazily, so creating it here does not require a live database.
    engine = create_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.expected_migration_head = head_revision()

    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(tenancy_router)
    return app
