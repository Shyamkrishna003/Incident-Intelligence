"""Batch-level validation and normalization. Pure functions; the clock is passed in."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from incident_intel.core.errors import InvalidInputError
from incident_intel.ingestion.schemas import MetricPointIn
from incident_intel.telemetry.messages import MetricPointMessage, attributes_hash


@dataclass(frozen=True)
class IngestLimits:
    max_point_age: timedelta
    max_future_skew: timedelta


def _error(index: int, field: str, message: str, error_type: str) -> dict[str, Any]:
    return {"loc": ["body", "points", index, field], "msg": message, "type": error_type}


def normalize_points(
    points: Sequence[MetricPointIn], *, now: datetime, limits: IngestLimits
) -> list[MetricPointMessage]:
    """Validate rules that need context (clock, whole batch) and convert timestamps to UTC.

    Any invalid point rejects the whole batch, so a batch is either fully accepted or not
    at all, which keeps client retries simple.
    """
    oldest = now - limits.max_point_age
    newest = now + limits.max_future_skew
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, datetime]] = set()
    normalized: list[MetricPointMessage] = []

    for index, point in enumerate(points):
        timestamp = point.timestamp.astimezone(UTC)
        if timestamp < oldest:
            errors.append(
                _error(index, "timestamp", "Timestamp is older than allowed.", "timestamp_too_old")
            )
        elif timestamp > newest:
            errors.append(
                _error(index, "timestamp", "Timestamp is in the future.", "timestamp_in_future")
            )

        identity = (point.service, point.metric, attributes_hash(point.attributes), timestamp)
        if identity in seen:
            errors.append(
                _error(
                    index,
                    "timestamp",
                    "Duplicate point: same series and timestamp appear earlier in the batch.",
                    "duplicate_point",
                )
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

    if errors:
        raise InvalidInputError("One or more points are invalid.", details=errors[:50])
    return normalized
