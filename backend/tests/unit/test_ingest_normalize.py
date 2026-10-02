from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from incident_intel.core.errors import InvalidInputError
from incident_intel.ingestion.normalize import IngestLimits, normalize_points
from incident_intel.ingestion.schemas import MetricPointIn

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
LIMITS = IngestLimits(max_point_age=timedelta(days=7), max_future_skew=timedelta(minutes=5))


def _point(timestamp: datetime, **overrides: Any) -> MetricPointIn:
    fields: dict[str, Any] = {
        "service": "payment-api",
        "metric": "latency",
        "timestamp": timestamp,
        "value": 1.0,
        **overrides,
    }
    return MetricPointIn(**fields)


def _errors(points: list[MetricPointIn]) -> list[dict[str, Any]]:
    with pytest.raises(InvalidInputError) as excinfo:
        normalize_points(points, now=NOW, limits=LIMITS)
    assert excinfo.value.details is not None
    return excinfo.value.details


def test_converts_timestamps_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 9, 28, 17, 0, tzinfo=ist)

    [point] = normalize_points([_point(local)], now=NOW, limits=LIMITS)

    assert point.timestamp == datetime(2026, 9, 28, 11, 30, tzinfo=UTC)
    assert point.timestamp.utcoffset() == timedelta(0)


def test_accepts_the_window_boundaries() -> None:
    oldest = NOW - LIMITS.max_point_age
    newest = NOW + LIMITS.max_future_skew

    points = normalize_points([_point(oldest), _point(newest)], now=NOW, limits=LIMITS)

    assert len(points) == 2


def test_rejects_points_older_than_the_window() -> None:
    [error] = _errors([_point(NOW - LIMITS.max_point_age - timedelta(seconds=1))])

    assert error == {
        "loc": ["body", "points", 0, "timestamp"],
        "msg": "Timestamp is older than allowed.",
        "type": "timestamp_too_old",
    }


def test_rejects_points_too_far_in_the_future() -> None:
    [error] = _errors([_point(NOW + LIMITS.max_future_skew + timedelta(seconds=1))])

    assert error["type"] == "timestamp_in_future"


def test_rejects_duplicate_series_and_timestamp_within_a_batch() -> None:
    same_instant_other_zone = NOW.astimezone(timezone(timedelta(hours=2)))

    [error] = _errors([_point(NOW), _point(same_instant_other_zone)])

    assert error["type"] == "duplicate_point"
    assert error["loc"] == ["body", "points", 1, "timestamp"]


def test_same_timestamp_on_different_series_is_not_a_duplicate() -> None:
    points = normalize_points(
        [
            _point(NOW, attributes={"region": "eu"}),
            _point(NOW, attributes={"region": "us"}),
            _point(NOW, metric="errors"),
            _point(NOW, service="checkout"),
        ],
        now=NOW,
        limits=LIMITS,
    )

    assert len(points) == 4


def test_reports_every_invalid_point() -> None:
    errors = _errors(
        [
            _point(NOW - timedelta(days=30)),
            _point(NOW),
            _point(NOW + timedelta(days=1)),
        ]
    )

    assert [(e["loc"][2], e["type"]) for e in errors] == [
        (0, "timestamp_too_old"),
        (2, "timestamp_in_future"),
    ]
