"""Publishing to Kafka from asyncio code.

librdkafka is callback-based: ``produce()`` only enqueues locally, and the delivery report
arrives later on whichever thread calls ``poll()``. ``KafkaPublisher`` runs a dedicated poll
thread and bridges each delivery report back to an asyncio future, so ``await publish()``
returns only once the broker has acknowledged the write (``acks=all``).
"""

import asyncio
import contextlib
import threading
from collections.abc import Iterable, Mapping
from typing import Protocol

import structlog
from confluent_kafka import KafkaError, KafkaException, Message, Producer

from incident_intel.core.config import RuntimeSettings
from incident_intel.streaming.client import base_config

logger = structlog.get_logger(__name__)


class PublishError(Exception):
    """The message was not confirmed as durably written. Safe to retry (idempotent keys)."""


class MessagePublisher(Protocol):
    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None: ...

    async def missing_topics(
        self, topics: Iterable[str], *, timeout_seconds: float
    ) -> set[str]: ...

    async def close(self) -> None: ...


def _settle(future: asyncio.Future[None], error: KafkaError | None) -> None:
    if future.done():  # the caller already gave up waiting
        return
    if error is None:
        future.set_result(None)
    else:
        future.set_exception(PublishError(f"delivery failed: {error.name()}"))


class KafkaPublisher:
    def __init__(self, settings: RuntimeSettings, *, delivery_timeout_seconds: float) -> None:
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._producer = Producer(
            {
                **base_config(settings),
                # Durability: the leader and all in-sync replicas must have the write.
                "acks": "all",
                # Broker-side dedupe of producer retries (no duplicates from our own retries).
                "enable.idempotence": True,
                "compression.type": "zstd",
                "linger.ms": 5,
                "message.max.bytes": settings.kafka_max_message_bytes,
                # librdkafka gives up (and reports failure) after this long.
                "delivery.timeout.ms": int(delivery_timeout_seconds * 1000),
            }
        )
        self._stopping = threading.Event()
        self._poller = threading.Thread(
            target=self._poll_loop, name="kafka-producer-poll", daemon=True
        )
        self._poller.start()

    def _poll_loop(self) -> None:
        while not self._stopping.is_set():
            self._producer.poll(0.1)

    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        delivered: asyncio.Future[None] = loop.create_future()

        def on_delivery(error: KafkaError | None, _message: Message) -> None:
            # RuntimeError: the event loop already closed during shutdown.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_settle, delivered, error)

        try:
            self._producer.produce(
                topic,
                value=value,
                key=key,
                headers=list((headers or {}).items()),
                on_delivery=on_delivery,
            )
        except BufferError as exc:
            raise PublishError("local producer queue is full") from exc
        except KafkaException as exc:
            raise PublishError(f"produce rejected: {exc.args[0].name()}") from exc

        try:
            # Slightly longer than librdkafka's own timeout, so its verdict normally wins.
            await asyncio.wait_for(delivered, timeout=self._delivery_timeout_seconds + 1)
        except TimeoutError as exc:
            raise PublishError("delivery not confirmed in time") from exc

    async def missing_topics(self, topics: Iterable[str], *, timeout_seconds: float) -> set[str]:
        """Return which of ``topics`` do not exist. Raises PublishError if Kafka is unreachable."""
        try:
            metadata = await asyncio.to_thread(
                lambda: self._producer.list_topics(timeout=timeout_seconds)
            )
        except KafkaException as exc:
            raise PublishError(f"metadata request failed: {exc.args[0].name()}") from exc
        existing = {name for name, topic in metadata.topics.items() if topic.error is None}
        return set(topics) - existing

    async def close(self) -> None:
        remaining = await asyncio.to_thread(self._producer.flush, 5.0)
        if remaining:
            logger.warning("kafka_producer_closed_with_pending_messages", pending=remaining)
        self._stopping.set()
        await asyncio.to_thread(self._poller.join, 2.0)
