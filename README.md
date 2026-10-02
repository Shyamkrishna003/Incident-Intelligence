# Incident Intelligence

AI-assisted incident detection, investigation, root-cause analysis, and reliability learning.

**Observe → Detect → Correlate → Investigate → Explain → Act → Verify → Learn**

- Product requirements: [PRD.md](PRD.md)
- Engineering rules: [CLAUDE.md](CLAUDE.md)
- Architecture and roadmap: [docs/architecture-proposal.md](docs/architecture-proposal.md)

## Status

Implemented through **slice 2a: metric ingestion via Kafka** (the *Observe* stage):

- **Foundation (slice 1):**
  - FastAPI backend with typed configuration, structured JSON logs (with secret redaction), and request IDs.
  - PostgreSQL with Alembic migrations.
  - Organizations, projects, and project-scoped API keys. Only an HMAC of each key is stored, and create/revoke actions are audit-logged.
- **Ingestion (slice 2a):**
  - Services send metric batches with an API key.
  - The API validates each batch and hands it to Kafka.
  - A storage consumer writes it to PostgreSQL exactly once, even when requests are retried or messages redelivered.
  - Batches that can never be stored go to a dead-letter topic.
  - Stored metrics can be read back through the API, scoped to the caller's project.

Next is slice 2b: Redis for rate limiting, synchronous idempotency conflicts (409) and an API-key cache. After that come sign-in (Firebase), the frontend, detection and AI investigation. See the architecture doc, §11.

## How ingestion works

```
service ──POST /v1/ingest/metrics──► API ── validate, stamp tenant ids ──► Kafka topic
          (API key, Idempotency-Key)  │                                  telemetry.metrics.v1
                                      │ 202 once Kafka acknowledged              │
                                      ▼                                          ▼
                                                               storage consumer ──► PostgreSQL
                                                                   │ (commit offset only
                                                                   │  after the DB commit)
                                                                   └─► telemetry.metrics.v1.dlq
                                                                       (messages that can never
                                                                        be stored)
```

- **Tenant safety.** The organization and project come from the verified API key and are stamped onto the Kafka message. The request body can't set them, and the database rejects rows whose project doesn't match their parent's.
- **No duplicates.** The `batch_id` is derived from the project and the `Idempotency-Key`, so a retried request maps to the same batch. The consumer skips batches it has already stored, and each point is unique on (series, timestamp).
- **Asynchronous.** A 202 means the batch is safely in Kafka. It becomes readable after the consumer stores it, normally within a second.

### Failure behavior

| Failure | What happens |
|---|---|
| Kafka down | Ingestion returns **503** with `Retry-After` after at most about 5 s. `/readyz` reports `kafka: unavailable`. Clients retry with the same Idempotency-Key. |
| PostgreSQL down | The API returns **503** with `Retry-After`, because authentication needs the DB. Batches already in Kafka wait there. The consumer retries with backoff (0.5 s doubling to a 30 s cap) and stores them once the DB is back. |
| Malformed or unstorable message | The message is sent to the dead-letter topic with a reason, and later messages keep flowing. Inspect with `make dlq`. |
| Same Idempotency-Key reused with different data | The first batch is kept. The second is dead-lettered as `idempotency_conflict`. Slice 2b will reject this synchronously with 409. |
| Consumer crash mid-batch | The offset isn't committed, so the batch is delivered again and stored once. |

### Kafka at a glance

- **Responsibility:** the durable buffer between accepting telemetry and storing it. It decouples the two, lets consumers replay after an outage, and lets several consumers read the same stream (detection joins in a later slice).
- **Topics:**
  - `telemetry.metrics.v1`: 6 partitions, keyed by project, 3-day retention.
  - `telemetry.metrics.v1.dlq`: 1 partition, 14-day retention.
  - Both have a 2 MiB max message size.
  - Topics are created by `ii kafka init`. The broker never auto-creates them.
- **Local setup:**
  - A single broker in KRaft mode (no ZooKeeper), with the JVM heap capped at 512 MB.
  - It runs PLAINTEXT, without authentication, and is published on localhost only. TLS, SASL and ACLs come with deployment hardening.
- **Operating cost:** one more service to run and monitor. Consumer lag is the key health metric (Prometheus arrives in the hardening slice).

## Prerequisites

- Python 3.12 (with `venv`)
- Docker with Compose v2. Your user must be able to run `docker` without sudo.
- GNU Make

## Quick start

Everything in Docker:

```bash
make env          # create .env with a generated API-key pepper (never overwrites an existing .env)
make venv         # create .venv and install hash-pinned dependencies (host tooling)
make up           # PostgreSQL, Kafka, migrations, topic setup, API, storage consumer
make bootstrap ORG=acme PROJECT=payments   # prints an API key ONCE on stdout
```

Send metrics and read them back:

```bash
KEY=ii_...   # from make bootstrap
./examples/send-metrics.sh
```

Or run the API and consumer on the host, for fast iteration:

```bash
make infra        # PostgreSQL + Kafka in Docker, create topics
make migrate
make run          # API with auto-reload on http://localhost:8000
make consume      # storage consumer (in another terminal)
```

The OpenAPI docs are at http://localhost:8000/docs. They're disabled when `ENVIRONMENT=production`.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | none | Liveness. The process is up. Touches no dependencies. |
| GET | `/readyz` | none | Readiness. Returns 200 when the DB is reachable and migrated and Kafka is reachable with its topics, and 503 naming the failing check otherwise. |
| GET | `/v1/project` | API key | The organization, project, and key the credential belongs to |
| POST | `/v1/ingest/metrics` | API key with `ingest:write` | Accepts 1–1000 points (body ≤ 1 MiB). Requires an `Idempotency-Key` header. Returns **202** with `batch_id`. |
| GET | `/v1/services` | API key with `telemetry:read` | Services that have sent telemetry to this project |
| GET | `/v1/services/{service}/metrics/{metric}` | API key with `telemetry:read` | Points in `[start, end)` (default: the last hour; at most 24 h), grouped by attribute set. `limit` defaults to 1000 and can be at most 10 000; `truncated` is true if more points exist. |

### Ingestion rules

- **Names:** `service` and `metric` are lowercase letters, digits, `.`, `_` and `-`, up to 128 characters, starting with a letter or digit.
- **Values:** `value` must be a JSON number. Strings, booleans, NaN and infinity are rejected.
- **Timestamps:** `timestamp` must include a timezone and fall within the last 7 days or at most 5 minutes in the future. Timestamps are stored in UTC.
- **Attributes:** at most 16 string pairs, with keys up to 64 characters and values up to 256.
- **Whole-batch validation:** a batch is accepted or rejected as a whole. Any invalid point rejects it with 422, and `details` lists each problem by position. Duplicate points in one batch (same series and timestamp) are rejected too.
- **Duplicates across batches:** points already stored for the same series and timestamp are skipped. The first write wins.

### Errors

Errors share one envelope: `{"error": {"code", "message", "request_id", "details"?}}`.

- Authentication failures always return the same generic 401. The specific reason is logged server-side only.
- A missing scope returns 403.
- A dependency outage returns 503 with `Retry-After`.
- Validation errors never echo submitted values.

## Admin CLI

Run from `backend/` with `../.venv/bin/ii`, or through the make targets.

```bash
ii bootstrap --org acme --project payments [--org-name "Acme"] [--project-name "Payments"]
ii api-keys create --org acme --project payments --name ci [--scope ingest:write] [--expires-in-days 90]
ii api-keys list   --org acme --project payments
ii api-keys revoke --prefix <12-char prefix>
ii kafka init                      # create missing topics (existing ones are left unchanged)
ii consume storage                 # run the storage consumer until SIGTERM/SIGINT
ii dlq inspect [--limit 20] [--show-values]   # dead-lettered messages; bodies hidden by default
```

- The plaintext API key is written to **stdout once**. Messages and logs go to stderr, so `KEY=$(ii api-keys create ...)` captures just the key.
- The consumer and Kafka commands don't need, or read, the API-key pepper.

## Development

| Command | What it does |
|---|---|
| `make check` | lint + type check + all tests |
| `make lint` / `make fmt` | ruff check / ruff format |
| `make typecheck` | mypy `--strict` |
| `make test` | pytest: unit, integration (PostgreSQL) and end-to-end (Kafka). Needs `make infra`. |
| `make test-unit` | tests that need neither PostgreSQL nor Kafka |
| `make logs` / `make dlq` | follow API and consumer logs / inspect the dead-letter topic |
| `make lock` | re-pin dependencies after editing `backend/pyproject.toml` |

### Testing

- **Integration tests** use the database in `TEST_DATABASE_URL`. Its name must end in `_test`, otherwise the run is refused.
  - Each test runs in a transaction that is rolled back afterwards.
  - One test checks that the models and migrations define exactly the same schema.
- **End-to-end Kafka tests** (marker `kafka`) run when `TEST_KAFKA_BOOTSTRAP_SERVERS` is set. Otherwise they're reported as skipped.
  - Each test creates its own uniquely named topics and deletes them afterwards.
- **Consumer delivery rules** (commit only after success, retry transient errors, dead-letter permanent ones) are unit-tested with an in-memory message source.

### Layout

```
backend/
  src/incident_intel/
    core/        config, logging + redaction, errors, middleware (request context, body limit)
    db/          engine/session lifecycle, base model, migration helpers
    tenancy/     organizations, projects, API keys, tenant context
    audit/       audit log
    api/         health probes, auth + scope dependencies
    streaming/   Kafka: topics, async publisher, consumer loop with dead-lettering, admin
    ingestion/   ingestion API: schemas, normalization, publishing
    telemetry/   message contract, models, idempotent storage consumer, read API
    migrations/  Alembic environment and revisions (shipped inside the package)
    cli.py       `ii` admin CLI
  tests/{unit,integration,kafka}/
examples/              example client script
infra/postgres/init/   first-run database init (creates the test database)
docs/                  architecture and decisions
```

## Configuration

See [.env.example](.env.example).

- `API_KEY_PEPPER` is required, with a minimum of 32 characters. **Changing it invalidates every existing API key.** Only the API container receives it.
- `KAFKA_BOOTSTRAP_SERVERS` is `localhost:9094` from the host. Containers use `kafka:9092`, set in compose.
- Real environment variables override `.env`, and `.env` is git-ignored. Never commit it.

## Known limitations

- **No rate limiting** yet (slice 2b, Redis).
- **Idempotency conflicts** (a key reused with different data) are detected asynchronously and dead-lettered rather than rejected with 409 (slice 2b).
- **Hot partitions:** Kafka messages are keyed by project, so one project's batches are processed in order but by a single consumer at a time. This is a throughput ceiling for very large tenants, revisited if measurements show it.
- **Units:** a series records its unit from its first point. A different unit later isn't flagged.
- **No retention job:** PostgreSQL metric data isn't yet deleted after the planned 30 days.
- **Consumer monitoring:** the storage consumer has no health endpoint, and consumer lag isn't yet exported (hardening slice).
- **Topic config:** `ii kafka init` doesn't reconcile the settings of topics that already exist.
- **Pepper rotation:** rotating the API-key pepper isn't supported, because hashes aren't versioned yet.
- **Row-level security:** tenant isolation is enforced in the application layer and by composite foreign keys. PostgreSQL row-level security is planned for the hardening slice.
- **CI:** there's no CI workflow until a GitHub remote exists.
