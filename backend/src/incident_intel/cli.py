"""Administrative CLI (``ii``).

Plaintext API keys are printed to stdout exactly once; logs go to stderr so the key can be
captured cleanly (for example ``KEY=$(ii api-keys create ...)``).

Tenancy commands load the full API settings (they need the API-key pepper). Kafka and
consumer commands load only runtime settings, so worker processes never hold API secrets.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from confluent_kafka import KafkaException
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.audit.service import CLI_ACTOR
from incident_intel.cache.services import build_redis_cache
from incident_intel.core.config import (
    RuntimeSettings,
    Settings,
    get_runtime_settings,
    get_settings,
)
from incident_intel.core.errors import AppError
from incident_intel.core.logging import configure_logging
from incident_intel.db.session import create_engine, create_session_factory
from incident_intel.detection.consumer import run_detection_consumer
from incident_intel.detection.evaluation import candidates, evaluate, format_report
from incident_intel.detection.service import DetectionSettings
from incident_intel.investigation.llm import WorkerSettings, build_provider
from incident_intel.investigation.prompts import PROMPT_VERSION
from incident_intel.investigation.worker import run_worker
from incident_intel.learning.evaluation import builtin_cases, format_results, run_cases
from incident_intel.learning.service import project_eval_cases
from incident_intel.simulator.client import IngestClient, SimulatorError, build_http_client
from incident_intel.simulator.runner import SimulationPlan, run_simulation
from incident_intel.streaming.admin import ensure_topics, read_topic_from_start
from incident_intel.streaming.topics import DEPLOYMENTS_DLQ, LOGS_DLQ, METRICS_DLQ, topic_name
from incident_intel.telemetry.consumer import run_storage_consumer
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

SIMULATOR_KEY_ENV = "SIMULATOR_API_KEY"
_DLQ_TOPICS = {"metrics": METRICS_DLQ, "logs": LOGS_DLQ, "deployments": DEPLOYMENTS_DLQ}
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

    kafka = commands.add_parser("kafka", help="Kafka administration")
    kafka_commands = kafka.add_subparsers(dest="kafka_command", required=True)
    kafka_commands.add_parser("init", help="Create missing topics (existing ones are unchanged)")

    consume = commands.add_parser("consume", help="Run a long-lived consumer until SIGTERM")
    consume.add_argument("consumer", choices=["storage", "detection"])

    commands.add_parser("work", help="Run the investigation worker until SIGTERM")

    evaluation = commands.add_parser("eval", help="Score detectors on labelled scenarios")
    evaluation.add_argument("suite", choices=["detection", "investigation"])
    evaluation.add_argument(
        "--project",
        metavar="ORG/PROJECT",
        help="investigation suite: also run this project's saved cases",
    )
    evaluation.add_argument("--case", action="append", help="run only this case (repeatable)")

    dlq = commands.add_parser("dlq", help="Inspect dead-lettered messages")
    dlq_commands = dlq.add_subparsers(dest="dlq_command", required=True)
    inspect = dlq_commands.add_parser("inspect", help="Show dead-lettered messages")
    inspect.add_argument("--kind", choices=sorted(_DLQ_TOPICS), default="metrics")
    inspect.add_argument("--limit", type=int, default=20)
    inspect.add_argument(
        "--show-values",
        action="store_true",
        help="include a preview of each message body (contains customer telemetry)",
    )

    simulate = commands.add_parser(
        "simulate",
        help="Send synthetic telemetry (a payment incident) through the ingestion API",
        description=(
            "Generates clearly labelled synthetic telemetry and sends it to the ingestion API. "
            f"The API key is read from the {SIMULATOR_KEY_ENV} environment variable."
        ),
    )
    simulate.add_argument("--api-url", default="http://localhost:8000")
    simulate.add_argument("--backfill-minutes", type=int, default=60, help="history to send first")
    simulate.add_argument(
        "--incident-after-minutes",
        type=int,
        default=45,
        help="minutes after the start of the backfill at which the incident begins",
    )
    simulate.add_argument("--no-incident", action="store_true", help="healthy baseline only")
    simulate.add_argument("--step-seconds", type=int, default=15, help="sampling interval")
    simulate.add_argument("--seed", type=int, default=1)
    simulate.add_argument("--live", action="store_true", help="keep sending until interrupted")

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


async def _revoke(session: AsyncSession, prefix: str, settings: Settings) -> None:
    cache = build_redis_cache(settings)
    try:
        await revoke_api_key(session, key_prefix=prefix, actor=CLI_ACTOR)
        print(f"Revoked API key {prefix}.", file=sys.stderr)
        # The API caches verified keys briefly; drop the entry so revocation is immediate.
        if not await cache.api_keys.invalidate(prefix):
            print(
                "warning: Redis is unreachable, so the key could not be removed from the "
                "API-key cache. It may keep working for up to "
                f"{settings.api_key_cache_ttl_seconds} seconds.",
                file=sys.stderr,
            )
    finally:
        await cache.close()


async def _api_keys(session: AsyncSession, args: argparse.Namespace, settings: Settings) -> None:
    if args.key_command == "revoke":
        await _revoke(session, args.prefix, settings)
        return

    pepper = settings.api_key_pepper.get_secret_value()
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


async def _run_tenancy(args: argparse.Namespace, settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            if args.command == "bootstrap":
                pepper = settings.api_key_pepper.get_secret_value()
                await _bootstrap(session, args, pepper)
            elif args.command == "api-keys":
                await _api_keys(session, args, settings)
    finally:
        await engine.dispose()


def _kafka_init(settings: RuntimeSettings) -> None:
    for name, status in ensure_topics(settings).items():
        print(f"{name}\t{status}", file=sys.stderr)


async def _consume(settings: RuntimeSettings, consumer: str) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    if consumer == "detection":
        await run_detection_consumer(settings, stop)
    else:
        await run_storage_consumer(settings, stop)


def _eval_detection(settings: RuntimeSettings) -> None:
    """Compare the candidate detectors, using the configured threshold and engine."""
    detection = DetectionSettings.from_settings(settings)
    factories = candidates(settings.detection_threshold, settings.detection_min_history)
    reports = [evaluate(name, factory, detection.engine) for name, factory in factories.items()]
    print(
        f"Detection evaluation on synthetic labelled scenarios "
        f"(threshold {settings.detection_threshold}, "
        f"{settings.detection_min_consecutive} points in a row to open).\n"
    )
    print(format_report(reports))


def _dlq_inspect(args: argparse.Namespace, settings: RuntimeSettings) -> None:
    topic = topic_name(settings, _DLQ_TOPICS[args.kind])
    for message in read_topic_from_start(settings, topic, limit=args.limit):
        record = {
            "position": message.position,
            "reason": message.headers.get("dlq.reason"),
            "detail": message.headers.get("dlq.detail"),
            "source": message.headers.get("dlq.source"),
            "size_bytes": len(message.value),
        }
        if args.show_values:
            record["value_preview"] = message.value[:500].decode("utf-8", errors="replace")
        print(json.dumps(record))


async def _simulate(args: argparse.Namespace) -> None:
    # Read from the environment, not a flag: command lines are visible to other users.
    api_key = os.environ.get(SIMULATOR_KEY_ENV)
    if not api_key:
        raise SimulatorError(f"set {SIMULATOR_KEY_ENV} to a project API key with ingest:write")
    if not 1 <= args.backfill_minutes <= 6 * 24 * 60:
        raise SimulatorError("--backfill-minutes must be between 1 and 8640 (6 days)")
    if args.step_seconds < 1:
        raise SimulatorError("--step-seconds must be at least 1")
    plan = SimulationPlan(
        backfill=timedelta(minutes=args.backfill_minutes),
        step=timedelta(seconds=args.step_seconds),
        incident_after=None if args.no_incident else timedelta(minutes=args.incident_after_minutes),
        seed=args.seed,
        live=args.live,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    async with build_http_client(args.api_url, api_key) as http:
        summary = await run_simulation(IngestClient(http), plan, stop=stop)
    print(
        f"Sent synthetic telemetry: {summary.points} metric points, {summary.records} log "
        f"records, {summary.deployments} deployments in {summary.requests} requests.",
        file=sys.stderr,
    )


async def _eval_investigation(args: argparse.Namespace) -> bool:
    """Run evaluation cases against the configured model. Calls the real LLM."""
    settings = WorkerSettings()
    cases = builtin_cases()
    if args.project:
        org, _, project = args.project.partition("/")
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                cases += await project_eval_cases(
                    session, organization_slug=org, project_slug=project
                )
        finally:
            await engine.dispose()
    if args.case:
        cases = [case for case in cases if case.name in set(args.case)]
    if not cases:
        raise AppError("No evaluation cases match.")
    provider = build_provider(settings)
    try:
        results = await run_cases(provider, cases)
    finally:
        await provider.close()
    print(format_results(results, model=provider.model, prompt_version=PROMPT_VERSION))
    return all(result.passed for result in results)


async def _work() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    # Loaded here only: the worker is the one process that holds the LLM API key.
    await run_worker(WorkerSettings(), stop)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings: RuntimeSettings = (
        get_settings() if args.command in {"bootstrap", "api-keys"} else get_runtime_settings()
    )
    configure_logging(level=settings.log_level, json=settings.log_json, stream=sys.stderr)
    try:
        if isinstance(settings, Settings):
            asyncio.run(_run_tenancy(args, settings))
        elif args.command == "kafka":
            _kafka_init(settings)
        elif args.command == "consume":
            asyncio.run(_consume(settings, args.consumer))
        elif args.command == "work":
            asyncio.run(_work())
        elif args.command == "eval" and args.suite == "investigation":
            return 0 if asyncio.run(_eval_investigation(args)) else 1
        elif args.command == "eval":
            _eval_detection(settings)
        elif args.command == "dlq":
            _dlq_inspect(args, settings)
        elif args.command == "simulate":
            asyncio.run(_simulate(args))
    except AppError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except SimulatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KafkaException as exc:
        print(f"error: kafka: {exc.args[0].str()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
