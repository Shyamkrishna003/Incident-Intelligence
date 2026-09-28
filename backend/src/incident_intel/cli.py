"""Administrative CLI (``ii``).

Plaintext API keys are printed to stdout exactly once; logs go to stderr so the key can be
captured cleanly (for example ``KEY=$(ii api-keys create ...)``).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import CLI_ACTOR
from incident_intel.core.config import Settings, get_settings
from incident_intel.core.errors import AppError
from incident_intel.core.logging import configure_logging
from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.tenancy.api_keys import DEFAULT_SCOPES, ApiKeyScope
from incident_intel.tenancy.service import (
    IssuedApiKey,
    create_organization,
    create_project,
    get_project_by_slugs,
    issue_api_key,
    list_api_keys,
    revoke_api_key,
    validate_slug,
)

_KEY_WARNING = "Store this API key now; it cannot be shown again."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ii", description="Incident Intelligence admin CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    bootstrap = commands.add_parser(
        "bootstrap", help="Create an organization, a project, and its first API key"
    )
    bootstrap.add_argument("--org", required=True, help="organization slug")
    bootstrap.add_argument("--org-name", help="display name (defaults to the slug)")
    bootstrap.add_argument("--project", required=True, help="project slug")
    bootstrap.add_argument("--project-name", help="display name (defaults to the slug)")
    bootstrap.add_argument("--key-name", default="default")

    keys = commands.add_parser("api-keys", help="Manage project API keys")
    key_commands = keys.add_subparsers(dest="key_command", required=True)

    create = key_commands.add_parser("create", help="Issue a new API key")
    create.add_argument("--org", required=True)
    create.add_argument("--project", required=True)
    create.add_argument("--name", required=True)
    create.add_argument(
        "--scope",
        action="append",
        choices=[scope.value for scope in ApiKeyScope],
        help="repeatable; defaults to all scopes",
    )
    create.add_argument("--expires-in-days", type=int)

    revoke = key_commands.add_parser("revoke", help="Revoke an API key by its prefix")
    revoke.add_argument("--prefix", required=True)

    list_keys = key_commands.add_parser("list", help="List a project's API keys (no secrets)")
    list_keys.add_argument("--org", required=True)
    list_keys.add_argument("--project", required=True)

    return parser


def _print_issued(issued: IssuedApiKey) -> None:
    print(f"{_KEY_WARNING} (prefix: {issued.api_key.key_prefix})", file=sys.stderr)
    print(issued.plaintext)


async def _bootstrap(session: AsyncSession, args: argparse.Namespace, pepper: str) -> None:
    # Validate everything up front: each step commits, so a late validation failure would
    # otherwise leave an organization without a project.
    validate_slug(args.org, field="organization slug")
    validate_slug(args.project, field="project slug")
    organization = await create_organization(
        session, slug=args.org, name=args.org_name or args.org, actor=CLI_ACTOR
    )
    project = await create_project(
        session,
        organization_id=organization.id,
        slug=args.project,
        name=args.project_name or args.project,
        actor=CLI_ACTOR,
    )
    issued = await issue_api_key(
        session, project=project, name=args.key_name, pepper=pepper, actor=CLI_ACTOR
    )
    print(f"Created {organization.slug}/{project.slug}.", file=sys.stderr)
    _print_issued(issued)


async def _api_keys(session: AsyncSession, args: argparse.Namespace, pepper: str) -> None:
    if args.key_command == "revoke":
        api_key = await revoke_api_key(session, key_prefix=args.prefix, actor=CLI_ACTOR)
        print(f"Revoked API key {api_key.key_prefix}.", file=sys.stderr)
        return

    project = await get_project_by_slugs(
        session, organization_slug=args.org, project_slug=args.project
    )
    if args.key_command == "create":
        expires_at = (
            datetime.now(UTC) + timedelta(days=args.expires_in_days)
            if args.expires_in_days is not None
            else None
        )
        scopes = {ApiKeyScope(scope) for scope in args.scope} if args.scope else DEFAULT_SCOPES
        issued = await issue_api_key(
            session,
            project=project,
            name=args.name,
            pepper=pepper,
            actor=CLI_ACTOR,
            scopes=scopes,
            expires_at=expires_at,
        )
        _print_issued(issued)
    elif args.key_command == "list":
        for api_key in await list_api_keys(session, project=project):
            status = "revoked" if api_key.revoked_at else "active"
            expires = api_key.expires_at.isoformat() if api_key.expires_at else "never"
            print(
                f"{api_key.key_prefix}\t{status}\t{api_key.name}\t"
                f"{','.join(api_key.scopes)}\texpires={expires}"
            )


async def _run(args: argparse.Namespace, settings: Settings) -> None:
    engine = create_engine(settings)
    pepper = settings.api_key_pepper.get_secret_value()
    try:
        async with create_session_factory(engine)() as session:
            if args.command == "bootstrap":
                await _bootstrap(session, args, pepper)
            elif args.command == "api-keys":
                await _api_keys(session, args, pepper)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json, stream=sys.stderr)
    try:
        asyncio.run(_run(args, settings))
    except AppError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
