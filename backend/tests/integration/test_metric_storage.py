"""Storage consumer logic against PostgreSQL: idempotency, tenancy, and error classification."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.streaming.consumer import ConsumedMessage, PermanentMessageError
from incident_intel.telemetry.consumer import MetricBatchHandler
from incident_intel.telemetry.messages import MetricBatchMessage, MetricPointMessage
from incident_intel.telemetry.models import IngestBatch, MetricPoint, MetricSeries, Service
from incident_intel.telemetry.storage import (
    IdempotencyConflictError,
    StoreOutcome,
    store_metric_batch,
)
from tests.conftest import Tenant, TenantFactory
from tests.support import metric_batch, metric_point

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _batch(
    tenant: Tenant, points: list[MetricPointMessage], **kwargs: object
) -> MetricBatchMessage:
    return metric_batch(
        organization_id=tenant.organization.id,
        project_id=tenant.project.id,
        api_key_id=tenant.api_key.id,
        points=points,
        received_at=T0,
        **kwargs,  # type: ignore[arg-type]
    )


async def _count(session: AsyncSession, model: type, project_id: uuid.UUID) -> int:
    column = model.project_id  # type: ignore[attr-defined]
    return int(await session.scalar(select(func.count()).where(column == project_id)) or 0)


def _message(batch: MetricBatchMessage | bytes) -> ConsumedMessage:
    value = batch if isinstance(batch, bytes) else batch.model_dump_json().encode()
    return ConsumedMessage("telemetry.metrics.v1", 0, 7, b"key", value, {})


async def test_stores_batch_with_services_series_and_points(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    batch = _batch(
        tenant,
        [
            metric_point(T0, 100.0, attributes={"region": "eu"}),
            metric_point(T0 + timedelta(seconds=10), 120.0, attributes={"region": "eu"}),
            metric_point(T0, 90.0, attributes={"region": "us"}),
            metric_point(T0, 0.5, service="checkout", metric="errors", unit=None),
        ],
    )

    result = await store_metric_batch(db_session, batch)

    assert result.outcome is StoreOutcome.STORED
    assert result.stored_points == 4
    project = tenant.project.id
    assert await _count(db_session, Service, project) == 2
    assert await _count(db_session, MetricSeries, project) == 3  # eu, us, checkout/errors
    assert await _count(db_session, MetricPoint, project) == 4
    stored = await db_session.get(IngestBatch, batch.batch_id)
    assert stored is not None
    assert (stored.point_count, stored.stored_point_count) == (4, 4)


async def test_replaying_a_batch_is_a_no_op(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    batch = _batch(tenant, [metric_point(T0, 1.0), metric_point(T0 + timedelta(seconds=1), 2.0)])

    first = await store_metric_batch(db_session, batch)
    replay = await store_metric_batch(db_session, batch)

    assert (first.outcome, replay.outcome) == (StoreOutcome.STORED, StoreOutcome.DUPLICATE)
    assert replay.stored_points == 0
    assert await _count(db_session, MetricPoint, tenant.project.id) == 2
    assert await _count(db_session, IngestBatch, tenant.project.id) == 1


async def test_reused_batch_id_with_different_content_is_a_conflict(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    batch_id = uuid.uuid4()
    await store_metric_batch(db_session, _batch(tenant, [metric_point(T0, 1.0)], batch_id=batch_id))

    with pytest.raises(IdempotencyConflictError):
        await store_metric_batch(
            db_session, _batch(tenant, [metric_point(T0, 999.0)], batch_id=batch_id)
        )

    value = await db_session.scalar(
        select(MetricPoint.value).where(MetricPoint.project_id == tenant.project.id)
    )
    assert value == 1.0


async def test_first_write_wins_for_the_same_series_and_timestamp(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    await store_metric_batch(db_session, _batch(tenant, [metric_point(T0, 1.0)]))

    second = await store_metric_batch(
        db_session,
        _batch(tenant, [metric_point(T0, 2.0), metric_point(T0 + timedelta(seconds=1), 3.0)]),
    )

    assert second.stored_points == 1  # only the new timestamp was written
    values = (
        await db_session.scalars(
            select(MetricPoint.value)
            .where(MetricPoint.project_id == tenant.project.id)
            .order_by(MetricPoint.ts)
        )
    ).all()
    assert list(values) == [1.0, 3.0]


async def test_services_and_series_are_reused_across_batches(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    for offset in range(3):
        await store_metric_batch(
            db_session, _batch(tenant, [metric_point(T0 + timedelta(minutes=offset), 1.0)])
        )

    assert await _count(db_session, Service, tenant.project.id) == 1
    assert await _count(db_session, MetricSeries, tenant.project.id) == 1
    assert await _count(db_session, MetricPoint, tenant.project.id) == 3


async def test_same_service_name_in_two_projects_stays_separate(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant_a = await make_tenant()
    tenant_b = await make_tenant()

    await store_metric_batch(db_session, _batch(tenant_a, [metric_point(T0, 1.0)]))
    await store_metric_batch(db_session, _batch(tenant_b, [metric_point(T0, 2.0)]))

    service_a = await db_session.scalar(
        select(Service).where(Service.project_id == tenant_a.project.id)
    )
    service_b = await db_session.scalar(
        select(Service).where(Service.project_id == tenant_b.project.id)
    )
    assert service_a is not None
    assert service_b is not None
    assert service_a.id != service_b.id


async def test_database_rejects_a_batch_whose_org_and_project_do_not_match(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant_a = await make_tenant()
    tenant_b = await make_tenant()
    forged = metric_batch(
        organization_id=tenant_a.organization.id,
        project_id=tenant_b.project.id,
        api_key_id=tenant_a.api_key.id,
        points=[metric_point(T0, 1.0)],
        received_at=T0,
    )

    with pytest.raises(IntegrityError):
        await store_metric_batch(db_session, forged)


# --- Handler: maps outcomes onto the consumer's retry / dead-letter contract ---------------


async def test_handler_stores_a_valid_message(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_tenant: TenantFactory,
) -> None:
    tenant = await make_tenant()
    handler = MetricBatchHandler(session_factory)

    await handler(_message(_batch(tenant, [metric_point(T0, 1.0)])))

    assert await _count(db_session, MetricPoint, tenant.project.id) == 1


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"{}", b'{"schema_version": 99}'],
    ids=["not-json", "empty-object", "unknown-version"],
)
async def test_handler_dead_letters_malformed_messages(
    session_factory: async_sessionmaker[AsyncSession], raw: bytes
) -> None:
    with pytest.raises(PermanentMessageError) as excinfo:
        await MetricBatchHandler(session_factory)(_message(raw))

    assert excinfo.value.reason == "invalid_message"


async def test_handler_dead_letters_idempotency_conflicts(
    session_factory: async_sessionmaker[AsyncSession], make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    handler = MetricBatchHandler(session_factory)
    batch_id = uuid.uuid4()
    await handler(_message(_batch(tenant, [metric_point(T0, 1.0)], batch_id=batch_id)))

    with pytest.raises(PermanentMessageError) as excinfo:
        await handler(_message(_batch(tenant, [metric_point(T0, 2.0)], batch_id=batch_id)))

    assert excinfo.value.reason == "idempotency_conflict"


async def test_handler_dead_letters_rows_the_database_rejects(
    session_factory: async_sessionmaker[AsyncSession], make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    orphan = metric_batch(
        organization_id=tenant.organization.id,
        project_id=uuid.uuid4(),  # project does not exist
        api_key_id=tenant.api_key.id,
        points=[metric_point(T0, 1.0)],
        received_at=T0,
    )

    with pytest.raises(PermanentMessageError) as excinfo:
        await MetricBatchHandler(session_factory)(_message(orphan))

    assert excinfo.value.reason == "rejected_by_database"
