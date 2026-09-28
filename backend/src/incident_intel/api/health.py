"""Liveness and readiness probes.

- ``/healthz``: the process is up. Never touches dependencies, so a database outage does
  not cause an orchestrator to restart healthy API processes.
- ``/readyz``: the process can serve traffic: the database is reachable and its schema is
  at the migration head this code expects.
"""

import asyncio
from typing import Literal

import structlog
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

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


@router.get("/healthz", response_model=LivenessResponse)
async def healthz() -> LivenessResponse:
    return LivenessResponse(status="ok")


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readyz(request: Request, response: Response) -> ReadinessResponse:
    checks = await check_database(
        request.app.state.engine,
        expected_head=request.app.state.expected_migration_head,
        timeout_seconds=request.app.state.settings.readiness_timeout_seconds,
    )
    ready = all(status == "ok" for status in checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
