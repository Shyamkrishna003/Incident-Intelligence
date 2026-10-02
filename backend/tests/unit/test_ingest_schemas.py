from typing import Any

import pytest
from pydantic import ValidationError

from incident_intel.ingestion.schemas import MAX_POINTS_PER_BATCH, MetricBatchIn


def _point(**overrides: Any) -> dict[str, Any]:
    point: dict[str, Any] = {
        "service": "payment-api",
        "metric": "http.server.duration.p95",
        "unit": "ms",
        "timestamp": "2026-09-28T10:00:00Z",
        "value": 350.0,
        "attributes": {"region": "eu-west-1"},
    }
    point.update(overrides)
    return point


def _validate(*points: dict[str, Any], **batch_fields: Any) -> MetricBatchIn:
    return MetricBatchIn.model_validate_json(
        __import__("json").dumps({"points": list(points), **batch_fields})
    )


def test_accepts_a_valid_point() -> None:
    batch = _validate(_point())

    assert batch.points[0].value == 350.0
    assert batch.points[0].attributes == {"region": "eu-west-1"}


def test_accepts_integer_values_and_omitted_optional_fields() -> None:
    point = _point(value=42)
    del point["unit"], point["attributes"]

    batch = _validate(point)

    assert batch.points[0].value == 42.0
    assert batch.points[0].unit is None
    assert batch.points[0].attributes == {}


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"service": "Payment-API"}, "service"),  # uppercase
        ({"service": ".hidden"}, "service"),  # must start alphanumeric
        ({"service": "a" * 129}, "service"),  # too long
        ({"metric": "has space"}, "metric"),
        ({"unit": "m s"}, "unit"),
        ({"timestamp": "2026-09-28T10:00:00"}, "timestamp"),  # naive: timezone required
        ({"timestamp": "yesterday"}, "timestamp"),
        ({"value": True}, "value"),  # booleans are not numbers
        ({"value": "350"}, "value"),
        ({"value": 1e400}, "value"),  # overflows to infinity
        ({"attributes": {f"k{i}": "v" for i in range(17)}}, "attributes"),  # max 16
        ({"attributes": {"region": "x" * 257}}, "attributes"),  # value too long
        ({"attributes": {"bad key": "v"}}, "attributes"),
        ({"attributes": {"region": 1}}, "attributes"),  # values are strings
        ({"project_id": "00000000-0000-0000-0000-000000000000"}, "project_id"),  # no extras
    ],
)
def test_rejects_invalid_point(override: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _validate(_point(**override))

    assert any(field in [str(part) for part in err["loc"]] for err in excinfo.value.errors())


def test_rejects_nan() -> None:
    # Python's json module emits the non-standard NaN literal; it must still be refused.
    with pytest.raises(ValidationError):
        MetricBatchIn.model_validate_json(
            '{"points": [{"service": "a", "metric": "m", '
            '"timestamp": "2026-09-28T10:00:00Z", "value": NaN}]}'
        )


def test_rejects_empty_batch() -> None:
    with pytest.raises(ValidationError):
        _validate()


def test_rejects_oversized_batch() -> None:
    with pytest.raises(ValidationError):
        _validate(*[_point() for _ in range(MAX_POINTS_PER_BATCH + 1)])


def test_rejects_tenant_fields_at_batch_level() -> None:
    # Tenant identity comes from the API key only; the body cannot carry it.
    with pytest.raises(ValidationError):
        _validate(_point(), organization_id="00000000-0000-0000-0000-000000000000")
