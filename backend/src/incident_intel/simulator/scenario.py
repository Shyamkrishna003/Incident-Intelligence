"""The "payment incident" scenario, as pure functions of time.

Timeline, relative to the incident start ``T``:

- before ``T``: every service is healthy, with small random variation.
- ``T - 25 min``: ``inventory-api`` deploys v1.8.2. Unrelated: a deliberate red herring.
- ``T``: ``payment-api`` deploys v2.43.0, which introduces a query that scans a whole table.
- ``T .. T + 10 min``: database query time climbs, the payment service's connection pool
  fills, its latency and error rate rise, and ``checkout-web`` (which calls it) follows
  about two minutes later. ``inventory-api`` stays healthy throughout.
- ``T + 20 min``: ``payment-api`` rolls back to v2.42.3 and everything recovers over the
  next five minutes.

Values are deterministic for a given seed and timestamp, so re-running the simulator over
the same period produces identical batches (which the ingestion API then deduplicates).
"""

import hashlib
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

SOURCE_ATTRIBUTES = {"source": "simulator"}
SIMULATED_BY = "simulator"

RAMP = timedelta(minutes=10)
ROLLBACK_AFTER = timedelta(minutes=20)
RECOVERY = timedelta(minutes=5)
DOWNSTREAM_LAG = timedelta(minutes=2)
RED_HERRING_BEFORE = timedelta(minutes=25)

BAD_VERSION = "2.43.0"
GOOD_VERSION = "2.42.3"


@dataclass(frozen=True)
class MetricSpec:
    service: str
    metric: str
    unit: str | None
    baseline: float
    # Value at the height of the incident. Equal to the baseline for unaffected metrics.
    peak: float
    noise: float  # relative, e.g. 0.05 = ±5%
    lag: timedelta = timedelta(0)


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("payments-db", "db.query.duration.p95", "ms", 15, 900, 0.08),
    MetricSpec("payments-db", "db.sequential_scans.rate", "1/s", 0.2, 40, 0.15),
    MetricSpec("payment-api", "db.client.connections.utilization", "%", 35, 95, 0.04),
    MetricSpec("payment-api", "http.server.duration.p95", "ms", 120, 1700, 0.06),
    MetricSpec("payment-api", "http.server.error_rate", "%", 0.2, 12, 0.2),
    MetricSpec("checkout-web", "http.server.duration.p95", "ms", 90, 600, 0.06, DOWNSTREAM_LAG),
    MetricSpec("checkout-web", "http.server.error_rate", "%", 0.1, 6, 0.2, DOWNSTREAM_LAG),
    # Control: healthy throughout, so a detector has something it must NOT flag.
    MetricSpec("inventory-api", "http.server.duration.p95", "ms", 80, 80, 0.06),
    MetricSpec("inventory-api", "http.server.error_rate", "%", 0.1, 0.1, 0.2),
)


@dataclass
class Telemetry:
    """Request bodies for the three ingestion endpoints."""

    points: list[dict[str, Any]] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)


def _rng(seed: int, *parts: object) -> random.Random:
    """A generator that depends only on the seed and what is being generated, so a value
    does not change with how much was generated before it."""
    digest = hashlib.sha256(":".join(str(part) for part in (seed, *parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311 - not for security


def severity_at(at: datetime, incident_at: datetime | None, lag: timedelta = timedelta(0)) -> float:
    """How bad things are at ``at``: 0 = healthy, 1 = the height of the incident."""
    if incident_at is None:
        return 0.0
    elapsed = at - incident_at - lag
    if elapsed <= timedelta(0):
        return 0.0
    if elapsed < RAMP:
        return elapsed / RAMP
    since_rollback = elapsed - ROLLBACK_AFTER
    if since_rollback <= timedelta(0):
        return 1.0
    return max(0.0, 1.0 - since_rollback / RECOVERY)


def metric_value(spec: MetricSpec, at: datetime, incident_at: datetime | None, seed: int) -> float:
    level = spec.baseline + (spec.peak - spec.baseline) * severity_at(at, incident_at, spec.lag)
    jitter = _rng(seed, spec.service, spec.metric, at.timestamp()).uniform(-spec.noise, spec.noise)
    return round(max(0.0, level * (1 + jitter)), 3)


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _log(service: str, at: datetime, severity: str, message: str) -> dict[str, Any]:
    return {
        "service": service,
        "timestamp": _iso(at),
        "severity": severity,
        "message": message,
        "attributes": dict(SOURCE_ATTRIBUTES),
    }


def _logs_at(
    at: datetime, incident_at: datetime | None, seed: int, step_index: int
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    rng = _rng(seed, "logs", at.timestamp())
    core = severity_at(at, incident_at)
    downstream = severity_at(at, incident_at, DOWNSTREAM_LAG)

    # Routine chatter, sparse so the volume stays small.
    if step_index % 4 == 0:
        for service in ("payment-api", "checkout-web", "inventory-api"):
            records.append(_log(service, at, "info", f"handled {rng.randint(180, 260)} requests"))

    if core > 0.15:
        duration = round(15 + 885 * core * rng.uniform(0.9, 1.1))
        records.append(
            _log(
                "payments-db",
                at,
                "warn",
                # The text of a simulated log line, not a query that is ever executed.
                "slow query: SELECT * FROM payments WHERE customer_ref = $1 "  # noqa: S608
                f"(sequential scan, duration={duration}ms)",
            )
        )
    if core > 0.5:
        records.append(
            _log(
                "payment-api",
                at,
                "error",
                "connection pool exhausted: timed out after 5000ms waiting for a database "
                "connection (pool size 20)",
            )
        )
        if rng.random() < core:
            records.append(
                _log("payment-api", at, "error", "payment authorization failed: database timeout")
            )
    if downstream > 0.5:
        records.append(
            _log("checkout-web", at, "error", "POST /checkout failed: payment-api responded 503")
        )
    return records


def _deployments(
    start: datetime, end: datetime, incident_at: datetime | None
) -> list[dict[str, Any]]:
    if incident_at is None:
        return []
    candidates = [
        (
            "inventory-api",
            "1.8.2",
            incident_at - RED_HERRING_BEFORE,
            "9c1d2e3",
            "Add stock-level cache",
        ),
        (
            "payment-api",
            BAD_VERSION,
            incident_at,
            "4f7a9b2",
            "Look up payments by customer reference",
        ),
        (
            "payment-api",
            GOOD_VERSION,
            incident_at + ROLLBACK_AFTER,
            "a81c0de",
            f"Roll back {BAD_VERSION}",
        ),
    ]
    return [
        {
            "service": service,
            "version": version,
            "deployed_at": _iso(at),
            "commit_sha": commit,
            "environment": "production",
            "deployed_by": SIMULATED_BY,
            "description": f"{description} (synthetic deployment generated by ii simulate)",
        }
        for service, version, at, commit, description in candidates
        if start <= at < end
    ]


def generate(
    *,
    start: datetime,
    end: datetime,
    step: timedelta,
    incident_at: datetime | None,
    seed: int = 1,
) -> Telemetry:
    """Everything the scenario produces in ``[start, end)``, one sample per ``step``.

    ``start`` should lie on the step grid (see ``align``) so repeated runs line up.
    ``incident_at=None`` produces a healthy baseline with no incident.
    """
    telemetry = Telemetry(deployments=_deployments(start, end, incident_at))
    at = start
    while at < end:
        step_index = int(at.timestamp() // step.total_seconds())
        for spec in METRICS:
            telemetry.points.append(
                {
                    "service": spec.service,
                    "metric": spec.metric,
                    **({"unit": spec.unit} if spec.unit else {}),
                    "timestamp": _iso(at),
                    "value": metric_value(spec, at, incident_at, seed),
                    "attributes": dict(SOURCE_ATTRIBUTES),
                }
            )
        telemetry.records.extend(_logs_at(at, incident_at, seed, step_index))
        at += step
    return telemetry


def align(at: datetime, step: timedelta) -> datetime:
    """Round ``at`` down to the step grid."""
    seconds = step.total_seconds()
    return datetime.fromtimestamp(at.timestamp() // seconds * seconds, tz=UTC)
