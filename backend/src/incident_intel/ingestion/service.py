"""Accepting a metric batch: validate, normalize, and hand it durably to Kafka."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from incident_intel.core.errors import ServiceUnavailableError
from incident_intel.ingestion.normalize import IngestLimits, normalize_points
from incident_intel.ingestion.schemas import MetricBatchIn
from incident_intel.streaming.producer import MessagePublisher, PublishError
from incident_intel.telemetry.messages import MetricBatchMessage, points_content_hash
from incident_intel.tenancy.context import TenantContext

logger = structlog.get_logger(__name__)

# Fixed namespace for deriving batch ids. Never change it: existing ids would stop matching.
_BATCH_ID_NAMESPACE = uuid.UUID("5b8f4c1e-7a2d-4e59-9c3b-0f6a1d2e8b47")


def derive_batch_id(project_id: uuid.UUID, idempotency_key: str) -> uuid.UUID:
    """Same project + same Idempotency-Key → same batch id, across retries and processes."""
    return uuid.uuid5(_BATCH_ID_NAMESPACE, f"{project_id}:{idempotency_key}")


@dataclass(frozen=True)
class AcceptedBatch:
    batch_id: uuid.UUID
    accepted_points: int


async def accept_metric_batch(
    *,
    ctx: TenantContext,
    batch: MetricBatchIn,
    idempotency_key: str,
    publisher: MessagePublisher,
    topic: str,
    limits: IngestLimits,
    request_id: str | None = None,
    now: datetime | None = None,
) -> AcceptedBatch:
    now = now or datetime.now(UTC)
    points = normalize_points(batch.points, now=now, limits=limits)
    message = MetricBatchMessage(
        batch_id=derive_batch_id(ctx.project_id, idempotency_key),
        # Tenant identity comes only from the verified API key, never from the request body.
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        api_key_id=ctx.principal.api_key_id,
        idempotency_key=idempotency_key,
        content_sha256=points_content_hash(points),
        received_at=now,
        points=points,
    )
    headers = {"schema_version": str(message.schema_version)}
    if request_id:
        headers["request_id"] = request_id

    try:
        await publisher.publish(
            topic,
            # Keyed by project: one project's batches stay ordered within a partition.
            key=str(ctx.project_id).encode(),
            value=message.model_dump_json().encode(),
            headers=headers,
        )
    except PublishError as exc:
        logger.warning(
            "metric_batch_publish_failed", batch_id=str(message.batch_id), error=str(exc)
        )
        raise ServiceUnavailableError("The telemetry pipeline is temporarily unavailable.") from exc

    logger.info("metric_batch_accepted", batch_id=str(message.batch_id), points=len(points))
    return AcceptedBatch(batch_id=message.batch_id, accepted_points=len(points))
