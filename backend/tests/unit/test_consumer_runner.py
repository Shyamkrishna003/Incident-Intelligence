"""Delivery semantics of run_consumer, using an in-memory message source."""

import asyncio
from collections import deque

import pytest

from incident_intel.streaming.consumer import (
    ConsumedMessage,
    PermanentMessageError,
    RetryPolicy,
    run_consumer,
)
from tests.support import FakePublisher

FAST_RETRY = RetryPolicy(initial_delay_seconds=0.001, max_delay_seconds=0.005)
DLQ = "metrics.dlq"


def _message(offset: int, value: bytes = b"{}") -> ConsumedMessage:
    return ConsumedMessage(
        topic="metrics",
        partition=0,
        offset=offset,
        key=b"project",
        value=value,
        headers={"request_id": "r1"},
    )


class FakeSource:
    """Yields queued messages, then sets ``stop`` once drained."""

    def __init__(self, messages: list[ConsumedMessage], stop: asyncio.Event) -> None:
        self._queue = deque(messages)
        self._stop = stop
        self.committed: list[int] = []
        self.closed = False

    async def poll(self, timeout_seconds: float) -> ConsumedMessage | None:
        if not self._queue:
            self._stop.set()
            return None
        return self._queue.popleft()

    async def commit(self, message: ConsumedMessage) -> None:
        self.committed.append(message.offset)

    async def close(self) -> None:
        self.closed = True


async def _run(
    messages: list[ConsumedMessage],
    handler: object,
    publisher: FakePublisher | None = None,
) -> tuple[FakeSource, FakePublisher]:
    stop = asyncio.Event()
    source = FakeSource(messages, stop)
    publisher = publisher or FakePublisher()
    await asyncio.wait_for(
        run_consumer(
            source=source,
            handler=handler,  # type: ignore[arg-type]
            publisher=publisher,
            dlq_topic=DLQ,
            stop=stop,
            retry=FAST_RETRY,
            poll_timeout_seconds=0.01,
        ),
        timeout=5,
    )
    return source, publisher


async def test_commits_each_message_after_successful_handling() -> None:
    handled: list[int] = []

    async def handler(message: ConsumedMessage) -> None:
        handled.append(message.offset)

    source, publisher = await _run([_message(0), _message(1)], handler)

    assert handled == [0, 1]
    assert source.committed == [0, 1]
    assert publisher.messages == []


async def test_permanent_failure_is_dead_lettered_then_committed() -> None:
    async def handler(message: ConsumedMessage) -> None:
        if message.offset == 0:
            raise PermanentMessageError("invalid_message", "points.0.value: finite_number")

    source, publisher = await _run([_message(0, b"garbage"), _message(1)], handler)

    assert source.committed == [0, 1]  # processing continued past the bad message
    [dead] = publisher.messages
    assert dead.topic == DLQ
    assert dead.value == b"garbage"
    assert dead.key == b"project"
    assert dead.headers == {
        "request_id": "r1",
        "dlq.reason": "invalid_message",
        "dlq.detail": "points.0.value: finite_number",
        "dlq.source": "metrics/0@0",
    }


async def test_transient_failure_retries_the_same_message_until_it_succeeds() -> None:
    attempts: list[int] = []

    async def handler(message: ConsumedMessage) -> None:
        attempts.append(message.offset)
        if len(attempts) < 3:
            raise ConnectionError("database unavailable")

    source, publisher = await _run([_message(0), _message(1)], handler)

    assert attempts == [0, 0, 0, 1]  # message 1 waited until message 0 succeeded
    assert source.committed == [0, 1]
    assert publisher.messages == []


async def test_stopping_during_retries_leaves_the_message_uncommitted() -> None:
    stop = asyncio.Event()
    source = FakeSource([_message(0)], stop)

    async def handler(message: ConsumedMessage) -> None:
        stop.set()  # shutdown requested while the database is down
        raise ConnectionError("database unavailable")

    await asyncio.wait_for(
        run_consumer(
            source=source,
            handler=handler,
            publisher=FakePublisher(),
            dlq_topic=DLQ,
            stop=stop,
            retry=RetryPolicy(initial_delay_seconds=10),
        ),
        timeout=5,
    )

    assert source.committed == []  # it will be redelivered after restart


async def test_dead_letter_publish_failure_is_retried_not_skipped() -> None:
    publisher = FakePublisher(fail_publish=True)
    calls = 0

    async def handler(message: ConsumedMessage) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            publisher.fail_publish = False  # the DLQ becomes reachable again
        raise PermanentMessageError("invalid_message")

    source, _ = await _run([_message(0)], handler, publisher)

    assert calls == 3
    assert source.committed == [0]
    assert [m.topic for m in publisher.messages] == [DLQ]


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 0.5), (2, 1.0), (3, 2.0), (10, 30.0)],
)
def test_retry_backoff_is_exponential_and_capped(attempt: int, expected: float) -> None:
    assert RetryPolicy().delay(attempt) == expected
