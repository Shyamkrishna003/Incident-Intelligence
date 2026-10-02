"""Backfill and live modes for the simulator."""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from incident_intel.simulator.client import IngestClient, SendSummary
from incident_intel.simulator.scenario import align, generate

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SimulationPlan:
    backfill: timedelta
    step: timedelta
    # How long after the start of the backfill the incident begins; None = no incident.
    incident_after: timedelta | None
    seed: int = 1
    live: bool = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_simulation(
    client: IngestClient,
    plan: SimulationPlan,
    *,
    stop: asyncio.Event,
    now: Callable[[], datetime] = _utcnow,
) -> SendSummary:
    """Send the backfill period, then (in live mode) one step at a time until stopped."""
    end = align(now(), plan.step)
    start = end - plan.backfill
    incident_at = start + plan.incident_after if plan.incident_after is not None else None
    logger.info(
        "simulation_started",
        start=start.isoformat(),
        incident_at=incident_at.isoformat() if incident_at else None,
        live=plan.live,
    )

    summary = await client.send(
        generate(start=start, end=end, step=plan.step, incident_at=incident_at, seed=plan.seed)
    )
    sent_until = end
    while plan.live and not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=plan.step.total_seconds())
        upto = align(now(), plan.step)
        if upto > sent_until:
            summary.add(
                await client.send(
                    generate(
                        start=sent_until,
                        end=upto,
                        step=plan.step,
                        incident_at=incident_at,
                        seed=plan.seed,
                    )
                )
            )
            sent_until = upto
    return summary
