"""The scenario simulator: what it generates, and how it sends it."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from incident_intel.ingestion.schemas import DeploymentBatchIn, LogBatchIn, MetricBatchIn
from incident_intel.simulator.client import IngestClient, SimulatorError
from incident_intel.simulator.runner import SimulationPlan, run_simulation
from incident_intel.simulator.scenario import (
    METRICS,
    Telemetry,
    align,
    generate,
    metric_value,
    severity_at,
)

STEP = timedelta(seconds=15)
START = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
INCIDENT = START + timedelta(minutes=45)


def _full_run() -> Telemetry:
    return generate(start=START, end=START + timedelta(minutes=75), step=STEP, incident_at=INCIDENT)


def _values(telemetry: Telemetry, service: str, metric: str) -> dict[datetime, float]:
    return {
        datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")): p["value"]
        for p in telemetry.points
        if p["service"] == service and p["metric"] == metric
    }


def test_generation_is_deterministic_and_independent_of_the_window() -> None:
    whole = _full_run()
    again = _full_run()
    later_only = generate(
        start=INCIDENT, end=START + timedelta(minutes=75), step=STEP, incident_at=INCIDENT
    )

    assert whole == again
    # A value depends only on its timestamp, not on where generation started.
    whole_latency = _values(whole, "payment-api", "http.server.duration.p95")
    later_latency = _values(later_only, "payment-api", "http.server.duration.p95")
    assert all(whole_latency[at] == value for at, value in later_latency.items())
    assert generate(start=START, end=INCIDENT, step=STEP, incident_at=INCIDENT, seed=2) != generate(
        start=START, end=INCIDENT, step=STEP, incident_at=INCIDENT, seed=1
    )


def test_incident_shape() -> None:
    assert severity_at(INCIDENT - timedelta(seconds=1), INCIDENT) == 0
    assert severity_at(INCIDENT + timedelta(minutes=5), INCIDENT) == pytest.approx(0.5)
    assert severity_at(INCIDENT + timedelta(minutes=15), INCIDENT) == 1
    assert severity_at(INCIDENT + timedelta(minutes=22, seconds=30), INCIDENT) == pytest.approx(0.5)
    assert severity_at(INCIDENT + timedelta(minutes=30), INCIDENT) == 0
    assert severity_at(INCIDENT + timedelta(minutes=15), None) == 0


def test_affected_metrics_rise_and_the_control_service_stays_flat() -> None:
    telemetry = _full_run()
    before = INCIDENT - timedelta(minutes=10)
    peak = INCIDENT + timedelta(minutes=15)

    latency = _values(telemetry, "payment-api", "http.server.duration.p95")
    control = _values(telemetry, "inventory-api", "http.server.duration.p95")
    downstream = _values(telemetry, "checkout-web", "http.server.duration.p95")

    assert latency[before] < 150
    assert latency[peak] > 1500
    assert max(control.values()) < 90  # never leaves its normal band
    # The downstream service is still healthy shortly after the incident starts...
    assert downstream[INCIDENT + timedelta(minutes=1)] < 100
    # ...and degraded later.
    assert downstream[peak] > 500


def test_deployments_include_the_cause_a_red_herring_and_the_rollback() -> None:
    deployments = _full_run().deployments

    assert [(d["service"], d["version"]) for d in deployments] == [
        ("inventory-api", "1.8.2"),
        ("payment-api", "2.43.0"),
        ("payment-api", "2.42.3"),
    ]
    assert deployments[1]["deployed_at"] == "2026-10-02T12:45:00Z"
    assert generate(start=START, end=INCIDENT, step=STEP, incident_at=None).deployments == []


def test_error_logs_appear_only_during_the_incident() -> None:
    telemetry = _full_run()

    def errors_between(start: datetime, end: datetime) -> list[str]:
        return [
            r["message"]
            for r in telemetry.records
            if r["severity"] == "error"
            and start <= datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) < end
        ]

    assert errors_between(START, INCIDENT) == []
    during = errors_between(INCIDENT + timedelta(minutes=10), INCIDENT + timedelta(minutes=20))
    assert any("connection pool exhausted" in message for message in during)
    assert any("payment-api responded 503" in message for message in during)
    assert errors_between(INCIDENT + timedelta(minutes=26), START + timedelta(minutes=75)) == []


def test_everything_is_labelled_synthetic() -> None:
    telemetry = _full_run()

    assert all(p["attributes"] == {"source": "simulator"} for p in telemetry.points)
    assert all(r["attributes"] == {"source": "simulator"} for r in telemetry.records)
    assert all(d["deployed_by"] == "simulator" for d in telemetry.deployments)
    assert all("synthetic" in d["description"] for d in telemetry.deployments)


def test_generated_data_satisfies_the_ingestion_schemas() -> None:
    telemetry = _full_run()

    for offset in range(0, len(telemetry.points), 1000):
        MetricBatchIn.model_validate({"points": telemetry.points[offset : offset + 1000]})
    for offset in range(0, len(telemetry.records), 1000):
        LogBatchIn.model_validate({"records": telemetry.records[offset : offset + 1000]})
    DeploymentBatchIn.model_validate({"deployments": telemetry.deployments})
    assert len(telemetry.points) == 300 * len(METRICS)  # 75 minutes at 15-second steps


def test_values_are_never_negative() -> None:
    assert all(
        metric_value(spec, START + timedelta(seconds=15 * i), INCIDENT, seed=3) >= 0
        for spec in METRICS
        for i in range(400)
    )


def test_align_rounds_down_to_the_step_grid() -> None:
    assert align(datetime(2026, 10, 2, 12, 0, 14, 900000, tzinfo=UTC), STEP) == START
    assert align(datetime(2026, 10, 2, 12, 0, 15, tzinfo=UTC), STEP) == START + STEP


# --- Sending -----------------------------------------------------------------------------


class _Api:
    """A stand-in for the ingestion API that records requests."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = statuses or []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 202
        headers = {"Retry-After": "0"} if status in {429, 503} else {}
        return httpx.Response(status, json={"status": "accepted"}, headers=headers)

    def client(self) -> IngestClient:
        return IngestClient(
            httpx.AsyncClient(
                transport=httpx.MockTransport(self),
                base_url="http://api.test",
                headers={"Authorization": "Bearer ii_key"},
            )
        )


async def test_client_splits_into_batches_and_sends_deployments_first() -> None:
    api = _Api()
    telemetry = _full_run()

    summary = await api.client().send(telemetry)

    paths = [request.url.path for request in api.requests]
    assert paths[0] == "/v1/ingest/deployments"
    assert paths.count("/v1/ingest/metrics") == 3  # 2,700 points in batches of 1,000
    assert summary.points == len(telemetry.points)
    assert summary.records == len(telemetry.records)
    assert summary.deployments == 3
    assert summary.requests == len(api.requests)
    assert all(r.headers["Authorization"] == "Bearer ii_key" for r in api.requests)
    assert all(len(json.loads(r.content).get("points", [])) <= 1000 for r in api.requests)


async def test_resending_the_same_data_reuses_the_idempotency_keys() -> None:
    first, second = _Api(), _Api()
    telemetry = generate(start=START, end=START + timedelta(minutes=2), step=STEP, incident_at=None)

    await first.client().send(telemetry)
    await second.client().send(telemetry)

    keys = [request.headers["Idempotency-Key"] for request in first.requests]
    assert keys == [request.headers["Idempotency-Key"] for request in second.requests]
    assert len(set(keys)) == len(keys)


async def test_client_retries_when_rate_limited_using_the_same_key() -> None:
    api = _Api(statuses=[429, 503, 202])
    telemetry = Telemetry(
        points=generate(start=START, end=START + STEP, step=STEP, incident_at=None).points
    )

    await api.client().send(telemetry)

    assert len(api.requests) == 3
    assert len({request.headers["Idempotency-Key"] for request in api.requests}) == 1


async def test_client_stops_on_a_rejected_batch() -> None:
    api = _Api(statuses=[422])
    telemetry = Telemetry(points=[{"service": "Bad Name"}])

    with pytest.raises(SimulatorError, match="422"):
        await api.client().send(telemetry)

    assert len(api.requests) == 1


async def test_backfill_covers_the_requested_period_and_places_the_incident() -> None:
    api = _Api()
    now = START + timedelta(minutes=60, seconds=7)

    summary = await run_simulation(
        api.client(),
        SimulationPlan(
            backfill=timedelta(minutes=60), step=STEP, incident_after=timedelta(minutes=45)
        ),
        stop=asyncio.Event(),
        now=lambda: now,
    )

    assert summary.points == 240 * len(METRICS)
    deployments = json.loads(api.requests[0].content)["deployments"]
    # The incident deployment is 45 minutes into the hour; the rollback is still to come.
    assert [(d["version"], d["deployed_at"]) for d in deployments] == [
        ("1.8.2", "2026-10-02T12:20:00Z"),
        ("2.43.0", "2026-10-02T12:45:00Z"),
    ]


async def test_live_mode_sends_each_new_step_exactly_once() -> None:
    api = _Api()
    stop = asyncio.Event()
    clock = [START + timedelta(minutes=1)]

    def now() -> datetime:
        current = clock[0]
        clock[0] = current + timedelta(seconds=1)
        if current >= START + timedelta(minutes=1, seconds=3):
            stop.set()
        return current

    summary = await run_simulation(
        api.client(),
        SimulationPlan(
            backfill=timedelta(minutes=1),
            step=timedelta(seconds=1),
            incident_after=None,
            live=True,
        ),
        stop=stop,
        now=now,
    )

    timestamps = [
        point["timestamp"]
        for request in api.requests
        if request.url.path == "/v1/ingest/metrics"
        for point in json.loads(request.content)["points"]
        if point["service"] == "payment-api" and point["metric"] == "http.server.duration.p95"
    ]
    assert len(timestamps) == len(set(timestamps))  # no step sent twice
    assert len(timestamps) > 60  # the minute of backfill plus live steps
    assert summary.points == len(timestamps) * len(METRICS)
