"""One investigation, as a fixed sequence of steps.

    1. collect_evidence   code only; snapshots what the model will see
    2. analyze            model call -> draft report (one repair attempt if malformed)
    3. validate           code only; checks every citation
    4. verify             model call -> tries to disprove each hypothesis
    5. finish             the checked report is stored

There is no loop the model controls and no tool it can call. Each step commits, so
progress and failures are visible while the run is in flight, and each is recorded.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.errors import NotFoundError
from incident_intel.investigation.evidence import EvidenceItem, collect_evidence
from incident_intel.investigation.llm import LLMError, LLMProvider, LLMResponse
from incident_intel.investigation.models import (
    Investigation,
    InvestigationEvidence,
    InvestigationStep,
)
from incident_intel.investigation.prompts import (
    ANALYSIS_SYSTEM,
    PROMPT_VERSION,
    VERIFICATION_SYSTEM,
    analysis_prompt,
    repair_prompt,
    verification_prompt,
)
from incident_intel.investigation.schemas import RawReport, RawVerification, Report
from incident_intel.investigation.validation import (
    apply_verification,
    order_hypotheses,
    validate_report,
)
from incident_intel.tenancy.context import TenantScope

logger = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class InvestigationFailed(Exception):
    """The run cannot produce a report. The message is shown to the user."""


def _problems(exc: ValidationError) -> list[str]:
    # Locations and error types only, never the offending values.
    return [
        f"{'.'.join(str(p) for p in err['loc']) or 'reply'}: {err['msg']}" for err in exc.errors()
    ]


def _parse[ModelT: BaseModel](model: type[ModelT], text: str) -> ModelT:
    try:
        return model.model_validate_json(text)
    except ValidationError:
        # Some models wrap JSON in a Markdown fence despite being told not to.
        stripped = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        return model.model_validate_json(stripped.strip())


class ReportWriter:
    """The model-facing part of an investigation: draft a report from evidence, and review
    its hypotheses. Needs no database, so the evaluation suite uses it directly."""

    def __init__(self, provider: LLMProvider, *, max_llm_attempts: int, sleep: Sleep) -> None:
        self._provider = provider
        self._max_llm_attempts = max_llm_attempts
        self._sleep = sleep
        self.input_tokens = 0
        self.output_tokens = 0

    async def call_model(self, summary: dict[str, Any], *, system: str, user: str) -> LLMResponse:
        """Call the model, waiting and retrying while the provider is rate limited or
        temporarily failing."""
        for attempt in range(1, self._max_llm_attempts + 1):
            try:
                response = await self._provider.generate_json(system=system, user=user)
            except LLMError as exc:
                summary["model_calls"] = summary.get("model_calls", 0) + 1
                if not exc.transient or attempt == self._max_llm_attempts:
                    raise InvestigationFailed(str(exc)) from exc
                delay = min(exc.retry_after_seconds or 2.0**attempt, 60.0)
                summary["retries"] = summary.get("retries", 0) + 1
                logger.info("llm_retry", attempt=attempt, delay_seconds=delay, reason=str(exc))
                await self._sleep(delay)
                continue
            summary["model_calls"] = summary.get("model_calls", 0) + 1
            self.input_tokens += response.input_tokens or 0
            self.output_tokens += response.output_tokens or 0
            return response
        raise InvestigationFailed("The model could not be reached.")  # pragma: no cover

    async def analyze(self, summary: dict[str, Any], evidence: list[dict[str, Any]]) -> RawReport:
        response = await self.call_model(
            summary, system=ANALYSIS_SYSTEM, user=analysis_prompt(evidence)
        )
        try:
            return _parse(RawReport, response.text)
        except ValidationError as first:
            summary["repaired"] = True
            repaired = await self.call_model(
                summary,
                system=ANALYSIS_SYSTEM,
                user=repair_prompt(evidence, _problems(first)),
            )
            try:
                return _parse(RawReport, repaired.text)
            except ValidationError as second:
                raise InvestigationFailed(
                    "The model's reply did not match the required report format, even "
                    "after one correction."
                ) from second

    async def verify(
        self, summary: dict[str, Any], evidence: list[dict[str, Any]], report: Report
    ) -> RawVerification | None:
        if not report.hypotheses:
            summary["skipped"] = "no hypotheses"
            return None
        hypotheses = [
            {
                "index": index,
                "statement": hypothesis.statement,
                "supporting_evidence": hypothesis.supporting_evidence,
            }
            for index, hypothesis in enumerate(report.hypotheses)
        ]
        response = await self.call_model(
            summary, system=VERIFICATION_SYSTEM, user=verification_prompt(evidence, hypotheses)
        )
        try:
            verification = _parse(RawVerification, response.text)
        except (ValidationError, json.JSONDecodeError):
            summary["skipped"] = "malformed reply"
            return None
        summary["verdicts"] = [review.verdict for review in verification.reviews]
        return verification


async def write_checked_report(
    writer: ReportWriter, evidence: list[dict[str, Any]]
) -> tuple[Report, list[str]]:
    """Analyze, check citations, verify. The same sequence an investigation runs, without
    the database or step records. Raises InvestigationFailed."""
    valid_refs = {str(item["ref"]) for item in evidence}
    raw = await writer.analyze({}, evidence)
    report, notes = validate_report(raw, valid_refs)
    try:
        verification = await writer.verify({}, evidence, report)
    except InvestigationFailed as exc:
        return order_hypotheses(report), [*notes, f"The verification step could not run ({exc})."]
    if verification is None:
        return order_hypotheses(report), notes
    verified, changes = apply_verification(report, verification, valid_refs)
    return verified, [*notes, *changes]


class _Run(ReportWriter):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: LLMProvider,
        investigation: Investigation,
        *,
        max_llm_attempts: int,
        sleep: Sleep,
    ) -> None:
        super().__init__(provider, max_llm_attempts=max_llm_attempts, sleep=sleep)
        self._session_factory = session_factory
        self._id = investigation.id
        self._project_id = investigation.project_id
        self._attempt = investigation.attempts
        self._scope = TenantScope(investigation.organization_id, investigation.project_id)
        self._incident_id = investigation.incident_id
        self._seq = 0

    async def step[T](self, kind: str, action: Callable[[dict[str, Any]], Awaitable[T]]) -> T:
        """Run one step and record it, whether it succeeds or fails."""
        self._seq += 1
        started_at, clock = datetime.now(UTC), time.perf_counter()
        summary: dict[str, Any] = {}
        status, error = "ok", None
        try:
            return await action(summary)
        except Exception as exc:
            status, error = "failed", str(exc)[:500]
            raise
        finally:
            async with self._session_factory() as session:
                session.add(
                    InvestigationStep(
                        investigation_id=self._id,
                        project_id=self._project_id,
                        attempt=self._attempt,
                        seq=self._seq,
                        kind=kind,
                        status=status,
                        started_at=started_at,
                        duration_ms=round((time.perf_counter() - clock) * 1000),
                        summary=summary,
                        error=error,
                    )
                )
                await session.commit()

    async def collect(self, summary: dict[str, Any]) -> list[EvidenceItem]:
        async with self._session_factory() as session:
            try:
                detail, items = await collect_evidence(session, self._scope, self._incident_id)
            except NotFoundError as exc:
                raise InvestigationFailed("The incident no longer exists.") from exc
            if not detail.anomalies:
                raise InvestigationFailed("This incident has no anomalies to investigate.")
            # A retried run replaces the evidence of the earlier attempt.
            await session.execute(
                delete(InvestigationEvidence).where(
                    InvestigationEvidence.investigation_id == self._id
                )
            )
            session.add_all(
                InvestigationEvidence(
                    investigation_id=self._id,
                    ref=item.ref,
                    project_id=self._project_id,
                    position=position,
                    kind=item.kind,
                    title=item.title,
                    data=item.data,
                )
                for position, item in enumerate(items)
            )
            await session.commit()
        summary["evidence_items"] = len(items)
        summary["by_kind"] = {
            kind: sum(item.kind == kind for item in items) for kind in {i.kind for i in items}
        }
        return items


async def _pipeline(run: _Run, notes: list[str]) -> Report:
    """Steps 1-4. Appends to ``notes`` whatever the checks changed."""
    items = await run.step("collect_evidence", run.collect)
    evidence = [item.for_model() for item in items]
    valid_refs = {item.ref for item in items}

    raw = await run.step("analyze", lambda summary: run.analyze(summary, evidence))

    async def validate(summary: dict[str, Any]) -> Report:
        checked, changes = validate_report(raw, valid_refs)
        notes.extend(changes)
        summary.update(
            facts=len(checked.observed_facts),
            hypotheses=len(checked.hypotheses),
            changes=len(changes),
        )
        return checked

    report = await run.step("validate", validate)

    try:
        verification = await run.step(
            "verify", lambda summary: run.verify(summary, evidence, report)
        )
    except InvestigationFailed as exc:
        # The report stands without the second opinion; say so instead of failing.
        notes.append(f"The verification step could not run ({exc}).")
        return order_hypotheses(report)
    if verification is None:
        return order_hypotheses(report)
    verified, changes = apply_verification(report, verification, valid_refs)
    notes.extend(changes)
    return verified


async def run_investigation(
    session_factory: async_sessionmaker[AsyncSession],
    provider: LLMProvider,
    investigation_id: uuid.UUID,
    *,
    max_llm_attempts: int = 4,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Run a claimed investigation to completion: it ends "succeeded" or "failed"."""
    async with session_factory() as session:
        investigation = await session.get(Investigation, investigation_id)
        if investigation is None or investigation.status != "running":
            return
        investigation.provider = provider.name
        investigation.model = provider.model
        investigation.prompt_version = PROMPT_VERSION
        await session.commit()
        run = _Run(
            session_factory, provider, investigation, max_llm_attempts=max_llm_attempts, sleep=sleep
        )

    report: Report | None = None
    notes: list[str] = []
    error: str | None = None
    try:
        report = await _pipeline(run, notes)
    except InvestigationFailed as exc:
        error = str(exc)[:500]
    except Exception as exc:
        logger.exception("investigation_crashed", investigation_id=str(investigation_id))
        error = f"Unexpected error ({type(exc).__name__})."

    async with session_factory() as session:
        investigation = await session.get(Investigation, investigation_id)
        if investigation is None:
            return
        investigation.status = "failed" if error else "succeeded"
        investigation.error = error
        investigation.report = report.model_dump(mode="json") if report and not error else None
        investigation.validation_notes = notes
        investigation.input_tokens = run.input_tokens
        investigation.output_tokens = run.output_tokens
        investigation.finished_at = datetime.now(UTC)
        investigation.locked_until = None
        await session.commit()
    logger.info(
        "investigation_finished",
        investigation_id=str(investigation_id),
        status="failed" if error else "succeeded",
        error=error,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
    )
