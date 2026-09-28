import asyncio
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from incident_intel.db.migrations import alembic_config
from incident_intel.db.registry import Base

pytestmark = pytest.mark.integration

TENANCY_TABLES = {"organizations", "projects", "api_keys", "audit_logs"}


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    def diff(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    async with engine.connect() as connection:
        differences = await connection.run_sync(diff)

    assert differences == []


def _table_names(database_url: str) -> set[str]:
    async def inspect_tables() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_conn: set(inspect(sync_conn).get_table_names())
                )
        finally:
            await engine.dispose()

    return asyncio.run(inspect_tables())


def test_downgrade_and_upgrade_round_trip(migrated_database: str) -> None:
    # Sync test on purpose: Alembic's async env.py drives its own event loop.
    config = alembic_config(migrated_database)

    command.downgrade(config, "base")
    assert not TENANCY_TABLES & _table_names(migrated_database)

    command.upgrade(config, "head")
    assert _table_names(migrated_database) >= TENANCY_TABLES
