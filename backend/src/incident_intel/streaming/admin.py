"""Topic administration and dead-letter inspection (admin CLI only; blocking calls)."""

import uuid
from collections.abc import Iterable
from typing import Literal

from confluent_kafka import OFFSET_BEGINNING, Consumer, KafkaError, KafkaException, TopicPartition
from confluent_kafka.admin import AdminClient

# confluent_kafka.admin re-exports NewTopic without marking it public in its type stubs;
# importing from the defining module keeps mypy's explicit-export check meaningful.
from confluent_kafka.cimpl import NewTopic

from incident_intel.core.config import RuntimeSettings
from incident_intel.streaming.client import base_config
from incident_intel.streaming.consumer import ConsumedMessage, decode_headers
from incident_intel.streaming.topics import ALL_TOPICS, TopicSpec, topic_name

TopicStatus = Literal["created", "exists"]


def ensure_topics(
    settings: RuntimeSettings,
    specs: Iterable[TopicSpec] = ALL_TOPICS,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, TopicStatus]:
    """Create any missing topics. Existing topics are left unchanged (not reconfigured)."""
    admin = AdminClient(base_config(settings))
    new_topics = [
        NewTopic(
            topic_name(settings, spec),
            num_partitions=spec.partitions,
            replication_factor=settings.kafka_replication_factor,
            config={
                "retention.ms": str(spec.retention_ms),
                "max.message.bytes": str(settings.kafka_max_message_bytes),
            },
        )
        for spec in specs
    ]
    futures = admin.create_topics(
        new_topics, operation_timeout=timeout_seconds, request_timeout=timeout_seconds
    )
    results: dict[str, TopicStatus] = {}
    for name, future in futures.items():
        try:
            future.result()
            results[name] = "created"
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise
            results[name] = "exists"
    return results


def delete_topics(settings: RuntimeSettings, names: Iterable[str], *, timeout: float = 30) -> None:
    admin = AdminClient(base_config(settings))
    for future in admin.delete_topics(list(names), operation_timeout=timeout).values():
        future.result()


def read_topic_from_start(
    settings: RuntimeSettings, topic: str, *, limit: int, idle_timeout_seconds: float = 5.0
) -> list[ConsumedMessage]:
    """Read up to ``limit`` messages from the beginning without joining a group or committing."""
    consumer = Consumer(
        {
            **base_config(settings),
            "group.id": f"ii-inspect-{uuid.uuid4().hex}",
            "enable.auto.commit": False,
            "enable.partition.eof": True,
        }
    )
    try:
        metadata = consumer.list_topics(topic, timeout=idle_timeout_seconds)
        partitions = list(metadata.topics[topic].partitions)
        consumer.assign([TopicPartition(topic, p, OFFSET_BEGINNING) for p in partitions])

        messages: list[ConsumedMessage] = []
        finished: set[int] = set()
        while len(messages) < limit and len(finished) < len(partitions):
            raw = consumer.poll(idle_timeout_seconds)
            if raw is None:
                break
            error = raw.error()
            if error is not None:
                if error.code() == KafkaError._PARTITION_EOF:
                    finished.add(raw.partition() or 0)
                    continue
                raise KafkaException(error)
            value = raw.value()
            key = raw.key()
            messages.append(
                ConsumedMessage(
                    topic=topic,
                    partition=raw.partition() or 0,
                    offset=raw.offset() or 0,
                    key=key.encode() if isinstance(key, str) else key,
                    value=(value.encode() if isinstance(value, str) else value) or b"",
                    headers=decode_headers(raw.headers()),
                )
            )
        return messages
    finally:
        consumer.close()
