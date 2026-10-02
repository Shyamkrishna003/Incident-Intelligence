"""The storage consumer: Kafka `telemetry.metrics.v1` → PostgreSQL."""

import asyncio

import structlog
from pydantic import ValidationError
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.config import RuntimeSettings
from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.streaming.consumer import (
    ConsumedMessage,
    KafkaMessageSource,
    PermanentMessageError,
    run_consumer,
)
from incident_intel.streaming.producer import KafkaPublisher
from incident_intel.streaming.topics import METRICS, METRICS_DLQ, topic_name
from incident_intel.telemetry.messages import MetricBatchMessage
from incident_intel.telemetry.storage import IdempotencyConflictError, store_metric_batch

logger = structlog.get_logger(__name__)

STORAGE_CONSUMER_GROUP = "telemetry-storage"
_MAX_DETAIL = 300


def _summarize_validation(exc: ValidationError) -> str:
    # Locations and error types only: never echo message content into logs or the DLQ.
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['type']}" for err in exc.errors()[:5]
    )


class MetricBatchHandler:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, message: ConsumedMessage) -> None:
        try:
            batch = MetricBatchMessage.model_validate_json(message.value)
        except ValidationError as exc:
            raise PermanentMessageError("invalid_message", _summarize_validation(exc)) from exc

        async with self._session_factory() as session:
            try:
                result = await store_metric_batch(session, batch)
            except IdempotencyConflictError as exc:
                raise PermanentMessageError(
                    "idempotency_conflict",
                    f"batch {batch.batch_id} was already stored with different content",
                ) from exc
            except (IntegrityError, DataError) as exc:
                # The data itself is unacceptable (e.g. its project no longer exists);
                # retrying cannot help. Connection errors are not caught here: they retry.
                raise PermanentMessageError(
                    "rejected_by_database", str(exc.orig)[:_MAX_DETAIL]
                ) from exc

        logger.info(
            "metric_batch_processed",
            position=message.position,
            batch_id=str(batch.batch_id),
            project_id=str(batch.project_id),
            outcome=result.outcome.value,
            points=len(batch.points),
            stored_points=result.stored_points,
        )


async def run_storage_consumer(settings: RuntimeSettings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    publisher = KafkaPublisher(settings, delivery_timeout_seconds=10.0)
    source = KafkaMessageSource(
        settings, group_id=STORAGE_CONSUMER_GROUP, topics=[topic_name(settings, METRICS)]
    )
    logger.info("storage_consumer_started", group=STORAGE_CONSUMER_GROUP)
    try:
        await run_consumer(
            source=source,
            handler=MetricBatchHandler(create_session_factory(engine)),
            publisher=publisher,
            dlq_topic=topic_name(settings, METRICS_DLQ),
            stop=stop,
        )
    finally:
        await source.close()
        await publisher.close()
        await engine.dispose()
        logger.info("storage_consumer_stopped")
