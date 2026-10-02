"""Batch-level validation and normalization. Pure functions; the clock is passed in.

Any invalid item rejects the whole batch, so a batch is either fully accepted or not at
all, which keeps client retries simple.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from incident_intel.core.errors import InvalidInputError
from incident_intel.ingestion.schemas import DeploymentIn, LogRecordIn, MetricPointIn
from incident_intel.telemetry.messages import (
    DeploymentMessage,
    LogRecordMessage,
    MetricPointMessage,
    attributes_hash,
)

_MAX_REPORTED_ERRORS = 50


@dataclass(frozen=True)
class IngestLimits:
    max_point_age: timedelta
    max_future_skew: timedelta


class _Checker:
    """Collects per-item errors for one batch field (`points`, `records`, `deployments`)."""

    def __init__(self, collection: str, *, now: datetime, limits: IngestLimits) -> None:
        self._collection = collection
        self._oldest = now - limits.max_point_age
        self._newest = now + limits.max_future_skew
        self.errors: list[dict[str, Any]] = []

    def add(self, index: int, field: str, message: str, error_type: str) -> None:
        self.errors.append(
            {"loc": ["body", self._collection, index, field], "msg": message, "type": error_type}
        )

    def timestamp(self, index: int, field: str, value: datetime) -> datetime:
        """Convert to UTC and check the accepted time window."""
        utc = value.astimezone(UTC)
        if utc < self._oldest:
            self.add(index, field, "Timestamp is older than allowed.", "timestamp_too_old")
        elif utc > self._newest:
            self.add(index, field, "Timestamp is in the future.", "timestamp_in_future")
        return utc

    def finish(self, noun: str) -> None:
        if self.errors:
            raise InvalidInputError(
                f"One or more {noun} are invalid.", details=self.errors[:_MAX_REPORTED_ERRORS]
            )


def normalize_points(
    points: Sequence[MetricPointIn], *, now: datetime, limits: IngestLimits
) -> list[MetricPointMessage]:
    check = _Checker("points", now=now, limits=limits)
    seen: set[tuple[str, str, str, datetime]] = set()
    normalized: list[MetricPointMessage] = []

    for index, point in enumerate(points):
        timestamp = check.timestamp(index, "timestamp", point.timestamp)
        identity = (point.service, point.metric, attributes_hash(point.attributes), timestamp)
        if identity in seen:
            check.add(
                index,
                "timestamp",
                "Duplicate point: same series and timestamp appear earlier in the batch.",
                "duplicate_point",
            )
        seen.add(identity)
        normalized.append(
            MetricPointMessage(
                service=point.service,
                metric=point.metric,
                unit=point.unit,
                timestamp=timestamp,
                value=point.value,
                attributes=point.attributes,
            )
        )

    check.finish("points")
    return normalized


def normalize_logs(
    records: Sequence[LogRecordIn], *, now: datetime, limits: IngestLimits
) -> list[LogRecordMessage]:
    # Identical log lines at the same instant are legitimate, so there is no duplicate check.
    check = _Checker("records", now=now, limits=limits)
    normalized = [
        LogRecordMessage(
            service=record.service,
            timestamp=check.timestamp(index, "timestamp", record.timestamp),
            severity=record.severity,
            message=record.message,
            attributes=record.attributes,
            trace_id=record.trace_id,
        )
        for index, record in enumerate(records)
    ]
    check.finish("log records")
    return normalized


def normalize_deployments(
    deployments: Sequence[DeploymentIn], *, now: datetime, limits: IngestLimits
) -> list[DeploymentMessage]:
    check = _Checker("deployments", now=now, limits=limits)
    seen: set[tuple[str, str, datetime]] = set()
    normalized: list[DeploymentMessage] = []

    for index, deployment in enumerate(deployments):
        deployed_at = check.timestamp(index, "deployed_at", deployment.deployed_at)
        identity = (deployment.service, deployment.version, deployed_at)
        if identity in seen:
            check.add(
                index,
                "deployed_at",
                "Duplicate deployment: same service, version, and time appear earlier.",
                "duplicate_deployment",
            )
        seen.add(identity)
        normalized.append(
            DeploymentMessage(
                service=deployment.service,
                version=deployment.version,
                deployed_at=deployed_at,
                commit_sha=deployment.commit_sha,
                environment=deployment.environment,
                deployed_by=deployment.deployed_by,
                description=deployment.description,
            )
        )

    check.finish("deployments")
    return normalized
