"""The investigation worker: runs queued investigations, one at a time."""

import asyncio
import contextlib
from datetime import timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.crypto import SecretBox
from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.integrations.github.client import GitHubClient
from incident_intel.integrations.github.evidence import GitHubCodeChanges
from incident_intel.investigation.code_changes import CodeChangeSource
from incident_intel.investigation.llm import WorkerSettings, build_provider
from incident_intel.investigation.orchestrator import run_investigation
from incident_intel.investigation.service import claim_next

logger = structlog.get_logger(__name__)


def build_code_changes(
    settings: WorkerSettings, session_factory: async_sessionmaker[AsyncSession]
) -> CodeChangeSource | None:
    """GitHub as the source of code changes, or None when stored tokens cannot be read
    because no encryption key is configured."""
    if settings.secrets_encryption_key is None:
        logger.warning("code_changes_disabled", reason="SECRETS_ENCRYPTION_KEY is not set")
        return None
    return GitHubCodeChanges(
        session_factory,
        SecretBox(settings.secrets_encryption_key.get_secret_value()),
        GitHubClient(
            base_url=settings.github_api_url, timeout_seconds=settings.github_timeout_seconds
        ),
    )


async def run_worker(settings: WorkerSettings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    provider = build_provider(settings)
    code_changes = build_code_changes(settings, session_factory)
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
                code_changes=code_changes,
            )
    finally:
        await provider.close()
        if code_changes is not None:
            await code_changes.close()
        await engine.dispose()
        logger.info("investigation_worker_stopped")
