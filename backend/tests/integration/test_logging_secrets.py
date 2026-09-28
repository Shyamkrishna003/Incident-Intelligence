import io
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from incident_intel.core.logging import build_formatter
from tests.conftest import TenantFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def log_output(app: FastAPI) -> Iterator[io.StringIO]:
    """Capture fully rendered log lines (after redaction) for the duration of a test."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_formatter(json=True))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)


async def test_api_keys_never_reach_the_logs(
    client: AsyncClient, make_tenant: TenantFactory, log_output: io.StringIO
) -> None:
    tenant = await make_tenant()
    key = tenant.plaintext_key
    forged = f"ii_{tenant.api_key.key_prefix}_" + "A" * 43

    await client.get("/v1/project", headers=tenant.auth_headers)
    await client.get("/v1/project", headers={"Authorization": f"Bearer {forged}"})
    await client.get(f"/v1/project?api_key={key}", headers=tenant.auth_headers)

    output = log_output.getvalue()
    lines = [json.loads(line) for line in output.splitlines()]
    events = [line["event"] for line in lines]
    assert events.count("http_request") == 3
    assert "api_key_rejected" in events
    assert key not in output
    assert forged not in output
    assert key.rsplit("_", 1)[-1] not in output
    assert all("authorization" not in {k.lower() for k in line} for line in lines)


async def test_request_logs_carry_request_and_tenant_context(
    client: AsyncClient, make_tenant: TenantFactory, log_output: io.StringIO
) -> None:
    tenant = await make_tenant()

    response = await client.get(
        "/v1/project", headers={**tenant.auth_headers, "X-Request-ID": "req-123"}
    )

    assert response.status_code == 200
    access = [
        json.loads(line)
        for line in log_output.getvalue().splitlines()
        if json.loads(line)["event"] == "http_request"
    ]
    assert access[-1]["request_id"] == "req-123"
    assert access[-1]["project_id"] == str(tenant.project.id)
    assert access[-1]["status"] == 200
