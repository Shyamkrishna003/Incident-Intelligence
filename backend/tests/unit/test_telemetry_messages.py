import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from incident_intel.ingestion.service import derive_batch_id
from incident_intel.telemetry.messages import (
    MetricBatchMessage,
    attributes_hash,
    points_content_hash,
)
from tests.support import metric_batch, metric_point

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def test_attributes_hash_ignores_key_order() -> None:
    assert attributes_hash({"a": "1", "b": "2"}) == attributes_hash({"b": "2", "a": "1"})
    assert attributes_hash({"a": "1"}) != attributes_hash({"a": "2"})
    assert attributes_hash({}) != attributes_hash({"a": ""})


def test_content_hash_changes_with_any_value() -> None:
    base = [metric_point(NOW, 1.0)]

    assert points_content_hash(base) == points_content_hash([metric_point(NOW, 1.0)])
    assert points_content_hash(base) != points_content_hash([metric_point(NOW, 1.5)])


def test_batch_message_round_trips_through_json() -> None:
    message = metric_batch(
        organization_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        points=[metric_point(NOW, 350.0, attributes={"region": "eu"})],
        received_at=NOW,
    )

    decoded = MetricBatchMessage.model_validate_json(message.model_dump_json())

    assert decoded == message


def test_batch_message_rejects_unknown_schema_version() -> None:
    message = metric_batch(
        organization_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        points=[metric_point(NOW, 1.0)],
        received_at=NOW,
    )
    payload = message.model_dump(mode="json") | {"schema_version": 2}

    with pytest.raises(ValidationError):
        MetricBatchMessage.model_validate(payload)


def test_batch_id_is_stable_per_project_and_key() -> None:
    project_a, project_b = uuid.uuid4(), uuid.uuid4()

    assert derive_batch_id(project_a, "k1") == derive_batch_id(project_a, "k1")
    assert derive_batch_id(project_a, "k1") != derive_batch_id(project_a, "k2")
    # The same key in another project must not collide.
    assert derive_batch_id(project_a, "k1") != derive_batch_id(project_b, "k1")
