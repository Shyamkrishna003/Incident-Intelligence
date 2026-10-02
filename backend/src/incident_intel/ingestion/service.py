"""Accepting a telemetry batch: validate, normalize, and hand it durably to Kafka.

Metrics, logs, and deployments share one path. They differ only in how items are
normalized and which message type and topic they use.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel

from incident_intel.cache.idempotency import ClaimOutcome, IdempotencyStore
from incident_intel.core.errors import IdempotencyKeyReusedError, ServiceUnavailableError
from incident_intel.ingestion.normalize import (
    IngestLimits,
    normalize_deployments,
    normalize_logs,
    normalize_points,
)
from incident_intel.ingestion.schemas import DeploymentBatchIn, LogBatchIn, MetricBatchIn
from incident_intel.streaming.producer import MessagePublisher, PublishError
from incident_intel.telemetry.messages import (
    DeploymentBatchMessage,
    LogBatchMessage,
    MetricBatchMessage,
    content_hash,
)
from incident_intel.tenancy.context import TenantContext

logger = structlog.get_logger(__name__)

BatchKind = Literal["metrics", "logs", "deployments"]

# Fixed namespace for deriving batch ids. Never change it: existing ids would stop matching.
_BATCH_ID_NAMESPACE = uuid.UUID("5b8f4c1e-7a2d-4e59-9c3b-0f6a1d2e8b47")


def _scoped_key(idempotency_key: str, kind: BatchKind) -> str:
    """Keeps one Idempotency-Key from colliding across kinds of telemetry.

    Metrics use the bare key (the original scheme). Other kinds are prefixed with
    ``<kind>/``; a client key can never contain ``/``, so the two cannot overlap.
    """
    return idempotency_key if kind == "metrics" else f"{kind}/{idempotency_key}"


def derive_batch_id(
    project_id: uuid.UUID, idempotency_key: str, kind: BatchKind = "metrics"
) -> uuid.UUID:
    """Same project + kind + Idempotency-Key → same batch id, across retries and processes."""
    return uuid.uuid5(_BATCH_ID_NAMESPACE, f"{project_id}:{_scoped_key(idempotency_key, kind)}")


@dataclass(frozen=True)
class AcceptedBatch:
    batch_id: uuid.UUID
    accepted: int


@dataclass(frozen=True)
class IngestContext:
    """What every ingestion request needs besides its body."""

    ctx: TenantContext
    idempotency_key: str
    publisher: MessagePublisher
    idempotency: IdempotencyStore
    limits: IngestLimits
    request_id: str | None = None
    now: datetime | None = None


async def _accept(
    request: IngestContext,
    *,
    kind: BatchKind,
    topic: str,
    items: Sequence[BaseModel],
    build_message: Callable[[dict[str, Any]], BaseModel],
) -> AcceptedBatch:
    ctx = request.ctx
    batch_id = derive_batch_id(ctx.project_id, request.idempotency_key, kind)
    content_sha256 = content_hash(items)
    claim_key = _scoped_key(request.idempotency_key, kind)

    # Claimed only after validation, so a rejected batch never burns its key.
    claim = await request.idempotency.claim(ctx.project_id, claim_key, content_sha256)
    if claim is ClaimOutcome.CONFLICT:
        logger.info("idempotency_key_reused", batch_id=str(batch_id), kind=kind)
        raise IdempotencyKeyReusedError()
    if claim is ClaimOutcome.PUBLISHED:
        # A retry of a batch Kafka already acknowledged: same answer, no second message.
        logger.info("batch_replayed", batch_id=str(batch_id), kind=kind, items=len(items))
        return AcceptedBatch(batch_id=batch_id, accepted=len(items))
    # NEW, IN_FLIGHT, or UNAVAILABLE: publish. A possible duplicate message is harmless
    # because the storage consumer stores each batch id once.

    message = build_message(
        {
            "batch_id": batch_id,
            # Tenant identity comes only from the verified API key, never from the body.
            "organization_id": ctx.organization_id,
            "project_id": ctx.project_id,
            "api_key_id": ctx.principal.api_key_id,
            "idempotency_key": request.idempotency_key,
            "content_sha256": content_sha256,
            "received_at": request.now or datetime.now(UTC),
        }
    )
    headers = {"schema_version": "1"}
    if request.request_id:
        headers["request_id"] = request.request_id

    try:
        await request.publisher.publish(
            topic,
            # Keyed by project: one project's batches stay ordered within a partition.
            key=str(ctx.project_id).encode(),
            value=message.model_dump_json().encode(),
            headers=headers,
        )
    except PublishError as exc:
        logger.warning("batch_publish_failed", batch_id=str(batch_id), kind=kind, error=str(exc))
        raise ServiceUnavailableError("The telemetry pipeline is temporarily unavailable.") from exc

    if claim is not ClaimOutcome.UNAVAILABLE:
        await request.idempotency.mark_published(ctx.project_id, claim_key, content_sha256)
    logger.info("batch_accepted", batch_id=str(batch_id), kind=kind, items=len(items))
    return AcceptedBatch(batch_id=batch_id, accepted=len(items))


def _now(request: IngestContext) -> datetime:
    return request.now or datetime.now(UTC)


async def accept_metric_batch(
    request: IngestContext, batch: MetricBatchIn, *, topic: str
) -> AcceptedBatch:
    points = normalize_points(batch.points, now=_now(request), limits=request.limits)
    return await _accept(
        request,
        kind="metrics",
        topic=topic,
        items=points,
        build_message=lambda envelope: MetricBatchMessage(**envelope, points=points),
    )


async def accept_log_batch(
    request: IngestContext, batch: LogBatchIn, *, topic: str
) -> AcceptedBatch:
    records = normalize_logs(batch.records, now=_now(request), limits=request.limits)
    return await _accept(
        request,
        kind="logs",
        topic=topic,
        items=records,
        build_message=lambda envelope: LogBatchMessage(**envelope, records=records),
    )


async def accept_deployment_batch(
    request: IngestContext, batch: DeploymentBatchIn, *, topic: str
) -> AcceptedBatch:
    deployments = normalize_deployments(batch.deployments, now=_now(request), limits=request.limits)
    return await _accept(
        request,
        kind="deployments",
        topic=topic,
        items=deployments,
        build_message=lambda envelope: DeploymentBatchMessage(**envelope, deployments=deployments),
    )
