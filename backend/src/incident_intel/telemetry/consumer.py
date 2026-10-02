"""The storage consumer: Kafka telemetry topics → PostgreSQL."""

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from pydantic import ValidationError
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.config import RuntimeSettings
from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.streaming.consumer import (
    ConsumedMessage,
    KafkaMessageSource,
    MessageHandler,
    PermanentMessageError,
    run_consumer,
)
from incident_intel.streaming.producer import KafkaPublisher
from incident_intel.streaming.topics import (
    DEPLOYMENTS,
    LOGS,
    METRICS,
    TELEMETRY_TOPICS,
    TopicSpec,
    topic_name,
)
from incident_intel.telemetry.messages import (
    BatchEnvelope,
    DeploymentBatchMessage,
    LogBatchMessage,
    MetricBatchMessage,
)
from incident_intel.telemetry.storage import (
    IdempotencyConflictError,
    StoreResult,
    store_deployment_batch,
    store_log_batch,
    store_metric_batch,
)

logger = structlog.get_logger(__name__)

STORAGE_CONSUMER_GROUP = "telemetry-storage"
_MAX_DETAIL = 300


def _summarize_validation(exc: ValidationError) -> str:
    # Locations and error types only: never echo message content into logs or the DLQ.
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['type']}" for err in exc.errors()[:5]
    )


class BatchHandler[BatchT: BatchEnvelope]:
    """Stores one kind of batch, mapping failures onto the consumer's delivery contract:
    invalid or unacceptable messages are permanent (dead-lettered); anything else, such as
    the database being down, is left to be retried."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        kind: str,
        message_type: type[BatchT],
        store: Callable[[AsyncSession, BatchT], Awaitable[StoreResult]],
    ) -> None:
        self._session_factory = session_factory
        self._kind = kind
        self._message_type = message_type
        self._store = store

    async def __call__(self, message: ConsumedMessage) -> None:
        try:
            batch = self._message_type.model_validate_json(message.value)
        except ValidationError as exc:
            raise PermanentMessageError("invalid_message", _summarize_validation(exc)) from exc

        async with self._session_factory() as session:
            try:
                result = await self._store(session, batch)
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
            "batch_processed",
            kind=self._kind,
            position=message.position,
            batch_id=str(batch.batch_id),
            project_id=str(batch.project_id),
            outcome=result.outcome.value,
            stored=result.stored_points,
        )


def metric_handler(session_factory: async_sessionmaker[AsyncSession]) -> MessageHandler:
    return BatchHandler(
        session_factory, kind="metrics", message_type=MetricBatchMessage, store=store_metric_batch
    )


def log_handler(session_factory: async_sessionmaker[AsyncSession]) -> MessageHandler:
    return BatchHandler(
        session_factory, kind="logs", message_type=LogBatchMessage, store=store_log_batch
    )


def deployment_handler(session_factory: async_sessionmaker[AsyncSession]) -> MessageHandler:
    return BatchHandler(
        session_factory,
        kind="deployments",
        message_type=DeploymentBatchMessage,
        store=store_deployment_batch,
    )


_HANDLER_FACTORIES: dict[
    TopicSpec, Callable[[async_sessionmaker[AsyncSession]], MessageHandler]
] = {
    METRICS: metric_handler,
    LOGS: log_handler,
    DEPLOYMENTS: deployment_handler,
}


def storage_handler(
    settings: RuntimeSettings, session_factory: async_sessionmaker[AsyncSession]
) -> MessageHandler:
    """One handler for every telemetry topic: each message goes to its topic's handler."""
    by_topic = {
        topic_name(settings, spec): factory(session_factory)
        for spec, factory in _HANDLER_FACTORIES.items()
    }

    async def handle(message: ConsumedMessage) -> None:
        handler = by_topic.get(message.topic)
        if handler is None:
            raise PermanentMessageError("unexpected_topic", message.topic)
        await handler(message)

    return handle


def dead_letter_topics(settings: RuntimeSettings) -> dict[str, str]:
    return {topic_name(settings, spec): topic_name(settings, dlq) for spec, dlq in TELEMETRY_TOPICS}


async def run_storage_consumer(settings: RuntimeSettings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    publisher = KafkaPublisher(settings, delivery_timeout_seconds=10.0)
    dead_letters = dead_letter_topics(settings)
    source = KafkaMessageSource(
        settings, group_id=STORAGE_CONSUMER_GROUP, topics=sorted(dead_letters)
    )
    logger.info(
        "storage_consumer_started", group=STORAGE_CONSUMER_GROUP, topics=sorted(dead_letters)
    )
    try:
        await run_consumer(
            source=source,
            handler=storage_handler(settings, create_session_factory(engine)),
            publisher=publisher,
            dlq_topic=dead_letters,
            stop=stop,
        )
    finally:
        await source.close()
        await publisher.close()
        await engine.dispose()
        logger.info("storage_consumer_stopped")
