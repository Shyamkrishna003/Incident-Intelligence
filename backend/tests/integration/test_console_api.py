"""Signed-in user endpoints: identity, organizations, projects, API keys, and reading
telemetry, with role checks and tenant isolation."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.models import AuditLog
from incident_intel.auth.tokens import VerifiedIdentity
from incident_intel.telemetry.storage import store_metric_batch
from incident_intel.tenancy.models import ApiKey, Membership, User
from incident_intel.tenancy.roles import Role
from incident_intel.tenancy.users import get_or_create_user
from tests.conftest import SignedInUser, UserFactory
from tests.support import FakeCache, FakeTokenVerifier, metric_batch, metric_point

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Workspace:
    """An organization with one project, owned by `owner`."""

    owner: SignedInUser
    organization_id: str
    project_id: str


async def _workspace(client: AsyncClient, owner: SignedInUser) -> Workspace:
    slug = f"org-{uuid.uuid4().hex[:8]}"
    org = await client.post(
        "/v1/organizations", json={"slug": slug, "name": "Acme"}, headers=owner.headers
    )
    assert org.status_code == 201, org.text
    project = await client.post(
        f"/v1/organizations/{org.json()['id']}/projects",
        json={"slug": "payments", "name": "Payments"},
        headers=owner.headers,
    )
    assert project.status_code == 201, project.text
    return Workspace(owner, org.json()["id"], project.json()["id"])


async def _join(
    client: AsyncClient,
    session: AsyncSession,
    user: SignedInUser,
    workspace: Workspace,
    role: Role,
) -> None:
    """Add `user` to the workspace's organization (invitations are a later slice)."""
    me = await client.get("/v1/me", headers=user.headers)  # creates the user row
    session.add(
        Membership(
            organization_id=uuid.UUID(workspace.organization_id),
            user_id=uuid.UUID(me.json()["user"]["id"]),
            role=role.value,
        )
    )
    await session.commit()


# --- Identity ---------------------------------------------------------------------------


async def test_first_request_creates_the_user_once(
    client: AsyncClient, db_session: AsyncSession, make_user: UserFactory
) -> None:
    user = make_user()

    first = await client.get("/v1/me", headers=user.headers)
    second = await client.get("/v1/me", headers=user.headers)

    assert first.status_code == 200
    assert first.json()["organizations"] == []
    assert first.json()["user"]["email"] == f"{user.uid}@example.test"
    assert second.json()["user"]["id"] == first.json()["user"]["id"]
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.firebase_uid == user.uid)
    )
    assert count == 1


async def test_profile_follows_the_token(
    client: AsyncClient, make_user: UserFactory, token_verifier: FakeTokenVerifier
) -> None:
    before = make_user(email_verified=False)
    assert (await client.get("/v1/me", headers=before.headers)).json()["user"][
        "email_verified"
    ] is False

    # The same person signs in again after verifying their email and setting a name.
    after_token = token_verifier.issue(uid=before.uid, email_verified=True, display_name="Priya")
    after = await client.get("/v1/me", headers={"Authorization": f"Bearer {after_token}"})

    assert after.json()["user"]["email_verified"] is True
    assert after.json()["user"]["display_name"] == "Priya"


async def test_creating_a_user_that_already_exists_is_safe(db_session: AsyncSession) -> None:
    identity = VerifiedIdentity(
        uid=f"uid-{uuid.uuid4().hex}",
        email="a@example.test",
        email_verified=True,
        display_name=None,
        sign_in_provider="password",
    )

    first = await get_or_create_user(db_session, identity)
    again = await get_or_create_user(db_session, identity)

    assert again.id == first.id


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer unknown-token"}, {"Authorization": "Basic abc"}],
    ids=["missing", "unknown", "wrong-scheme"],
)
async def test_rejects_requests_without_a_valid_token(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/v1/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_verification_outage_is_a_503(
    client: AsyncClient, make_user: UserFactory, token_verifier: FakeTokenVerifier
) -> None:
    user = make_user()
    token_verifier.unavailable = True

    response = await client.get("/v1/me", headers=user.headers)

    assert response.status_code == 503


async def test_users_are_rate_limited_individually(
    client: AsyncClient, make_user: UserFactory, cache: FakeCache
) -> None:
    cache.limit = 1
    busy, quiet = make_user(), make_user()
    await client.get("/v1/me", headers=busy.headers)

    assert (await client.get("/v1/me", headers=busy.headers)).status_code == 429
    assert (await client.get("/v1/me", headers=quiet.headers)).status_code == 200


# --- Organizations and projects ----------------------------------------------------------


async def test_creating_an_organization_makes_the_caller_its_owner(
    client: AsyncClient, db_session: AsyncSession, make_user: UserFactory
) -> None:
    user = make_user()
    workspace = await _workspace(client, user)

    me = (await client.get("/v1/me", headers=user.headers)).json()

    [organization] = me["organizations"]
    assert organization["id"] == workspace.organization_id
    assert organization["role"] == "owner"
    assert [p["id"] for p in organization["projects"]] == [workspace.project_id]
    audit = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.organization_id == uuid.UUID(workspace.organization_id))
        )
    ).all()
    assert {(row.action, row.actor_type, row.actor_id) for row in audit} == {
        ("organization.created", "user", me["user"]["id"]),
        ("project.created", "user", me["user"]["id"]),
    }


async def test_unverified_email_cannot_create_an_organization(
    client: AsyncClient, make_user: UserFactory
) -> None:
    user = make_user(email_verified=False)

    response = await client.post(
        "/v1/organizations", json={"slug": "acme-x", "name": "Acme"}, headers=user.headers
    )

    assert response.status_code == 403
    assert (await client.get("/v1/me", headers=user.headers)).json()["organizations"] == []


async def test_duplicate_organization_slug_is_a_conflict_and_leaves_no_membership(
    client: AsyncClient, make_user: UserFactory
) -> None:
    first, second = make_user(), make_user()
    slug = f"org-{uuid.uuid4().hex[:8]}"
    await client.post("/v1/organizations", json={"slug": slug, "name": "A"}, headers=first.headers)

    response = await client.post(
        "/v1/organizations", json={"slug": slug, "name": "B"}, headers=second.headers
    )

    assert response.status_code == 409
    assert (await client.get("/v1/me", headers=second.headers)).json()["organizations"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "Bad Slug", "name": "A"},
        {"slug": "ok", "name": "   "},
        {"slug": "ok"},
        {"slug": "ok", "name": "A", "role": "owner"},
    ],
    ids=["bad-slug", "blank-name", "missing-name", "extra-field"],
)
async def test_organization_input_is_validated(
    client: AsyncClient, make_user: UserFactory, body: dict[str, Any]
) -> None:
    response = await client.post("/v1/organizations", json=body, headers=make_user().headers)

    assert response.status_code == 422


async def test_owned_organizations_are_capped(
    client: AsyncClient, app: Any, make_user: UserFactory
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"max_owned_organizations_per_user": 1}
    )
    user = make_user()
    await _workspace(client, user)

    response = await client.post(
        "/v1/organizations",
        json={"slug": f"org-{uuid.uuid4().hex[:8]}", "name": "Second"},
        headers=user.headers,
    )

    assert response.status_code == 409


# --- Roles ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "can_read", "can_admin"),
    [
        (Role.VIEWER, True, False),
        (Role.MEMBER, True, False),
        (Role.ADMIN, True, True),
        (Role.OWNER, True, True),
    ],
)
async def test_role_matrix(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: UserFactory,
    role: Role,
    can_read: bool,
    can_admin: bool,
) -> None:
    workspace = await _workspace(client, make_user())
    user = make_user()
    await _join(client, db_session, user, workspace, role)
    project = f"/v1/projects/{workspace.project_id}"
    denied = 403

    services = await client.get(f"{project}/services", headers=user.headers)
    list_keys = await client.get(f"{project}/api-keys", headers=user.headers)
    create_key = await client.post(f"{project}/api-keys", json={"name": "ci"}, headers=user.headers)
    create_project = await client.post(
        f"/v1/organizations/{workspace.organization_id}/projects",
        json={"slug": "second", "name": "Second"},
        headers=user.headers,
    )
    revoke = await client.delete(f"{project}/api-keys/abcdefghijkl", headers=user.headers)

    assert services.status_code == (200 if can_read else denied)
    assert list_keys.status_code == (200 if can_admin else denied)
    assert create_key.status_code == (201 if can_admin else denied)
    assert create_project.status_code == (201 if can_admin else denied)
    # An admin gets "no such key"; anyone below is stopped before the lookup.
    assert revoke.status_code == (404 if can_admin else denied)


async def test_losing_membership_takes_effect_on_the_next_request(
    client: AsyncClient, db_session: AsyncSession, make_user: UserFactory
) -> None:
    workspace = await _workspace(client, make_user())
    user = make_user()
    await _join(client, db_session, user, workspace, Role.ADMIN)
    url = f"/v1/projects/{workspace.project_id}/services"
    assert (await client.get(url, headers=user.headers)).status_code == 200

    await db_session.execute(
        update(Membership)
        .where(Membership.organization_id == uuid.UUID(workspace.organization_id))
        .where(Membership.role == Role.ADMIN.value)
        .values(role=Role.VIEWER.value)
    )
    await db_session.commit()

    # Same token, no re-login: authorization is read from our database every time.
    assert (await client.get(url, headers=user.headers)).status_code == 200
    assert (
        await client.get(f"/v1/projects/{workspace.project_id}/api-keys", headers=user.headers)
    ).status_code == 403


# --- Tenant isolation -------------------------------------------------------------------


async def test_another_organizations_resources_are_not_found(
    client: AsyncClient, make_user: UserFactory
) -> None:
    theirs = await _workspace(client, make_user())
    outsider = make_user()
    await _workspace(client, outsider)  # the outsider is an owner, but of a different org
    project = f"/v1/projects/{theirs.project_id}"

    responses = [
        await client.get(f"{project}/services", headers=outsider.headers),
        await client.get(
            f"{project}/services/payment-api/metrics/latency", headers=outsider.headers
        ),
        await client.get(f"{project}/services/payment-api/metrics", headers=outsider.headers),
        await client.get(f"{project}/api-keys", headers=outsider.headers),
        await client.post(f"{project}/api-keys", json={"name": "x"}, headers=outsider.headers),
        await client.delete(f"{project}/api-keys/abcdefghijkl", headers=outsider.headers),
        await client.post(
            f"/v1/organizations/{theirs.organization_id}/projects",
            json={"slug": "sneaky", "name": "Sneaky"},
            headers=outsider.headers,
        ),
    ]

    assert [r.status_code for r in responses] == [404] * 7
    # Indistinguishable from a project that does not exist at all.
    missing = await client.get(f"/v1/projects/{uuid.uuid4()}/services", headers=outsider.headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == responses[0].json()["error"]["message"]


async def test_cannot_revoke_another_projects_key_by_prefix(
    client: AsyncClient, make_user: UserFactory
) -> None:
    victim = await _workspace(client, make_user())
    victim_key = (
        await client.post(
            f"/v1/projects/{victim.project_id}/api-keys",
            json={"name": "prod"},
            headers=victim.owner.headers,
        )
    ).json()
    attacker = await _workspace(client, make_user())

    response = await client.delete(
        f"/v1/projects/{attacker.project_id}/api-keys/{victim_key['prefix']}",
        headers=attacker.owner.headers,
    )

    assert response.status_code == 404
    still_works = await client.get(
        "/v1/project", headers={"Authorization": f"Bearer {victim_key['key']}"}
    )
    assert still_works.status_code == 200


# --- API keys ---------------------------------------------------------------------------


async def test_api_key_lifecycle(
    client: AsyncClient, db_session: AsyncSession, make_user: UserFactory
) -> None:
    workspace = await _workspace(client, make_user())
    headers = workspace.owner.headers
    keys_url = f"/v1/projects/{workspace.project_id}/api-keys"

    created = await client.post(
        keys_url,
        json={"name": "ingest only", "scopes": ["ingest:write"], "expires_in_days": 30},
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["scopes"] == ["ingest:write"]
    assert body["expires_at"] is not None
    key_headers = {"Authorization": f"Bearer {body['key']}"}

    # The new key works as a machine credential for exactly this project.
    as_machine = await client.get("/v1/project", headers=key_headers)
    assert as_machine.json()["project"]["id"] == workspace.project_id

    # Listing never exposes the key or its hash.
    listed = await client.get(keys_url, headers=headers)
    [item] = listed.json()["api_keys"]
    assert item["prefix"] == body["prefix"]
    assert "key" not in item
    stored = await db_session.scalar(select(ApiKey).where(ApiKey.key_prefix == body["prefix"]))
    assert stored is not None
    assert stored.key_hash not in listed.text
    assert body["key"] not in listed.text

    # Revoking takes effect immediately and can be repeated.
    assert (await client.delete(f"{keys_url}/{body['prefix']}", headers=headers)).status_code == 204
    assert (await client.delete(f"{keys_url}/{body['prefix']}", headers=headers)).status_code == 204
    assert (await client.get("/v1/project", headers=key_headers)).status_code == 401
    [after] = (await client.get(keys_url, headers=headers)).json()["api_keys"]
    assert after["revoked_at"] is not None

    actions = (
        await db_session.scalars(
            select(AuditLog.action).where(
                AuditLog.project_id == uuid.UUID(workspace.project_id),
                AuditLog.actor_type == "user",
            )
        )
    ).all()
    assert sorted(actions) == ["api_key.created", "api_key.revoked", "project.created"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "x", "scopes": []},
        {"name": "x", "scopes": ["admin"]},
        {"name": "x", "expires_in_days": 0},
    ],
    ids=["blank-name", "no-scopes", "unknown-scope", "bad-expiry"],
)
async def test_api_key_input_is_validated(
    client: AsyncClient, make_user: UserFactory, body: dict[str, Any]
) -> None:
    workspace = await _workspace(client, make_user())

    response = await client.post(
        f"/v1/projects/{workspace.project_id}/api-keys", json=body, headers=workspace.owner.headers
    )

    assert response.status_code == 422


async def test_an_api_key_is_not_a_user_credential_and_vice_versa(
    client: AsyncClient, make_user: UserFactory
) -> None:
    workspace = await _workspace(client, make_user())
    key = (
        await client.post(
            f"/v1/projects/{workspace.project_id}/api-keys",
            json={"name": "ci"},
            headers=workspace.owner.headers,
        )
    ).json()["key"]

    key_on_user_route = await client.get("/v1/me", headers={"Authorization": f"Bearer {key}"})
    token_on_key_route = await client.get("/v1/project", headers=workspace.owner.headers)

    assert key_on_user_route.status_code == 401
    assert token_on_key_route.status_code == 401


# --- Reading telemetry ------------------------------------------------------------------


async def test_user_reads_metrics_of_their_project_only(
    client: AsyncClient, db_session: AsyncSession, make_user: UserFactory
) -> None:
    mine = await _workspace(client, make_user())
    other = await _workspace(client, make_user())
    now = datetime.now(UTC).replace(microsecond=0)

    for workspace, value in ((mine, 120.0), (other, 999.0)):
        key = (
            await client.post(
                f"/v1/projects/{workspace.project_id}/api-keys",
                json={"name": "ci"},
                headers=workspace.owner.headers,
            )
        ).json()
        await store_metric_batch(
            db_session,
            metric_batch(
                organization_id=uuid.UUID(workspace.organization_id),
                project_id=uuid.UUID(workspace.project_id),
                api_key_id=uuid.UUID(key["id"]),
                points=[
                    metric_point(now - timedelta(minutes=1), value, metric="latency"),
                ],
                received_at=now,
            ),
        )

    services = await client.get(
        f"/v1/projects/{mine.project_id}/services", headers=mine.owner.headers
    )
    metrics = await client.get(
        f"/v1/projects/{mine.project_id}/services/payment-api/metrics/latency",
        headers=mine.owner.headers,
    )

    assert [s["name"] for s in services.json()["services"]] == ["payment-api"]
    listed = await client.get(
        f"/v1/projects/{mine.project_id}/services/payment-api/metrics", headers=mine.owner.headers
    )
    assert [m["name"] for m in listed.json()["metrics"]] == ["latency"]
    values = [p["value"] for s in metrics.json()["series"] for p in s["points"]]
    assert values == [120.0]
