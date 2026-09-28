import io
import json
from typing import Any

import structlog

from incident_intel.core.logging import REDACTED, configure_logging, redact_sensitive
from incident_intel.tenancy.api_keys import generate_api_key


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    return dict(redact_sensitive(None, "info", event))


def test_masks_sensitive_keys_case_insensitively() -> None:
    result = _redact(
        {"event": "x", "Authorization": "Bearer abc", "password": "hunter2", "API_KEY": "k"}
    )

    assert result["Authorization"] == REDACTED
    assert result["password"] == REDACTED
    assert result["API_KEY"] == REDACTED
    assert result["event"] == "x"


def test_masks_nested_structures() -> None:
    result = _redact({"event": "x", "request": {"headers": [{"cookie": "session=1"}]}})

    assert result["request"]["headers"][0]["cookie"] == REDACTED


def test_scrubs_api_keys_inside_strings() -> None:
    key = generate_api_key("p" * 40).plaintext

    result = _redact({"event": f"failed with {key}", "path": f"/v1/project?k={key}"})

    assert key not in json.dumps(result)
    assert REDACTED in result["event"]


def test_scrubs_bearer_tokens_inside_strings() -> None:
    result = _redact({"event": "header was Bearer eyJhbGciOi.payload.sig"})

    assert "eyJhbGciOi" not in result["event"]


def test_leaves_ordinary_values_untouched() -> None:
    event = {"event": "http_request", "status": 200, "path": "/healthz", "duration_ms": 1.5}

    assert _redact(event) == event


def test_rendered_output_contains_no_secrets() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", json=True, stream=stream)
    key = generate_api_key("p" * 40).plaintext

    structlog.get_logger("redaction-test").info(
        "auth attempt", presented=key, authorization=f"Bearer {key}"
    )

    output = stream.getvalue()
    assert key not in output
    line = json.loads(output.strip().splitlines()[-1])
    assert line["event"] == "auth attempt"
    assert line["authorization"] == REDACTED
