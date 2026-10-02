"""Topic registry. Topics are created explicitly (`ii kafka init`); broker auto-create is off."""

from dataclasses import dataclass

from incident_intel.core.config import RuntimeSettings

_DAY_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: int
    retention_ms: int


# Validated metric batches from the ingestion API, keyed by project_id so each project's
# batches stay ordered within one partition. Retention is a replay buffer, not storage.
METRICS = TopicSpec("telemetry.metrics.v1", partitions=6, retention_ms=3 * _DAY_MS)

# Messages the storage consumer could never process, kept longer for inspection.
METRICS_DLQ = TopicSpec("telemetry.metrics.v1.dlq", partitions=1, retention_ms=14 * _DAY_MS)

ALL_TOPICS = (METRICS, METRICS_DLQ)


def topic_name(settings: RuntimeSettings, spec: TopicSpec) -> str:
    return f"{settings.kafka_topic_prefix}{spec.name}"
