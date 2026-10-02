"""At-least-once message processing with dead-lettering.

Delivery contract implemented by ``run_consumer``:

1. A message's offset is committed only after its handler succeeded, or after the message
   was written to the dead-letter topic. A crash before the commit means the message is
   delivered again, so handlers must be idempotent.
2. ``PermanentMessageError`` (the message can never succeed, e.g. it is malformed) sends the
   message to the dead-letter topic, then processing continues with the next one.
3. Any other exception is treated as transient (e.g. the database is down): the same message
   is retried with capped exponential backoff and the partition does not advance. Blocking
   is deliberate: messages wait durably in Kafka instead of being dropped.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Protocol, TypeVar

import structlog
from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition

from incident_intel.core.config import RuntimeSettings
from incident_intel.streaming.client import base_config
from incident_intel.streaming.producer import MessagePublisher, PublishError

logger = structlog.get_logger(__name__)

T = TypeVar("T")

_MAX_DLQ_DETAIL = 500


@dataclass(frozen=True)
class ConsumedMessage:
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes
    headers: dict[str, str]

    @property
    def position(self) -> str:
        return f"{self.topic}/{self.partition}@{self.offset}"


class PermanentMessageError(Exception):
    """The message can never be processed successfully; dead-letter it."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


MessageHandler = Callable[[ConsumedMessage], Awaitable[None]]


class MessageSource(Protocol):
    async def poll(self, timeout_seconds: float) -> ConsumedMessage | None: ...

    async def commit(self, message: ConsumedMessage) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class RetryPolicy:
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0

    def delay(self, attempt: int) -> float:
        return float(min(self.max_delay_seconds, self.initial_delay_seconds * 2 ** (attempt - 1)))


async def run_consumer(
    *,
    source: MessageSource,
    handler: MessageHandler,
    publisher: MessagePublisher,
    dlq_topic: str | Mapping[str, str],
    stop: asyncio.Event,
    retry: RetryPolicy | None = None,
    poll_timeout_seconds: float = 1.0,
) -> None:
    """Process messages until ``stop`` is set. Returns without committing an unfinished one.

    ``dlq_topic`` is one dead-letter topic, or a mapping from each source topic to its own.
    """
    retry = retry or RetryPolicy()
    while not stop.is_set():
        message = await source.poll(poll_timeout_seconds)
        if message is None:
            continue
        settled = await _process_until_settled(
            message, handler=handler, publisher=publisher, dlq_topic=dlq_topic,
            stop=stop, retry=retry,
        )  # fmt: skip
        if not settled:
            return
        await source.commit(message)


async def _process_until_settled(
    message: ConsumedMessage,
    *,
    handler: MessageHandler,
    publisher: MessagePublisher,
    dlq_topic: str | Mapping[str, str],
    stop: asyncio.Event,
    retry: RetryPolicy,
) -> bool:
    """Handle ``message`` until it succeeds or is dead-lettered. False if stopped first."""
    attempt = 0
    while True:
        try:
            await handler(message)
            return True
        except PermanentMessageError as exc:
            try:
                # A missing mapping entry is a wiring bug: the KeyError is retried and
                # logged loudly instead of silently dropping the message.
                target = dlq_topic if isinstance(dlq_topic, str) else dlq_topic[message.topic]
                await _dead_letter(message, exc, publisher=publisher, dlq_topic=target)
                return True
            except PublishError:
                logger.exception("dead_letter_publish_failed", position=message.position)
        except Exception:
            # Deliberately broad: anything not known to be permanent is retried, never dropped.
            logger.exception("message_processing_failed", position=message.position)

        attempt += 1
        delay = retry.delay(attempt)
        logger.warning(
            "message_retry_scheduled",
            position=message.position,
            attempt=attempt,
            delay_seconds=delay,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return False  # shutting down; leave the offset uncommitted
        except TimeoutError:
            continue


async def _dead_letter(
    message: ConsumedMessage,
    error: PermanentMessageError,
    *,
    publisher: MessagePublisher,
    dlq_topic: str,
) -> None:
    await publisher.publish(
        dlq_topic,
        key=message.key,
        value=message.value,
        headers={
            **message.headers,
            "dlq.reason": error.reason,
            "dlq.detail": error.detail[:_MAX_DLQ_DETAIL],
            "dlq.source": message.position,
        },
    )
    logger.warning(
        "message_dead_lettered",
        position=message.position,
        reason=error.reason,
        detail=error.detail[:_MAX_DLQ_DETAIL],
    )


RawHeaders = list[tuple[str, str | bytes | None]] | dict[str, str | bytes | None] | None


def decode_headers(raw: RawHeaders) -> dict[str, str]:
    pairs = raw.items() if isinstance(raw, dict) else (raw or [])
    decoded: dict[str, str] = {}
    for name, value in pairs:
        if isinstance(value, bytes):
            decoded[name] = value.decode("utf-8", errors="replace")
        elif value is not None:
            decoded[name] = value
    return decoded


class KafkaMessageSource:
    """A consumer-group member. All librdkafka calls run on one dedicated thread."""

    def __init__(self, settings: RuntimeSettings, *, group_id: str, topics: list[str]) -> None:
        self._consumer = Consumer(
            {
                **base_config(settings),
                "group.id": group_id,
                # Offsets are committed explicitly, only after processing (see module docs).
                "enable.auto.commit": False,
                # A brand-new group starts from the oldest retained message: nothing is skipped.
                "auto.offset.reset": "earliest",
            }
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kafka-consumer")
        self._consumer.subscribe(topics)

    async def _run(self, function: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, function)

    async def poll(self, timeout_seconds: float) -> ConsumedMessage | None:
        raw = await self._run(partial(self._consumer.poll, timeout_seconds))
        if raw is None:
            return None
        error = raw.error()
        if error is not None:
            if error.fatal():
                raise KafkaException(error)
            if error.code() != KafkaError._PARTITION_EOF:
                logger.warning("kafka_consume_error", code=error.name(), reason=error.str())
            return None
        return ConsumedMessage(
            topic=raw.topic() or "",
            partition=raw.partition() or 0,
            offset=raw.offset() or 0,
            key=_as_bytes(raw.key()),
            value=_as_bytes(raw.value()) or b"",
            headers=decode_headers(raw.headers()),
        )

    async def commit(self, message: ConsumedMessage) -> None:
        offsets = [TopicPartition(message.topic, message.partition, message.offset + 1)]
        try:
            await self._run(partial(self._consumer.commit, offsets=offsets, asynchronous=False))
        except KafkaException as exc:
            # Typically a rebalance moved the partition away. The message will be delivered
            # again to its new owner, and processing is idempotent, so this is safe.
            logger.warning(
                "offset_commit_failed", position=message.position, code=exc.args[0].name()
            )

    async def close(self) -> None:
        await self._run(self._consumer.close)
        self._executor.shutdown(wait=True)


def _as_bytes(value: str | bytes | None) -> bytes | None:
    if value is None or isinstance(value, bytes):
        return value
    return value.encode()
