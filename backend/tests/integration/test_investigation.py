"""An investigation end to end against PostgreSQL, with a scripted model."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.audit.service import CLI_ACTOR
from incident_intel.core.errors import ConflictError
from incident_intel.investigation.llm import LLMError
from incident_intel.investigation.models import Investigation, InvestigationStep
from incident_intel.investigation.orchestrator import run_investigation
from incident_intel.investigation.prompts import (
    ANALYSIS_SYSTEM,
    PROMPT_VERSION,
    VERIFICATION_SYSTEM,
)
from incident_intel.investigation.service import (
    claim_next,
    get_investigation,
    request_investigation,
)
from incident_intel.tenancy.models import Membership
from incident_intel.tenancy.roles import Role
from tests.conftest import SignedInUser, UserFactory
from tests.integration.conftest import (
    HOLDS,
    HOSTILE_LOG,
    LEASE,
    Case,
    good_report,
)
from tests.support import FakeLLM

pytestmark = pytest.mark.integration


async def _run(
    case: Case,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    llm: FakeLLM,
) -> Investigation:
    queued = await request_investigation(
        db_session,
        case.scope,
        case.incident_id,
        user_id=case.user_id,
        actor=CLI_ACTOR,
        max_per_hour=50,
    )
    claimed = await claim_next(db_session, lease=LEASE, max_attempts=2)
    assert claimed is not None
    assert claimed.id == queued.id

    async def no_wait(_seconds: float) -> None:
        return None

    await run_investigation(session_factory, llm, queued.id, max_llm_attempts=3, sleep=no_wait)
    finished = await db_session.scalar(
        select(Investigation)
        .where(Investigation.id == queued.id)
        .execution_options(populate_existing=True)
    )
    assert finished is not None
    return finished


async def _steps(db_session: AsyncSession, investigation: Investigation) -> list[tuple[str, str]]:
    rows = await db_session.scalars(
        select(InvestigationStep)
        .where(InvestigationStep.investigation_id == investigation.id)
        .order_by(InvestigationStep.seq)
    )
    return [(step.kind, step.status) for step in rows]


# --- The run -----------------------------------------------------------------------------


async def test_a_successful_investigation_stores_a_checked_report_and_its_trail(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    llm = FakeLLM(replies=[good_report(), HOLDS])

    investigation = await _run(case, db_session, session_factory, llm)

    assert (investigation.status, investigation.error) == ("succeeded", None)
    assert (investigation.provider, investigation.model, investigation.prompt_version) == (
        "fake",
        "fake-model",
        PROMPT_VERSION,
    )
    assert (investigation.input_tokens, investigation.output_tokens) == (200, 40)
    assert investigation.report is not None
    assert investigation.report["hypotheses"][0]["assessment"] == "supported"
    assert investigation.report["hypotheses"][0]["review"] == "Nothing contradicts it."
    assert investigation.validation_notes == []
    assert await _steps(db_session, investigation) == [
        ("collect_evidence", "ok"),
        ("analyze", "ok"),
        ("validate", "ok"),
        ("verify", "ok"),
    ]
    assert [system for system, _ in llm.calls] == [ANALYSIS_SYSTEM, VERIFICATION_SYSTEM]


async def test_evidence_is_bounded_scrubbed_and_snapshotted(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    llm = FakeLLM(replies=[good_report(), HOLDS])

    investigation = await _run(case, db_session, session_factory, llm)

    detail = await get_investigation(db_session, case.scope, investigation.id)
    by_ref = {item.ref: item for item in detail.evidence}
    assert [item.kind for item in detail.evidence] == [
        "incident",
        "anomaly",
        "deployment",
        "deployment",
        "dependencies",
        "unaffected_services",
        "logs",
    ]
    assert by_ref["E2"].data["most_extreme_value"] == 900.0
    assert (by_ref["E3"].data["version"], by_ref["E3"].data["service_is_affected"]) == (
        "2.43.0",
        True,
    )
    assert by_ref["E3"].data["seconds_before_incident_start"] == 30
    # The unrelated deployment is included, marked as not affected.
    assert (by_ref["E4"].data["service"], by_ref["E4"].data["service_is_affected"]) == (
        "inventory-api",
        False,
    )
    assert "inventory-api" in by_ref["E6"].data["services"]

    patterns = by_ref["E7"].data["patterns"]
    pool = next(p for p in patterns if "connection pool exhausted" in p["example"])
    assert (pool["count_during_incident"], pool["count_before_incident"]) == (4, 0)  # grouped
    slow = next(p for p in patterns if "slow request" in p["example"])
    assert (slow["count_during_incident"], slow["count_before_incident"]) == (0, 1)
    assert all("routine info line" not in p["example"] for p in patterns)  # warn and above only

    # What the model was sent is exactly the stored snapshot, with the secret redacted.
    _, user_prompt = llm.calls[0]
    assert "hunter2" not in user_prompt
    assert "hunter2" not in json.dumps([item.data for item in detail.evidence])
    assert "[REDACTED]" in user_prompt


async def test_hostile_log_text_stays_inside_the_evidence_data(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    llm = FakeLLM(replies=[good_report(), HOLDS])

    await _run(case, db_session, session_factory, llm)

    system, user_prompt = llm.calls[0]
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in system
    assert user_prompt.count("</evidence>") == 1
    block = user_prompt.split("<evidence>\n", 1)[1].rsplit("\n</evidence>", 1)[0]
    examples = [
        p["example"]
        for item in json.loads(block)
        if item["kind"] == "logs"
        for p in item["patterns"]
    ]
    assert HOSTILE_LOG in examples  # delivered verbatim, as data


async def test_invented_citations_are_removed_before_the_report_is_stored(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    draft = good_report(
        observed_facts=[
            {"statement": "Latency rose.", "evidence": ["E2", "E99"]},
            {"statement": "The database was on fire.", "evidence": []},
        ],
        hypotheses=[
            {
                "statement": "Aliens.",
                "assessment": "supported",
                "supporting_evidence": ["E404"],
                "reasoning": "A log said so.",
            }
        ],
    )

    investigation = await _run(case, db_session, session_factory, FakeLLM(replies=[draft, HOLDS]))

    assert investigation.report is not None
    assert investigation.report["observed_facts"] == [
        {"statement": "Latency rose.", "evidence": ["E2"]}
    ]
    assert investigation.report["hypotheses"][0]["assessment"] == "untested"
    assert len(investigation.validation_notes) == 4


async def test_verification_can_overturn_the_leading_hypothesis(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    contradicted = json.dumps(
        {
            "reviews": [
                {
                    "hypothesis_index": 0,
                    "verdict": "contradicted",
                    "contradicting_evidence": ["E7"],
                    "explanation": "Slow requests were logged before the deployment.",
                }
            ]
        }
    )

    investigation = await _run(
        case, db_session, session_factory, FakeLLM(replies=[good_report(), contradicted])
    )

    assert investigation.report is not None
    hypothesis = investigation.report["hypotheses"][0]
    assert (hypothesis["assessment"], hypothesis["contradicting_evidence"]) == (
        "contradicted",
        ["E7"],
    )


async def test_a_malformed_reply_gets_one_repair_attempt(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    llm = FakeLLM(replies=['{"summary": ""}', "```json\n" + good_report() + "\n```", HOLDS])

    investigation = await _run(case, db_session, session_factory, llm)

    assert investigation.status == "succeeded"
    assert "did not match the required JSON shape" in llm.calls[1][1]


async def test_two_malformed_replies_fail_the_investigation_visibly(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    investigation = await _run(
        case, db_session, session_factory, FakeLLM(replies=["not json", "{}"])
    )

    assert investigation.status == "failed"
    assert investigation.report is None
    assert investigation.error is not None
    assert "required report format" in investigation.error
    assert await _steps(db_session, investigation) == [
        ("collect_evidence", "ok"),
        ("analyze", "failed"),
    ]
    # The evidence that was collected is still there to look at.
    detail = await get_investigation(db_session, case.scope, investigation.id)
    assert len(detail.evidence) == 7


async def test_rate_limits_are_retried_and_then_reported(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    busy = LLMError("Gemini answered 429", transient=True, retry_after_seconds=1)
    recovered = await _run(
        case, db_session, session_factory, FakeLLM(replies=[busy, good_report(), HOLDS])
    )
    assert recovered.status == "succeeded"


async def test_persistent_provider_failure_fails_the_run(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    busy = LLMError("Gemini answered 429: quota", transient=True)
    investigation = await _run(
        case, db_session, session_factory, FakeLLM(replies=[busy, busy, busy])
    )

    assert (investigation.status, investigation.error) == ("failed", "Gemini answered 429: quota")


async def test_a_failed_verification_does_not_discard_the_report(
    case: Case, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    down = LLMError("Gemini answered 400", transient=False)

    investigation = await _run(
        case, db_session, session_factory, FakeLLM(replies=[good_report(), down])
    )

    assert investigation.status == "succeeded"
    assert investigation.report is not None
    assert any("verification step could not run" in note for note in investigation.validation_notes)


# --- Queue -------------------------------------------------------------------------------


async def test_only_one_investigation_per_incident_runs_at_a_time(
    case: Case, db_session: AsyncSession
) -> None:
    await request_investigation(
        db_session,
        case.scope,
        case.incident_id,
        user_id=case.user_id,
        actor=CLI_ACTOR,
        max_per_hour=50,
    )

    with pytest.raises(ConflictError):
        await request_investigation(
            db_session,
            case.scope,
            case.incident_id,
            user_id=case.user_id,
            actor=CLI_ACTOR,
            max_per_hour=50,
        )


async def test_an_abandoned_run_is_retried_then_failed(
    case: Case, db_session: AsyncSession
) -> None:
    queued = await request_investigation(
        db_session,
        case.scope,
        case.incident_id,
        user_id=case.user_id,
        actor=CLI_ACTOR,
        max_per_hour=50,
    )
    now = datetime.now(UTC)

    first = await claim_next(db_session, lease=LEASE, max_attempts=2, now=now)
    assert first is not None
    assert (first.status, first.attempts) == ("running", 1)
    # While the lease is valid nobody else takes it.
    assert (
        await claim_next(db_session, lease=LEASE, max_attempts=2, now=now + timedelta(minutes=5))
        is None
    )
    # The worker died: after the lease expires it is picked up again...
    second = await claim_next(
        db_session, lease=LEASE, max_attempts=2, now=now + timedelta(minutes=11)
    )
    assert second is not None
    assert second.attempts == 2
    # ...and after its last attempt it is failed, not retried forever.
    assert (
        await claim_next(db_session, lease=LEASE, max_attempts=2, now=now + timedelta(minutes=30))
        is None
    )
    await db_session.refresh(queued)
    assert queued.status == "failed"
    assert queued.error is not None
    assert "interrupted" in queued.error


# --- API ---------------------------------------------------------------------------------


async def _join(
    client: AsyncClient, db_session: AsyncSession, case: Case, user: SignedInUser, role: Role
) -> None:
    me = await client.get("/v1/me", headers=user.headers)
    db_session.add(
        Membership(
            organization_id=case.organization_id,
            user_id=uuid.UUID(me.json()["user"]["id"]),
            role=role.value,
        )
    )
    await db_session.commit()


async def test_api_request_read_and_permissions(
    case: Case,
    client: AsyncClient,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: UserFactory,
) -> None:
    member, viewer, outsider = make_user(), make_user(), make_user()
    await _join(client, db_session, case, member, Role.MEMBER)
    await _join(client, db_session, case, viewer, Role.VIEWER)
    base = f"/v1/projects/{case.project_id}"
    url = f"{base}/incidents/{case.incident_id}/investigations"

    assert (await client.post(url, headers=viewer.headers)).status_code == 403
    assert (await client.post(url, headers=outsider.headers)).status_code == 404
    requested = await client.post(url, headers=member.headers)
    assert requested.status_code == 202
    assert requested.json()["status"] == "queued"
    assert (await client.post(url, headers=member.headers)).status_code == 409

    claimed = await claim_next(db_session, lease=LEASE, max_attempts=2)
    assert claimed is not None
    await run_investigation(session_factory, FakeLLM(replies=[good_report(), HOLDS]), claimed.id)

    # This test shares one session between the worker and the API; a real request gets a
    # fresh session. Drop what this one has cached so the API reads current rows.
    db_session.expire_all()
    listed = await client.get(url, headers=viewer.headers)
    detail = await client.get(
        f"{base}/investigations/{requested.json()['id']}", headers=viewer.headers
    )
    hidden = await client.get(
        f"{base}/investigations/{requested.json()['id']}", headers=outsider.headers
    )

    assert [i["status"] for i in listed.json()["investigations"]] == ["succeeded"]
    body = detail.json()
    assert body["model"] == "fake-model"
    assert body["report"]["hypotheses"][0]["supporting_evidence"] == ["E2", "E3"]
    assert {e["ref"] for e in body["evidence"]} >= {"E2", "E3"}
    assert [s["kind"] for s in body["steps"]] == [
        "collect_evidence",
        "analyze",
        "validate",
        "verify",
    ]
    assert hidden.status_code == 404


async def test_investigations_per_incident_are_capped(
    case: Case, client: AsyncClient, db_session: AsyncSession, app: Any, make_user: UserFactory
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"investigations_per_incident_per_hour": 1}
    )
    member = make_user()
    await _join(client, db_session, case, member, Role.MEMBER)
    url = f"/v1/projects/{case.project_id}/incidents/{case.incident_id}/investigations"
    assert (await client.post(url, headers=member.headers)).status_code == 202
    claimed = await claim_next(db_session, lease=LEASE, max_attempts=2)
    assert claimed is not None
    claimed.status = "failed"
    await db_session.commit()

    response = await client.post(url, headers=member.headers)

    assert response.status_code == 429
