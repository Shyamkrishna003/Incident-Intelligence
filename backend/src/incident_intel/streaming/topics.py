"""Topic registry. Topics are created explicitly (`ii kafka init`); broker auto-create is off."""

from dataclasses import dataclass

from incident_intel.core.config import RuntimeSettings

_DAY_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: int
    retention_ms: int


def _dead_letter(spec: TopicSpec) -> TopicSpec:
    """Where messages from ``spec`` go when they can never be processed. Kept longer, for
    inspection."""
    return TopicSpec(f"{spec.name}.dlq", partitions=1, retention_ms=14 * _DAY_MS)


# Validated batches from the ingestion API, keyed by project_id so each project's batches
# stay ordered within one partition. Retention is a replay buffer, not storage.
METRICS = TopicSpec("telemetry.metrics.v1", partitions=6, retention_ms=3 * _DAY_MS)
LOGS = TopicSpec("telemetry.logs.v1", partitions=6, retention_ms=3 * _DAY_MS)
# Low volume, and consumers will care about order across services: one partition.
DEPLOYMENTS = TopicSpec("telemetry.deployments.v1", partitions=1, retention_ms=7 * _DAY_MS)

# "New metric points are stored for these series", published by the storage consumer.
# Detection reads it, so detection only ever runs on data that is already in PostgreSQL.
METRICS_STORED = TopicSpec("telemetry.metrics.stored.v1", partitions=6, retention_ms=3 * _DAY_MS)

METRICS_DLQ = _dead_letter(METRICS)
LOGS_DLQ = _dead_letter(LOGS)
DEPLOYMENTS_DLQ = _dead_letter(DEPLOYMENTS)
METRICS_STORED_DLQ = _dead_letter(METRICS_STORED)

# Each telemetry topic with its dead-letter topic.
TELEMETRY_TOPICS = ((METRICS, METRICS_DLQ), (LOGS, LOGS_DLQ), (DEPLOYMENTS, DEPLOYMENTS_DLQ))
ALL_TOPICS = (
    *(spec for pair in TELEMETRY_TOPICS for spec in pair),
    METRICS_STORED,
    METRICS_STORED_DLQ,
)


def topic_name(settings: RuntimeSettings, spec: TopicSpec) -> str:
    return f"{settings.kafka_topic_prefix}{spec.name}"
