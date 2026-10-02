"""Liveness and readiness probes.

- ``/healthz``: the process is up. Never touches dependencies, so a database outage does
  not cause an orchestrator to restart healthy API processes.
- ``/readyz``: the process can serve traffic: the database is reachable and its schema is
  at the migration head this code expects, and Kafka is reachable with the topics the API
  publishes to. Checks run concurrently, each bounded by a timeout.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from incident_intel.streaming.producer import MessagePublisher, PublishError

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])

CheckStatus = Literal["ok", "unavailable", "pending", "unknown"]


class LivenessResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckStatus]


async def check_database(
    engine: AsyncEngine, *, expected_head: str, timeout_seconds: float
) -> dict[str, CheckStatus]:
    try:
        async with asyncio.timeout(timeout_seconds), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            try:
                current = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            except ProgrammingError:  # alembic_version table does not exist yet
                current = None
    except (TimeoutError, OSError, SQLAlchemyError) as exc:
        logger.warning("readiness_database_unavailable", error_type=type(exc).__name__)
        return {"database": "unavailable", "migrations": "unknown"}

    if current != expected_head:
        logger.warning("readiness_migrations_pending", current=current, expected=expected_head)
        return {"database": "ok", "migrations": "pending"}
    return {"database": "ok", "migrations": "ok"}


async def check_redis(ping: Callable[[], Awaitable[bool]]) -> dict[str, CheckStatus]:
    return {"redis": "ok" if await ping() else "unavailable"}


async def check_kafka(
    publisher: MessagePublisher, *, topics: list[str], timeout_seconds: float
) -> dict[str, CheckStatus]:
    try:
        async with asyncio.timeout(timeout_seconds + 1):
            missing = await publisher.missing_topics(topics, timeout_seconds=timeout_seconds)
    except (TimeoutError, PublishError) as exc:
        logger.warning("readiness_kafka_unavailable", error_type=type(exc).__name__)
        return {"kafka": "unavailable"}
    if missing:
        # Topics are created by `ii kafka init`; the API never auto-creates them.
        logger.warning("readiness_kafka_topics_missing", missing=sorted(missing))
        return {"kafka": "pending"}
    return {"kafka": "ok"}


@router.get("/healthz", response_model=LivenessResponse)
async def healthz() -> LivenessResponse:
    return LivenessResponse(status="ok")


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readyz(request: Request, response: Response) -> ReadinessResponse:
    state = request.app.state
    timeout = state.settings.readiness_timeout_seconds
    database, kafka, redis = await asyncio.gather(
        check_database(
            state.engine, expected_head=state.expected_migration_head, timeout_seconds=timeout
        ),
        check_kafka(
            state.publisher,
            topics=[state.metrics_topic, state.logs_topic, state.deployments_topic],
            timeout_seconds=timeout,
        ),
        check_redis(state.cache.ping),
    )
    required = {**database, **kafka}
    ready = all(status == "ok" for status in required.values())
    if not ready:
        response.status_code = 503
    # Redis is reported but not required: every use of it has a fallback, so an outage
    # must not take the API out of rotation.
    return ReadinessResponse(status="ready" if ready else "not_ready", checks={**required, **redis})
