"""Programmatic access to Alembic configuration (readiness checks, tests)."""

from alembic.config import Config
from alembic.script import ScriptDirectory

SCRIPT_LOCATION = "incident_intel:migrations"


def alembic_config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    if database_url is not None:
        # Passed via attributes (not the ini option) so '%' in passwords needs no escaping.
        config.attributes["database_url"] = database_url
    return config


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("No Alembic migrations found")
    return head
