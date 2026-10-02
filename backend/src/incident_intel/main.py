"""ASGI application factory.

Run with: ``uvicorn incident_intel.main:create_app --factory``
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from incident_intel import __version__
from incident_intel.api import health
from incident_intel.auth.tokens import TokenVerifier, build_token_verifier
from incident_intel.cache.services import CacheServices, build_redis_cache
from incident_intel.core.config import Settings, get_settings
from incident_intel.core.errors import install_exception_handlers
from incident_intel.core.logging import configure_logging
from incident_intel.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from incident_intel.db.migrations import head_revision
from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.detection.router import project_router as detection_project_router
from incident_intel.detection.router import router as detection_router
from incident_intel.incidents.router import project_router as incidents_project_router
from incident_intel.incidents.router import router as incidents_router
from incident_intel.ingestion.router import router as ingestion_router
from incident_intel.investigation.router import router as investigation_router
from incident_intel.learning.router import router as learning_router
from incident_intel.streaming.producer import KafkaPublisher, MessagePublisher
from incident_intel.streaming.topics import DEPLOYMENTS, LOGS, METRICS, topic_name
from incident_intel.telemetry.event_router import project_router as event_project_router
from incident_intel.telemetry.event_router import router as event_router
from incident_intel.telemetry.router import project_router as telemetry_project_router
from incident_intel.telemetry.router import router as telemetry_router
from incident_intel.tenancy.console_router import router as console_router
from incident_intel.tenancy.router import router as tenancy_router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await app.state.publisher.close()
    await app.state.cache.close()
    await app.state.token_verifier.close()
    await app.state.engine.dispose()


def create_app(
    settings: Settings | None = None,
    *,
    publisher: MessagePublisher | None = None,
    cache: CacheServices | None = None,
    token_verifier: TokenVerifier | None = None,
) -> FastAPI:
    """Build the API. ``publisher``, ``cache`` and ``token_verifier`` can be injected
    (tests); by default Kafka, Redis and Firebase are used."""
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

    # None of these needs its backend to be up at construction time.
    engine = create_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.expected_migration_head = head_revision()
    app.state.publisher = publisher or KafkaPublisher(
        settings, delivery_timeout_seconds=settings.kafka_produce_timeout_seconds
    )
    app.state.metrics_topic = topic_name(settings, METRICS)
    app.state.logs_topic = topic_name(settings, LOGS)
    app.state.deployments_topic = topic_name(settings, DEPLOYMENTS)
    app.state.cache = cache or build_redis_cache(settings)
    app.state.token_verifier = token_verifier or build_token_verifier(settings)

    install_exception_handlers(app)
    # Starlette runs the last-added middleware first: request context wraps everything,
    # so even a 413 from the body limit carries a request id and gets logged.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(tenancy_router)
    app.include_router(ingestion_router)
    app.include_router(telemetry_router)
    app.include_router(console_router)
    app.include_router(telemetry_project_router)
    app.include_router(event_router)
    app.include_router(event_project_router)
    app.include_router(detection_router)
    app.include_router(detection_project_router)
    app.include_router(incidents_router)
    app.include_router(incidents_project_router)
    app.include_router(investigation_router)
    app.include_router(learning_router)
    return app
