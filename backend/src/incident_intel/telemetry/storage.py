"""Idempotent persistence of metric batches (used by the storage consumer)."""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.telemetry.messages import (
    SEVERITY_NUMBERS,
    BatchEnvelope,
    DeploymentBatchMessage,
    LogBatchMessage,
    MetricBatchMessage,
    attributes_hash,
)
from incident_intel.telemetry.models import (
    Deployment,
    IngestBatch,
    LogRecord,
    MetricPoint,
    MetricSeries,
    Service,
)


class StoreOutcome(StrEnum):
    STORED = "stored"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class StoreResult:
    outcome: StoreOutcome
    stored_points: int


class IdempotencyConflictError(Exception):
    """An idempotency key was reused for a batch with different content."""


SeriesKey = tuple[uuid.UUID, str, str]  # (service_id, metric name, attributes hash)


async def _claim_batch(
    session: AsyncSession, batch: BatchEnvelope, *, kind: str, count: int
) -> bool:
    """Record the batch, or report that it was already stored.

    Returns True when this call claimed it (the caller must store the items and commit).
    Returns False for a replay of the same batch; the transaction is then already ended.
    Raises IdempotencyConflictError when the id exists with different content.
    """
    claimed = await session.scalar(
        pg_insert(IngestBatch)
        .values(
            id=batch.batch_id,
            organization_id=batch.organization_id,
            project_id=batch.project_id,
            api_key_id=batch.api_key_id,
            kind=kind,
            idempotency_key=batch.idempotency_key,
            content_sha256=batch.content_sha256,
            point_count=count,
            stored_point_count=0,
            received_at=batch.received_at,
        )
        .on_conflict_do_nothing(index_elements=[IngestBatch.id])
        .returning(IngestBatch.id)
    )
    if claimed is not None:
        return True
    existing_hash = await session.scalar(
        select(IngestBatch.content_sha256).where(IngestBatch.id == batch.batch_id)
    )
    # Nothing was written; commit just ends the transaction. (A rollback would also
    # expire every ORM object the caller holds in this session.)
    await session.commit()
    if existing_hash != batch.content_sha256:
        raise IdempotencyConflictError(str(batch.batch_id))
    return False


async def _finish_batch(session: AsyncSession, batch: BatchEnvelope, stored: int) -> StoreResult:
    await session.execute(
        update(IngestBatch)
        .where(IngestBatch.id == batch.batch_id)
        .values(stored_point_count=stored)
    )
    await session.commit()
    return StoreResult(StoreOutcome.STORED, stored)


_DUPLICATE = StoreResult(StoreOutcome.DUPLICATE, 0)


async def store_metric_batch(session: AsyncSession, batch: MetricBatchMessage) -> StoreResult:
    """Store a batch in one transaction, which this function always ends.

    Replaying the same batch is a no-op. The batch row is claimed first: if its id already
    exists, another delivery of this batch was stored and nothing else is written.
    """
    if not await _claim_batch(session, batch, kind="metrics", count=len(batch.points)):
        return _DUPLICATE
    service_ids = await _upsert_services(session, batch, {point.service for point in batch.points})
    series_ids = await _upsert_series(session, batch, service_ids)
    stored = await _insert_points(session, batch, service_ids, series_ids)
    return await _finish_batch(session, batch, stored)


async def store_log_batch(session: AsyncSession, batch: LogBatchMessage) -> StoreResult:
    """Store a batch of log records once. Log lines have no natural identity, so
    deduplication is per batch: the claim above is the only guard, and it is enough because
    the records are written in the same transaction as the claim."""
    if not await _claim_batch(session, batch, kind="logs", count=len(batch.records)):
        return _DUPLICATE
    service_ids = await _upsert_services(session, batch, {r.service for r in batch.records})
    await session.execute(
        insert(LogRecord),
        [
            {
                "project_id": batch.project_id,
                "service_id": service_ids[record.service],
                "ts": record.timestamp,
                "severity": SEVERITY_NUMBERS[record.severity],
                "message": record.message,
                "attributes": dict(record.attributes),
                "trace_id": record.trace_id,
                "batch_id": batch.batch_id,
            }
            for record in batch.records
        ],
    )
    return await _finish_batch(session, batch, len(batch.records))


async def store_deployment_batch(
    session: AsyncSession, batch: DeploymentBatchMessage
) -> StoreResult:
    """Store deployments once. The same deployment reported in two different batches (for
    example by a retried CI job with a new idempotency key) is also stored once."""
    if not await _claim_batch(session, batch, kind="deployments", count=len(batch.deployments)):
        return _DUPLICATE
    service_ids = await _upsert_services(session, batch, {d.service for d in batch.deployments})
    result = await session.execute(
        pg_insert(Deployment)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "organization_id": batch.organization_id,
                    "project_id": batch.project_id,
                    "service_id": service_ids[deployment.service],
                    "version": deployment.version,
                    "deployed_at": deployment.deployed_at,
                    "commit_sha": deployment.commit_sha,
                    "environment": deployment.environment,
                    "deployed_by": deployment.deployed_by,
                    "description": deployment.description,
                }
                for deployment in batch.deployments
            ]
        )
        .on_conflict_do_nothing(
            index_elements=[
                Deployment.project_id,
                Deployment.service_id,
                Deployment.version,
                Deployment.deployed_at,
            ]
        )
        .returning(Deployment.id)
    )
    return await _finish_batch(session, batch, len(result.all()))


async def _upsert_services(
    session: AsyncSession, batch: BatchEnvelope, service_names: set[str]
) -> dict[str, uuid.UUID]:
    # Sorted: concurrent writers insert in the same order, which avoids deadlocks.
    names = sorted(service_names)
    await session.execute(
        pg_insert(Service)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "organization_id": batch.organization_id,
                    "project_id": batch.project_id,
                    "name": name,
                }
                for name in names
            ]
        )
        .on_conflict_do_nothing(index_elements=[Service.project_id, Service.name])
    )
    rows = await session.execute(
        select(Service.name, Service.id).where(
            Service.project_id == batch.project_id, Service.name.in_(names)
        )
    )
    return {name: service_id for name, service_id in rows}


async def _upsert_series(
    session: AsyncSession, batch: MetricBatchMessage, service_ids: dict[str, uuid.UUID]
) -> dict[SeriesKey, uuid.UUID]:
    wanted: dict[SeriesKey, dict[str, object]] = {}
    for point in batch.points:
        key = (service_ids[point.service], point.metric, attributes_hash(point.attributes))
        wanted.setdefault(
            key,
            {
                "id": uuid.uuid4(),
                "organization_id": batch.organization_id,
                "project_id": batch.project_id,
                "service_id": key[0],
                "name": point.metric,
                "unit": point.unit,
                "attributes": dict(point.attributes),
                "attributes_hash": key[2],
            },
        )
    ordered = [wanted[key] for key in sorted(wanted, key=lambda k: (str(k[0]), k[1], k[2]))]
    await session.execute(
        pg_insert(MetricSeries)
        .values(ordered)
        .on_conflict_do_nothing(
            index_elements=[
                MetricSeries.project_id,
                MetricSeries.service_id,
                MetricSeries.name,
                MetricSeries.attributes_hash,
            ]
        )
    )
    rows = await session.execute(
        select(
            MetricSeries.service_id,
            MetricSeries.name,
            MetricSeries.attributes_hash,
            MetricSeries.id,
        ).where(
            MetricSeries.project_id == batch.project_id,
            tuple_(MetricSeries.service_id, MetricSeries.name, MetricSeries.attributes_hash).in_(
                list(wanted)
            ),
        )
    )
    return {(service_id, name, hash_): series_id for service_id, name, hash_, series_id in rows}


async def _insert_points(
    session: AsyncSession,
    batch: MetricBatchMessage,
    service_ids: dict[str, uuid.UUID],
    series_ids: dict[SeriesKey, uuid.UUID],
) -> int:
    values = [
        {
            "series_id": series_ids[
                (service_ids[point.service], point.metric, attributes_hash(point.attributes))
            ],
            "ts": point.timestamp,
            "project_id": batch.project_id,
            "value": point.value,
        }
        for point in batch.points
    ]
    result = await session.execute(
        pg_insert(MetricPoint)
        .values(values)
        .on_conflict_do_nothing(index_elements=[MetricPoint.series_id, MetricPoint.ts])
        .returning(MetricPoint.ts)
    )
    return len(result.all())
