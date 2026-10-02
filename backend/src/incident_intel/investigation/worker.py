"""The investigation worker: runs queued investigations, one at a time."""

import asyncio
import contextlib
from datetime import timedelta

import structlog

from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.investigation.llm import WorkerSettings, build_provider
from incident_intel.investigation.orchestrator import run_investigation
from incident_intel.investigation.service import claim_next

logger = structlog.get_logger(__name__)


async def run_worker(settings: WorkerSettings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    provider = build_provider(settings)
    logger.info("investigation_worker_started", provider=provider.name, model=provider.model)
    try:
        while not stop.is_set():
            async with session_factory() as session:
                investigation = await claim_next(
                    session,
                    lease=timedelta(seconds=settings.investigation_lease_seconds),
                    max_attempts=settings.investigation_max_attempts,
                )
            if investigation is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
                continue
            await run_investigation(
                session_factory,
                provider,
                investigation.id,
                max_llm_attempts=settings.llm_max_attempts,
            )
    finally:
        await provider.close()
        await engine.dispose()
        logger.info("investigation_worker_stopped")
