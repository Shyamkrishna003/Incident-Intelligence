"""KafkaPublisher behavior when no broker is reachable (real librdkafka, no Kafka needed)."""

import time
from collections.abc import AsyncIterator

import pytest

from incident_intel.streaming.producer import KafkaPublisher, PublishError
from tests.support import make_settings

TIMEOUT = 1.0


@pytest.fixture
async def offline_publisher() -> AsyncIterator[KafkaPublisher]:
    settings = make_settings(
        "postgresql+asyncpg://u:p@127.0.0.1:1/x_test", kafka_bootstrap_servers="127.0.0.1:1"
    )
    publisher = KafkaPublisher(settings, delivery_timeout_seconds=TIMEOUT)
    yield publisher
    await publisher.close()


async def test_publish_fails_within_the_delivery_timeout(offline_publisher: KafkaPublisher) -> None:
    started = time.monotonic()

    with pytest.raises(PublishError):
        await offline_publisher.publish("any-topic", key=b"k", value=b"v")

    # Bounded: the API turns this into a 503 instead of hanging the request.
    assert time.monotonic() - started < TIMEOUT + 2


async def test_topic_check_fails_when_broker_is_unreachable(
    offline_publisher: KafkaPublisher,
) -> None:
    with pytest.raises(PublishError):
        await offline_publisher.missing_topics(["any-topic"], timeout_seconds=TIMEOUT)
