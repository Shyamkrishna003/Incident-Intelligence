"""The detection consumer: "metrics stored" events → anomalies."""

import asyncio

import structlog
from pydantic import ValidationError
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.config import RuntimeSettings
from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.detection.models import Anomaly
from incident_intel.detection.service import DetectionSettings, detect_series
from incident_intel.incidents.service import CorrelationSettings, apply_detection_changes
from incident_intel.streaming.consumer import (
    ConsumedMessage,
    KafkaMessageSource,
    MessageHandler,
    PermanentMessageError,
    run_consumer,
)
from incident_intel.streaming.producer import KafkaPublisher
from incident_intel.streaming.topics import METRICS_STORED, METRICS_STORED_DLQ, topic_name
from incident_intel.telemetry.messages import MetricsStoredEvent

logger = structlog.get_logger(__name__)

DETECTION_CONSUMER_GROUP = "detection"


def detection_handler(
    session_factory: async_sessionmaker[AsyncSession],
    settings: DetectionSettings,
    correlation: CorrelationSettings | None = None,
) -> MessageHandler:
    """Detect anomalies for the event's series, then group the changes into incidents.

    Both happen in one transaction, so an anomaly and its incident commit together.
    """
    correlation = correlation or CorrelationSettings()

    async def handle(message: ConsumedMessage) -> None:
        try:
            event = MetricsStoredEvent.model_validate_json(message.value)
        except ValidationError as exc:
            raise PermanentMessageError("invalid_message", str(exc.error_count())) from exc

        evaluated = 0
        opened: list[Anomaly] = []
        extended: list[Anomaly] = []
        closed: list[Anomaly] = []
        async with session_factory() as session:
            try:
                # Sorted so concurrent consumers touch series in the same order.
                for series_id in sorted(event.series_ids, key=str):
                    outcome = await detect_series(
                        session, project_id=event.project_id, series_id=series_id, settings=settings
                    )
                    evaluated += outcome.evaluated_points
                    opened += outcome.opened
                    extended += outcome.extended
                    closed += outcome.closed
                await apply_detection_changes(
                    session, opened=opened, extended=extended, closed=closed, config=correlation
                )
                # One commit per event: anomalies, incidents and positions land together.
                await session.commit()
            except (IntegrityError, DataError) as exc:
                raise PermanentMessageError("rejected_by_database", str(exc.orig)[:300]) from exc

        logger.info(
            "detection_completed",
            position=message.position,
            project_id=str(event.project_id),
            series=len(event.series_ids),
            evaluated_points=evaluated,
            anomalies_opened=len(opened),
            anomalies_closed=len(closed),
        )

    return handle


async def run_detection_consumer(settings: RuntimeSettings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    publisher = KafkaPublisher(settings, delivery_timeout_seconds=10.0)
    source = KafkaMessageSource(
        settings, group_id=DETECTION_CONSUMER_GROUP, topics=[topic_name(settings, METRICS_STORED)]
    )
    detection = DetectionSettings.from_settings(settings)
    logger.info(
        "detection_consumer_started",
        group=DETECTION_CONSUMER_GROUP,
        detector=detection.detector.name,
        threshold=detection.detector.threshold,
    )
    try:
        await run_consumer(
            source=source,
            handler=detection_handler(
                create_session_factory(engine),
                detection,
                CorrelationSettings.from_settings(settings),
            ),
            publisher=publisher,
            dlq_topic=topic_name(settings, METRICS_STORED_DLQ),
            stop=stop,
        )
    finally:
        await source.close()
        await publisher.close()
        await engine.dispose()
        logger.info("detection_consumer_stopped")
