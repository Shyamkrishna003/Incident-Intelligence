"""Shared fixtures.

Integration tests run against a dedicated PostgreSQL database (TEST_DATABASE_URL, whose name
must end in ``_test``). Migrations are applied once per session; each test then runs inside
an outer transaction on one connection that is rolled back afterwards, so tests are isolated
and leave no data behind. Every session in a test (the API's, the storage consumer's) joins
that transaction, and service code that calls ``commit()`` only releases a SAVEPOINT.

Kafka tests (marker ``kafka``) additionally need TEST_KAFKA_BOOTSTRAP_SERVERS, and Redis
tests (marker ``redis``) need TEST_REDIS_URL. Everything else uses in-memory fakes.
"""

import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

import pytest
from alembic import command
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from incident_intel.audit.service import CLI_ACTOR
from incident_intel.core.config import Settings
from incident_intel.db.migrations import alembic_config
from incident_intel.db.session import get_session
from incident_intel.main import create_app
from incident_intel.tenancy.api_keys import DEFAULT_SCOPES, ApiKeyScope
from incident_intel.tenancy.models import ApiKey, Organization, Project
from incident_intel.tenancy.service import create_organization, create_project, issue_api_key
from tests.support import TEST_PEPPER, FakeCache, FakePublisher, make_settings


class _TestEnvironment(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    test_database_url: SecretStr | None = None
    test_kafka_bootstrap_servers: str | None = None
    test_redis_url: SecretStr | None = None


@pytest.fixture(scope="session")
def test_environment() -> _TestEnvironment:
    return _TestEnvironment()


@pytest.fixture(scope="session")
def test_database_url(test_environment: _TestEnvironment) -> str:
    configured = test_environment.test_database_url
    if configured is None:
        pytest.skip("TEST_DATABASE_URL is not set; integration tests need the test database")
    url = configured.get_secret_value()
    if not (make_url(url).database or "").endswith("_test"):
        pytest.exit("Refusing to run: TEST_DATABASE_URL database name must end in '_test'", 2)
    return url


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str) -> str:
    command.upgrade(alembic_config(test_database_url), "head")
    return test_database_url


@pytest.fixture(scope="session")
def settings(migrated_database: str) -> Settings:
    return make_settings(migrated_database)


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    test_engine = create_async_engine(migrated_database, poolclass=NullPool)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


@pytest.fixture
def session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Sessions that join the test's outer transaction (for code that opens its own)."""
    return async_sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


@pytest.fixture
async def app(
    settings: Settings, db_session: AsyncSession, publisher: FakePublisher, cache: FakeCache
) -> AsyncIterator[FastAPI]:
    application = create_app(settings, publisher=publisher, cache=cache.services())

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _test_session
    yield application
    await application.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@dataclass(frozen=True)
class Tenant:
    organization: Organization
    project: Project
    api_key: ApiKey
    plaintext_key: str

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.plaintext_key}"}


class TenantFactory:
    """Creates an isolated organization/project with one API key (all scopes by default)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __call__(self, *, scopes: Iterable[ApiKeyScope] = DEFAULT_SCOPES) -> Tenant:
        suffix = uuid.uuid4().hex[:8]
        organization = await create_organization(
            self._session, slug=f"org-{suffix}", name=f"Org {suffix}", actor=CLI_ACTOR
        )
        project = await create_project(
            self._session,
            organization_id=organization.id,
            slug="payments",
            name="Payments",
            actor=CLI_ACTOR,
        )
        issued = await issue_api_key(
            self._session,
            project=project,
            name="test",
            pepper=TEST_PEPPER,
            actor=CLI_ACTOR,
            scopes=scopes,
        )
        return Tenant(organization, project, issued.api_key, issued.plaintext)


@pytest.fixture
def make_tenant(db_session: AsyncSession) -> TenantFactory:
    return TenantFactory(db_session)
