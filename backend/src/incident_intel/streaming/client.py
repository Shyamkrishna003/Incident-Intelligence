"""Shared librdkafka client configuration and logging hooks."""

import logging
from typing import Any

import structlog
from confluent_kafka import KafkaError

from incident_intel.core.config import RuntimeSettings

# librdkafka's own log lines, routed through our JSON formatter and redaction.
librdkafka_logger = logging.getLogger("incident_intel.kafka.librdkafka")
logger = structlog.get_logger("incident_intel.kafka")


def on_client_error(error: KafkaError) -> None:
    """Called by librdkafka for client-level errors (for example: broker unreachable)."""
    log = logger.error if error.fatal() else logger.warning
    log("kafka_client_error", code=error.name(), reason=error.str(), fatal=error.fatal())


def base_config(settings: RuntimeSettings) -> dict[str, Any]:
    return {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "client.id": settings.kafka_client_id,
        "logger": librdkafka_logger,
        "error_cb": on_client_error,
    }
