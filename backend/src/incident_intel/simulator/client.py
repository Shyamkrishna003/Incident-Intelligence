"""Sends generated telemetry through the ingestion API, the same way a real service would."""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from incident_intel.simulator.scenario import Telemetry

logger = structlog.get_logger(__name__)

# (endpoint path, body field, items per request). Limits match the API's batch limits.
_ENDPOINTS = (
    ("/v1/ingest/deployments", "deployments", 100),
    ("/v1/ingest/metrics", "points", 1000),
    ("/v1/ingest/logs", "records", 1000),
)
_MAX_ATTEMPTS = 5


class SimulatorError(Exception):
    """The API rejected the simulator's data, or stayed unavailable."""


@dataclass
class SendSummary:
    points: int = 0
    records: int = 0
    deployments: int = 0
    requests: int = 0

    def add(self, other: "SendSummary") -> None:
        self.points += other.points
        self.records += other.records
        self.deployments += other.deployments
        self.requests += other.requests


def _idempotency_key(path: str, items: Sequence[dict[str, Any]]) -> str:
    """Derived from the content: sending the same data again is recognized as a retry."""
    digest = hashlib.sha256(json.dumps([path, items], sort_keys=True).encode()).hexdigest()
    return f"sim-{digest[:40]}"


class IngestClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def declare_dependencies(self, edges: Sequence[tuple[str, str]]) -> None:
        """Tell the platform which simulated services call which."""
        body = {"dependencies": [{"service": a, "depends_on": b} for a, b in edges]}
        try:
            response = await self._http.put("/v1/dependencies", json=body)
        except httpx.TransportError as exc:
            raise SimulatorError(f"could not reach the API: {exc}") from exc
        if response.status_code != 200:
            raise SimulatorError(
                f"/v1/dependencies answered {response.status_code}: {response.text[:300]}"
            )

    async def send(self, telemetry: Telemetry) -> SendSummary:
        summary = SendSummary()
        for path, field_name, batch_size in _ENDPOINTS:
            items: list[dict[str, Any]] = getattr(telemetry, field_name)
            for offset in range(0, len(items), batch_size):
                batch = items[offset : offset + batch_size]
                await self._post(path, field_name, batch)
                summary.requests += 1
                setattr(summary, field_name, getattr(summary, field_name) + len(batch))
        return summary

    async def _post(self, path: str, field_name: str, items: list[dict[str, Any]]) -> None:
        headers = {"Idempotency-Key": _idempotency_key(path, items)}
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._http.post(path, json={field_name: items}, headers=headers)
            except httpx.TransportError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise SimulatorError(f"could not reach the API: {exc}") from exc
                await asyncio.sleep(min(2**attempt, 10))
                continue
            if response.status_code == 202:
                return
            if response.status_code in {429, 503} and attempt < _MAX_ATTEMPTS:
                # Rate limited or temporarily unavailable: wait as asked, then retry with
                # the same Idempotency-Key.
                delay = float(response.headers.get("Retry-After", "2"))
                logger.info("simulator_waiting", status=response.status_code, seconds=delay)
                await asyncio.sleep(min(delay, 60))
                continue
            raise SimulatorError(f"{path} answered {response.status_code}: {response.text[:300]}")


def build_http_client(api_url: str, api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=api_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(15.0),
    )
