"""Detection against PostgreSQL: the service, the event flow, and the anomalies API."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.config import Settings
from incident_intel.detection.consumer import detection_handler
from incident_intel.detection.detectors import RobustZScoreDetector
from incident_intel.detection.engine import EngineConfig, Episode, Point
from incident_intel.detection.evaluation import LabelledSeries, scenarios
from incident_intel.detection.evaluation import detect_series as pure_detect
from incident_intel.detection.models import Anomaly, DetectionState
from incident_intel.detection.service import DetectionSettings, detect_series
from incident_intel.streaming.consumer import ConsumedMessage, PermanentMessageError
from incident_intel.telemetry.consumer import metric_handler
from incident_intel.telemetry.messages import MetricsStoredEvent
from incident_intel.telemetry.storage import store_metric_batch
from incident_intel.tenancy.api_keys import ApiKeyScope
from tests.conftest import Tenant, TenantFactory
from tests.support import FakePublisher, metric_batch, metric_point

pytestmark = pytest.mark.integration

STEP = timedelta(seconds=15)
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
SETTINGS = DetectionSettings(
    detector=RobustZScoreDetector(threshold=6.0, min_history=30), engine=EngineConfig()
)


async def _store(
    session: AsyncSession, tenant: Tenant, points: list[Point], *, metric: str = "latency"
) -> uuid.UUID:
    """Store points for one series and return its id."""
    result = await store_metric_batch(
        session,
        metric_batch(
            organization_id=tenant.organization.id,
            project_id=tenant.project.id,
            api_key_id=tenant.api_key.id,
            points=[metric_point(p.ts, p.value, metric=metric) for p in points],
            received_at=T0,
        ),
    )
    return result.series_ids[0]


def _healthy(count: int, start: int = 0) -> list[Point]:
    wobble = [100.0, 101.0, 99.0, 100.5, 99.5]
    return [Point(T0 + (start + i) * STEP, wobble[i % 5]) for i in range(count)]


def _shifted(count: int, start: int, value: float = 300.0) -> list[Point]:
    return [Point(T0 + (start + i) * STEP, value) for i in range(count)]


async def _detect(session: AsyncSession, tenant: Tenant, series_id: uuid.UUID) -> None:
    await detect_series(
        session, project_id=tenant.project.id, series_id=series_id, settings=SETTINGS
    )
    await session.commit()


async def _anomalies(session: AsyncSession, series_id: uuid.UUID) -> list[Anomaly]:
    rows = await session.scalars(
        select(Anomaly).where(Anomaly.series_id == series_id).order_by(Anomaly.started_at)
    )
    return list(rows)


# --- Service ---------------------------------------------------------------------------


async def test_a_shift_opens_one_anomaly_with_its_evidence(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    series_id = await _store(db_session, tenant, [*_healthy(40), *_shifted(5, 40)])

    await _detect(db_session, tenant, series_id)

    [anomaly] = await _anomalies(db_session, series_id)
    assert anomaly.status == "open"
    assert anomaly.ended_at is None
    assert anomaly.started_at == T0 + 40 * STEP
    assert anomaly.detected_at == T0 + 41 * STEP
    assert anomaly.point_count == 5
    assert (anomaly.direction, anomaly.peak_value) == ("above", 300.0)
    assert anomaly.baseline_center == pytest.approx(100.0)
    assert anomaly.detector == "robust_zscore"
    assert anomaly.project_id == tenant.project.id
    state = await db_session.get(DetectionState, (series_id, "robust_zscore"))
    assert state is not None
    assert state.evaluated_through == T0 + 44 * STEP


async def test_detection_is_idempotent(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    series_id = await _store(db_session, tenant, [*_healthy(40), *_shifted(5, 40)])
    await _detect(db_session, tenant, series_id)
    [before] = await _anomalies(db_session, series_id)
    snapshot = (before.id, before.point_count, before.last_anomalous_at)

    # The same event delivered again: no new points, so nothing changes.
    await _detect(db_session, tenant, series_id)
    await _detect(db_session, tenant, series_id)

    [after] = await _anomalies(db_session, series_id)
    assert (after.id, after.point_count, after.last_anomalous_at) == snapshot


async def test_an_open_anomaly_is_extended_then_closed_by_later_batches(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    series_id = await _store(db_session, tenant, [*_healthy(40), *_shifted(3, 40)])
    await _detect(db_session, tenant, series_id)

    await _store(db_session, tenant, _shifted(4, 43, value=500.0))
    await _detect(db_session, tenant, series_id)
    [extended] = await _anomalies(db_session, series_id)
    assert (extended.status, extended.point_count, extended.peak_value) == ("open", 7, 500.0)

    await _store(db_session, tenant, _healthy(12, start=47))
    await _detect(db_session, tenant, series_id)

    [closed] = await _anomalies(db_session, series_id)
    assert (closed.id, closed.status, closed.closed_reason) == (extended.id, "closed", "recovered")
    assert closed.last_anomalous_at == T0 + 46 * STEP
    assert closed.ended_at is not None
    assert closed.ended_at >= closed.last_anomalous_at + timedelta(minutes=2)


async def test_late_points_before_the_evaluated_position_are_ignored(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    series_id = await _store(db_session, tenant, _healthy(40, start=10))
    await _detect(db_session, tenant, series_id)

    # Points older than anything already evaluated arrive late.
    await _store(db_session, tenant, _shifted(5, 0))
    await _detect(db_session, tenant, series_id)

    assert await _anomalies(db_session, series_id) == []


@pytest.mark.parametrize(
    "scenario_name", ["payment_incident", "brief_spike", "single_outliers", "drop"]
)
async def test_stepwise_detection_through_the_database_matches_one_pure_pass(
    db_session: AsyncSession, make_tenant: TenantFactory, scenario_name: str
) -> None:
    """Storing and evaluating a series a few points at a time must find exactly the
    anomalies the pure engine finds in a single pass over the whole series."""
    tenant = await make_tenant()
    scenario = next(s for s in scenarios() if s.name == scenario_name)
    # For the incident: one affected series, the delayed downstream one, and the control.
    chosen: list[LabelledSeries] = (
        [scenario.series[3], scenario.series[5], scenario.series[7]]
        if scenario_name == "payment_incident"
        else scenario.series
    )

    for index, labelled in enumerate(chosen):
        metric = f"metric-{index}"
        series_id = None
        for offset in range(0, len(labelled.points), 23):
            series_id = await _store(
                db_session, tenant, labelled.points[offset : offset + 23], metric=metric
            )
            await _detect(db_session, tenant, series_id)
        assert series_id is not None

        expected = pure_detect(labelled, SETTINGS.detector, SETTINGS.engine)
        stored = await _anomalies(db_session, series_id)

        def key(episode: Episode | Anomaly) -> tuple[object, ...]:
            return (
                episode.started_at,
                episode.detected_at,
                episode.last_anomalous_at,
                episode.ended_at,
                episode.closed_reason,
                episode.point_count,
                episode.severity,
                episode.direction,
                round(episode.peak_value, 6),
                round(episode.peak_score, 6),
            )

        assert [key(row) for row in stored] == [key(episode) for episode in expected]


async def test_database_allows_only_one_open_anomaly_per_series_and_detector(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    series_id = await _store(db_session, tenant, [*_healthy(40), *_shifted(5, 40)])
    await _detect(db_session, tenant, series_id)
    [existing] = await _anomalies(db_session, series_id)

    db_session.add(
        Anomaly(
            organization_id=existing.organization_id,
            project_id=existing.project_id,
            series_id=series_id,
            detector=existing.detector,
            detector_version="1",
            status="open",
            direction="above",
            severity="low",
            started_at=T0,
            detected_at=T0,
            last_anomalous_at=T0,
            peak_score=7,
            peak_value=1,
            peak_at=T0,
            baseline_center=1,
            baseline_spread=1,
            point_count=2,
        )
    )
    with pytest.raises(IntegrityError, match="uq_anomalies_open_per_series"):
        await db_session.flush()
    await db_session.rollback()


async def test_a_series_from_another_project_is_not_evaluated(
    db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    mine = await make_tenant()
    theirs = await make_tenant()
    their_series = await _store(db_session, theirs, [*_healthy(40), *_shifted(5, 40)])

    # An event claiming their series under my project must do nothing.
    outcome = await detect_series(
        db_session, project_id=mine.project.id, series_id=their_series, settings=SETTINGS
    )

    assert outcome.evaluated_points == 0
    assert await _anomalies(db_session, their_series) == []


# --- Event flow --------------------------------------------------------------------------


async def test_storing_metrics_announces_the_series_even_when_redelivered(
    session_factory: async_sessionmaker[AsyncSession], make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    publisher = FakePublisher()
    handler = metric_handler(session_factory, publisher=publisher, stored_topic="stored")
    batch = metric_batch(
        organization_id=tenant.organization.id,
        project_id=tenant.project.id,
        api_key_id=tenant.api_key.id,
        points=[metric_point(T0, 1.0), metric_point(T0, 2.0, metric="errors")],
        received_at=T0,
    )
    message = ConsumedMessage(
        "telemetry.metrics.v1", 0, 1, None, batch.model_dump_json().encode(), {}
    )

    await handler(message)
    await handler(message)  # redelivery: stored nothing, but must still be announced

    events = [MetricsStoredEvent.model_validate_json(m.value) for m in publisher.messages]
    assert [m.topic for m in publisher.messages] == ["stored", "stored"]
    assert events[0] == events[1]
    assert events[0].project_id == tenant.project.id
    assert len(events[0].series_ids) == 2
    assert publisher.messages[0].key == str(tenant.project.id).encode()


async def test_a_failed_announcement_is_retried_not_acknowledged(
    session_factory: async_sessionmaker[AsyncSession], make_tenant: TenantFactory
) -> None:
    tenant = await make_tenant()
    publisher = FakePublisher(fail_publish=True)
    handler = metric_handler(session_factory, publisher=publisher, stored_topic="stored")
    batch = metric_batch(
        organization_id=tenant.organization.id,
        project_id=tenant.project.id,
        api_key_id=tenant.api_key.id,
        points=[metric_point(T0, 1.0)],
        received_at=T0,
    )
    message = ConsumedMessage(
        "telemetry.metrics.v1", 0, 1, None, batch.model_dump_json().encode(), {}
    )

    # Not a PermanentMessageError: the consumer loop retries the message.
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - any non-permanent error
        await handler(message)
    assert not isinstance(excinfo.value, PermanentMessageError)

    publisher.fail_publish = False
    await handler(message)
    assert len(publisher.messages) == 1


async def test_detection_handler_processes_an_event_and_rejects_garbage(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_tenant: TenantFactory,
) -> None:
    tenant = await make_tenant()
    series_id = await _store(db_session, tenant, [*_healthy(40), *_shifted(5, 40)])
    handler = detection_handler(session_factory, SETTINGS)
    event = MetricsStoredEvent(
        organization_id=tenant.organization.id,
        project_id=tenant.project.id,
        batch_id=uuid.uuid4(),
        series_ids=[series_id, uuid.uuid4()],  # the unknown series is skipped
    )

    await handler(ConsumedMessage("stored", 0, 1, None, event.model_dump_json().encode(), {}))

    assert len(await _anomalies(db_session, series_id)) == 1
    with pytest.raises(PermanentMessageError):
        await handler(ConsumedMessage("stored", 0, 2, None, b"not an event", {}))


def test_detection_settings_come_from_configuration(settings: Settings) -> None:
    custom = settings.model_copy(
        update={"detection_detector": "ewma", "detection_threshold": 9.0, "detection_window": 60}
    )

    detection = DetectionSettings.from_settings(custom)

    assert (detection.detector.name, detection.detector.threshold) == ("ewma", 9.0)
    assert detection.engine.window == 60


# --- API ---------------------------------------------------------------------------------


async def test_anomalies_api_lists_filters_and_isolates(
    client: AsyncClient, db_session: AsyncSession, make_tenant: TenantFactory
) -> None:
    mine = await make_tenant()
    theirs = await make_tenant()
    recovered = await _store(
        db_session,
        mine,
        [*_healthy(40), *_shifted(4, 40), *_healthy(12, start=44)],
        metric="latency",
    )
    ongoing = await _store(
        db_session, mine, [*_healthy(40), *_shifted(4, 40, value=5.0)], metric="traffic"
    )
    their_series = await _store(db_session, theirs, [*_healthy(40), *_shifted(4, 40)])
    for tenant, series_id in ((mine, recovered), (mine, ongoing), (theirs, their_series)):
        await _detect(db_session, tenant, series_id)
    window = {
        "start": (T0 - timedelta(hours=1)).isoformat(),
        "end": (T0 + timedelta(hours=1)).isoformat(),
    }

    everything = await client.get("/v1/anomalies", params=window, headers=mine.auth_headers)
    only_open = await client.get(
        "/v1/anomalies", params={**window, "status": "open"}, headers=mine.auth_headers
    )
    one_metric = await client.get(
        "/v1/anomalies", params={**window, "metric": "latency"}, headers=mine.auth_headers
    )
    outside = await client.get(
        "/v1/anomalies",
        params={
            "start": (T0 + timedelta(hours=2)).isoformat(),
            "end": (T0 + timedelta(hours=3)).isoformat(),
            "status": "closed",
        },
        headers=mine.auth_headers,
    )

    listed = everything.json()["anomalies"]
    # Open first, and never another project's.
    assert [(a["metric"], a["status"]) for a in listed] == [
        ("traffic", "open"),
        ("latency", "closed"),
    ]
    assert listed[0]["direction"] == "below"
    assert listed[0]["service"] == "payment-api"
    assert listed[1]["closed_reason"] == "recovered"
    assert listed[1]["baseline_center"] == pytest.approx(100.0)
    assert [a["metric"] for a in only_open.json()["anomalies"]] == ["traffic"]
    assert [a["metric"] for a in one_metric.json()["anomalies"]] == ["latency"]
    assert outside.json()["anomalies"] == []


async def test_anomalies_api_requires_read_scope_and_validates_the_range(
    client: AsyncClient, make_tenant: TenantFactory
) -> None:
    write_only = await make_tenant(scopes={ApiKeyScope.INGEST_WRITE})
    reader = await make_tenant()

    forbidden = await client.get("/v1/anomalies", headers=write_only.auth_headers)
    too_long = await client.get(
        "/v1/anomalies",
        params={"start": (T0 - timedelta(days=40)).isoformat(), "end": T0.isoformat()},
        headers=reader.auth_headers,
    )
    unknown_service = await client.get(
        "/v1/anomalies", params={"service": "nope"}, headers=reader.auth_headers
    )

    assert (forbidden.status_code, too_long.status_code, unknown_service.status_code) == (
        403,
        422,
        404,
    )
