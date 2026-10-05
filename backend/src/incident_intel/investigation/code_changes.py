"""Where an investigation gets "what did this deployment change?" from.

The investigation only knows this interface. The GitHub implementation lives in
``integrations.github.evidence``; tests use an in-memory one.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from incident_intel.incidents.queries import CandidateDeployment
from incident_intel.tenancy.context import TenantScope

KIND = "code_change"


@dataclass(frozen=True)
class CodeChangeEvidence:
    # (title, data) for each deployment whose changes were found.
    items: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Shown with the report: what could not be included, and why.
    notes: list[str] = field(default_factory=list)
    # Counts and outcomes for the step record. Never tokens or content.
    summary: dict[str, Any] = field(default_factory=dict)


class CodeChangeSource(Protocol):
    async def collect(
        self, scope: TenantScope, deployments: Sequence[CandidateDeployment]
    ) -> CodeChangeEvidence:
        """Code changes for an incident's candidate deployments. Expected failures (not
        connected, repository unreadable, provider down) are reported in ``notes`` and
        ``summary``, not raised."""
        ...

    async def close(self) -> None: ...
