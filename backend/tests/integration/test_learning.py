"""Feedback, learning records and evaluation cases."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.audit.models import AuditLog
from incident_intel.audit.service import CLI_ACTOR
from incident_intel.investigation.llm import LLMError
from incident_intel.investigation.orchestrator import run_investigation
from incident_intel.investigation.service import claim_next, request_investigation
from incident_intel.learning.models import LearningRecord
from incident_intel.learning.service import project_eval_cases
from incident_intel.tenancy.models import Membership, Organization, Project
from incident_intel.tenancy.roles import Role
from tests.conftest import SignedInUser, UserFactory
from tests.integration.conftest import HOLDS, LEASE, Case, good_report
from tests.support import FakeLLM

pytestmark = pytest.mark.integration


async def _investigate(
    case: Case,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    succeed: bool = True,
) -> uuid.UUID:
    queued = await request_investigation(
        db_session,
        case.scope,
        case.incident_id,
        user_id=case.user_id,
        actor=CLI_ACTOR,
        max_per_hour=50,
    )
    investigation_id = queued.id
    claimed = await claim_next(db_session, lease=LEASE, max_attempts=2)
    assert claimed is not None
    replies: list[str | LLMError] = [good_report(), HOLDS] if succeed else ["not json", "{}"]
    await run_investigation(session_factory, FakeLLM(replies=replies), investigation_id)
    db_session.expire_all()
    return investigation_id


async def _join(
    client: AsyncClient, db_session: AsyncSession, case: Case, user: SignedInUser, role: Role
) -> str:
    me = await client.get("/v1/me", headers=user.headers)
    user_id = me.json()["user"]["id"]
    db_session.add(
        Membership(
            organization_id=case.organization_id, user_id=uuid.UUID(user_id), role=role.value
        )
    )
    await db_session.commit()
    return str(user_id)


async def test_feedback_is_saved_revised_and_shown_with_the_report(
    case: Case,
    client: AsyncClient,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: UserFactory,
) -> None:
    investigation_id = await _investigate(case, db_session, session_factory)
    alice, bob = make_user(), make_user()
    alice_id = await _join(client, db_session, case, alice, Role.MEMBER)
    await _join(client, db_session, case, bob, Role.MEMBER)
    base = f"/v1/projects/{case.project_id}/investigations/{investigation_id}"

    first = await client.put(
        f"{base}/feedback", json={"verdict": "incorrect"}, headers=alice.headers
    )
    revised = await client.put(
        f"{base}/feedback",
        json={
            "verdict": "partially_correct",
            "actual_cause": "A missing index on customer_ref.",
            "notes": "Right service.",
        },
        headers=alice.headers,
    )
    await client.put(f"{base}/feedback", json={"verdict": "correct"}, headers=bob.headers)
    as_alice = (await client.get(base, headers=alice.headers)).json()["feedback"]

    assert (first.status_code, revised.status_code) == (200, 200)
    assert revised.json()["mine"] is True
    # One feedback per person: Alice's was revised, not duplicated.
    assert sorted((f["verdict"], f["mine"]) for f in as_alice) == [
        ("correct", False),
        ("partially_correct", True),
    ]
    mine = next(f for f in as_alice if f["mine"])
    assert mine["actual_cause"] == "A missing index on customer_ref."
    audit = await db_session.scalars(
        select(AuditLog).where(
            AuditLog.action == "investigation.feedback", AuditLog.actor_id == alice_id
        )
    )
    assert len(audit.all()) == 2


async def test_feedback_creates_a_self_contained_learning_record(
    case: Case,
    client: AsyncClient,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: UserFactory,
) -> None:
    investigation_id = await _investigate(case, db_session, session_factory)
    member = make_user()
    await _join(client, db_session, case, member, Role.MEMBER)
    base = f"/v1/projects/{case.project_id}"

    await client.put(
        f"{base}/investigations/{investigation_id}/feedback",
        json={"verdict": "correct", "actual_cause": "Version 2.43.0."},
        headers=member.headers,
    )
    await client.put(
        f"{base}/investigations/{investigation_id}/feedback",
        json={"verdict": "incorrect", "actual_cause": "Actually a disk problem."},
        headers=member.headers,
    )

    records = (
        await db_session.scalars(
            select(LearningRecord).where(LearningRecord.project_id == case.project_id)
        )
    ).all()
    [record] = records  # one per investigation, updated in place
    assert (record.verdict, record.confirmed_cause) == ("incorrect", "Actually a disk problem.")
    snapshot = record.snapshot
    assert snapshot["investigation"]["model"] == "fake-model"
    assert snapshot["report"]["hypotheses"][0]["statement"].startswith("Version 2.43.0")
    assert [item["ref"] for item in snapshot["evidence"]][:3] == ["E1", "E2", "E3"]
    assert snapshot["feedback"][0]["verdict"] == "incorrect"
    assert snapshot["incident"]["title"] == "Anomalies in payment-api"

    overview = (await client.get(f"{base}/learning-records", headers=member.headers)).json()
    assert overview["counts"] == {"correct": 0, "partially_correct": 0, "incorrect": 1}
    assert overview["records"][0]["incident_title"] == "Anomalies in payment-api"
    assert overview["records"][0]["confirmed_cause"] == "Actually a disk problem."


async def test_feedback_rules_and_permissions(
    case: Case,
    client: AsyncClient,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: UserFactory,
) -> None:
    failed_id = await _investigate(case, db_session, session_factory, succeed=False)
    member, viewer, outsider = make_user(), make_user(), make_user()
    await _join(client, db_session, case, member, Role.MEMBER)
    await _join(client, db_session, case, viewer, Role.VIEWER)
    base = f"/v1/projects/{case.project_id}"
    url = f"{base}/investigations/{failed_id}/feedback"
    body = {"verdict": "correct"}

    assert (
        await client.put(url, json=body, headers=member.headers)
    ).status_code == 409  # no report
    assert (await client.put(url, json=body, headers=viewer.headers)).status_code == 403
    assert (await client.put(url, json=body, headers=outsider.headers)).status_code == 404
    assert (
        await client.put(url, json={"verdict": "great"}, headers=member.headers)
    ).status_code == 422
    assert (
        await client.put(
            f"{base}/investigations/{uuid.uuid4()}/feedback", json=body, headers=member.headers
        )
    ).status_code == 404
    assert (await client.get(f"{base}/learning-records", headers=viewer.headers)).status_code == 200
    assert (
        await client.get(f"{base}/learning-records", headers=outsider.headers)
    ).status_code == 404


async def test_a_reviewed_investigation_can_become_an_evaluation_case(
    case: Case,
    client: AsyncClient,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: UserFactory,
) -> None:
    investigation_id = await _investigate(case, db_session, session_factory)
    admin, member = make_user(), make_user()
    await _join(client, db_session, case, admin, Role.ADMIN)
    await _join(client, db_session, case, member, Role.MEMBER)
    base = f"/v1/projects/{case.project_id}"
    url = f"{base}/investigations/{investigation_id}/evaluation-case"
    body = {
        "name": "bad-deploy-2-43",
        "expect_supported": True,
        "must_mention": ["2.43.0"],
        "must_not_mention": ["inventory-api"],
    }

    assert (await client.post(url, json=body, headers=member.headers)).status_code == 403
    created = await client.post(url, json=body, headers=admin.headers)
    duplicate = await client.post(url, json=body, headers=admin.headers)
    no_rules = await client.post(url, json={"name": "empty-case"}, headers=admin.headers)
    listed = await client.get(f"{base}/evaluation-cases", headers=admin.headers)

    assert created.status_code == 201
    assert created.json()["evidence_items"] == 7
    assert (duplicate.status_code, no_rules.status_code) == (409, 422)
    assert [c["name"] for c in listed.json()] == ["bad-deploy-2-43"]

    # The operator's evaluation command loads it, with the evidence the model saw.
    org_slug, project_slug = (
        await db_session.execute(
            select(Organization.slug, Project.slug)
            .join(Project, Project.organization_id == Organization.id)
            .where(Project.id == case.project_id)
        )
    ).one()
    [loaded] = await project_eval_cases(
        db_session, organization_slug=org_slug, project_slug=project_slug
    )
    assert (loaded.name, loaded.must_mention, loaded.expect_supported) == (
        "bad-deploy-2-43",
        ("2.43.0",),
        True,
    )
    assert loaded.evidence[1]["kind"] == "anomaly"
    assert loaded.evidence[1]["most_extreme_value"] == 900.0
