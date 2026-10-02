# Incident Intelligence

AI-assisted incident detection, investigation, root-cause analysis, and reliability learning.

**Observe → Detect → Correlate → Investigate → Explain → Act → Verify → Learn**

- Product requirements: [PRD.md](PRD.md)
- Engineering rules: [CLAUDE.md](CLAUDE.md)
- Architecture and roadmap: [docs/architecture-proposal.md](docs/architecture-proposal.md)

## Status

Implemented through **slice 3a: metric ingestion (the *Observe* stage) plus sign-in for people**:

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
- **Protection (slice 2b):**
  - Each API key is rate limited (429 with `Retry-After`).
  - An `Idempotency-Key` reused with different data is rejected immediately (409), and a retry of an already-published batch isn't published again.
  - Verified API keys are cached briefly, so most requests skip the database lookup.
  - Redis is only a cache and limiter. If it's down, the API keeps working.

- **Users and roles (slice 3a):**
  - People sign in with Firebase Authentication (email/password, Google or GitHub) and call the API with their ID token.
  - Firebase only proves identity. Organization membership and roles live in PostgreSQL and are checked on every request.
  - Signed-in users can create an organization and projects, manage API keys, and read their projects' services and metrics.

Next come the web frontend (slice 3b), detection and AI investigation. See the architecture doc, §11.

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
- **No duplicates.** The `batch_id` is derived from the project and the `Idempotency-Key`, so a retried request maps to the same batch. The API remembers each key in Redis for 24 hours: a retry of a published batch returns the same answer without a second Kafka message, and different data under the same key gets a 409. Independently of Redis, the consumer skips batches it has already stored, and each point is unique on (series, timestamp).
- **Asynchronous.** A 202 means the batch is safely in Kafka. It becomes readable after the consumer stores it, normally within a second.

### Failure behavior

| Failure | What happens |
|---|---|
| Kafka down | Ingestion returns **503** with `Retry-After` after at most about 5 s. `/readyz` reports `kafka: unavailable`. Clients retry with the same Idempotency-Key. |
| PostgreSQL down | The API returns **503** with `Retry-After`, because authentication needs the DB. Batches already in Kafka wait there. The consumer retries with backoff (0.5 s doubling to a 30 s cap) and stores them once the DB is back. |
| Malformed or unstorable message | The message is sent to the dead-letter topic with a reason, and later messages keep flowing. Inspect with `make dlq`. |
| Same Idempotency-Key reused with different data | **409** `idempotency_conflict`; the first batch is kept. If Redis is down at that moment, the second batch is instead dead-lettered as `idempotency_conflict` by the consumer. |
| Redis down | The API keeps working. Authentication falls back to PostgreSQL, duplicates are still caught by the consumer, and rate limiting lets requests through (set `RATE_LIMIT_FAIL_OPEN=false` to answer 503 instead). `/readyz` stays ready and reports `redis: unavailable`. After a failure, Redis is skipped for 5 s at a time, so an outage adds one short timeout (0.25 s), not one per request. |
| API key over its rate limit | **429** `rate_limited` with `Retry-After` (seconds until the current window ends). |
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

## Two kinds of caller

| | Programs (services sending telemetry) | People (using the console) |
|---|---|---|
| Credential | Project API key: `Authorization: Bearer ii_...` | Firebase ID token: `Authorization: Bearer <token>` |
| Scope | Exactly one project, fixed by the key | Every organization the user is a member of |
| Permissions | Key scopes: `ingest:write`, `telemetry:read` | Role in the organization (below) |
| Endpoints | `/v1/project`, `/v1/ingest/...`, `/v1/services/...` | `/v1/me`, `/v1/organizations/...`, `/v1/projects/...` |

The two are not interchangeable: an API key is rejected on user endpoints, and an ID token on key endpoints.

### Roles

| Role | Can do |
|---|---|
| `viewer` | Read projects, services and metrics |
| `member` | Same as viewer for now; will act on incidents in a later slice |
| `admin` | Also create projects, and create, list and revoke API keys |
| `owner` | Everything. The user who created the organization. |

- A user gets **404** for an organization or project they aren't a member of, the same as for one that doesn't exist. **403** means they are a member but their role is too low.
- Role changes take effect on the next request, with no need to sign in again.
- Creating an organization needs a verified email address. A user can own at most 10 organizations (`MAX_OWNED_ORGANIZATIONS_PER_USER`).

### Sign-in at a glance

- **Responsibility:** Firebase Authentication proves who a person is. This API verifies the ID token's signature, issuer, audience and expiry with the official SDK.
- **What the backend needs:** only `FIREBASE_PROJECT_ID`, a public identifier. No service-account key is used; verification relies on Google's public signing keys, which the SDK fetches and caches.
- **If `FIREBASE_PROJECT_ID` is unset:** user endpoints answer 503. API-key endpoints are unaffected.
- **If Google's keys can't be fetched:** user endpoints answer 503 until they can.
- **Local emulator (optional):** `make emulator` starts the Firebase Auth emulator on `localhost:9099`. Set `FIREBASE_AUTH_EMULATOR_HOST=localhost:9099` and `FIREBASE_PROJECT_ID=demo-incident-intel` to use it. The emulator issues unsigned tokens and doesn't check expiry, so the API refuses to start with it when `ENVIRONMENT=production`.

### Redis at a glance

- **Responsibility:** fast, short-lived state at the API edge. It is never the source of truth, and it runs without persistence.
  - **Rate limiting:** a fixed-window counter per API key (default 600 requests per 60 s). A client can burst up to twice the limit across a window boundary.
  - **Idempotency claims:** `Idempotency-Key` → content hash and `pending`/`published` state, kept for 24 hours.
  - **API-key cache:** the verified key record (its HMAC, never the key), kept for 60 s and deleted on revocation.
- **Revocation:** `ii api-keys revoke` deletes the cache entry, so a revoked key stops working immediately. If Redis is unreachable at that moment, the CLI warns that the key may work for up to 60 more seconds.
- **Who uses it:** the API and the admin CLI. The storage consumer doesn't.
- **Local setup:** one container, 128 MB memory cap with least-recently-used eviction, no password, published on localhost only. Authentication and TLS come with deployment hardening.
- **Operating cost:** one small service. Losing its data is harmless.

## Prerequisites

- Python 3.12 (with `venv`)
- Docker with Compose v2. Your user must be able to run `docker` without sudo.
- GNU Make

## Quick start

Everything in Docker:

```bash
make env          # create .env with a generated API-key pepper (never overwrites an existing .env)
make venv         # create .venv and install hash-pinned dependencies (host tooling)
make up           # PostgreSQL, Kafka, Redis, migrations, topic setup, API, storage consumer
make bootstrap ORG=acme PROJECT=payments   # prints an API key ONCE on stdout
```

Send metrics and read them back:

```bash
KEY=ii_...   # from make bootstrap
./examples/send-metrics.sh
```

Or run the API and consumer on the host, for fast iteration:

```bash
make infra        # PostgreSQL + Kafka + Redis in Docker, create topics
make migrate
make run          # API with auto-reload on http://localhost:8000
make consume      # storage consumer (in another terminal)
```

The OpenAPI docs are at http://localhost:8000/docs. They're disabled when `ENVIRONMENT=production`.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | none | Liveness. The process is up. Touches no dependencies. |
| GET | `/readyz` | none | Readiness. Returns 200 when the DB is reachable and migrated and Kafka is reachable with its topics, and 503 naming the failing check otherwise. Redis is reported but not required. |
| GET | `/v1/project` | API key | The organization, project, and key the credential belongs to |
| POST | `/v1/ingest/metrics` | API key with `ingest:write` | Accepts 1–1000 points (body ≤ 1 MiB). Requires an `Idempotency-Key` header. Returns **202** with `batch_id`, or **409** if the key was already used with different data. |
| GET | `/v1/services` | API key with `telemetry:read` | Services that have sent telemetry to this project |
| GET | `/v1/me` | signed-in user | The user, their organizations and roles, and each organization's projects. Creates the user record on first call. |
| POST | `/v1/organizations` | signed-in user, verified email | Create an organization. The caller becomes its owner. |
| POST | `/v1/organizations/{organization_id}/projects` | admin or owner | Create a project |
| GET | `/v1/projects/{project_id}/api-keys` | admin or owner | List the project's API keys (never the key or its hash) |
| POST | `/v1/projects/{project_id}/api-keys` | admin or owner | Create an API key. The full key is in this response only. |
| DELETE | `/v1/projects/{project_id}/api-keys/{prefix}` | admin or owner | Revoke a key of this project. Takes effect immediately. |
| GET | `/v1/projects/{project_id}/services` | any member | Services in the project |
| GET | `/v1/projects/{project_id}/services/{service}/metrics/{metric}` | any member | Metric points, same parameters as the API-key endpoint |
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
- Exceeding the rate limit returns 429 with `Retry-After`. The limit applies to every authenticated endpoint, per API key or per user.
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
- The consumer and Kafka commands don't need, or read, the API-key pepper or Redis.
- `ii api-keys revoke` also removes the key from the Redis API-key cache.

## Development

| Command | What it does |
|---|---|
| `make check` | lint + type check + all tests |
| `make lint` / `make fmt` | ruff check / ruff format |
| `make typecheck` | mypy `--strict` |
| `make test` | pytest: unit, integration (PostgreSQL), Redis, and end-to-end (Kafka). Needs `make infra`. Emulator tests also need `make emulator`. |
| `make test-unit` | tests that need no PostgreSQL, Kafka or Redis |
| `make logs` / `make dlq` | follow API and consumer logs / inspect the dead-letter topic |
| `make lock` | re-pin dependencies after editing `backend/pyproject.toml` |

### Testing

- **Integration tests** use the database in `TEST_DATABASE_URL`. Its name must end in `_test`, otherwise the run is refused.
  - Each test runs in a transaction that is rolled back afterwards.
  - One test checks that the models and migrations define exactly the same schema.
- **End-to-end Kafka tests** (marker `kafka`) run when `TEST_KAFKA_BOOTSTRAP_SERVERS` is set. Otherwise they're reported as skipped.
  - Each test creates its own uniquely named topics and deletes them afterwards.
- **Sign-in** is tested with an in-memory token verifier. The real Firebase verifier is tested offline (malformed, wrong project, unsigned tokens).
  - **Emulator tests** (marker `firebase`) run when `TEST_FIREBASE_AUTH_EMULATOR_HOST` is set and `make emulator` is running. Otherwise they're reported as skipped.
- **Roles and isolation:** every role is checked against every user endpoint, and a user from another organization gets 404 everywhere.
- **Redis tests** (marker `redis`) run when `TEST_REDIS_URL` is set. Otherwise they're reported as skipped.
  - Every other test uses an in-memory stand-in. A contract suite runs the same checks against both the stand-in and real Redis, so the two can't drift apart.
  - Each test uses a random key prefix and deletes only its own keys.
- **Consumer delivery rules** (commit only after success, retry transient errors, dead-letter permanent ones) are unit-tested with an in-memory message source.

### Layout

```
backend/
  src/incident_intel/
    core/        config, logging + redaction, errors, middleware (request context, body limit)
    db/          engine/session lifecycle, base model, migration helpers
    auth/        verifying Firebase ID tokens
    tenancy/     organizations, projects, API keys, users, memberships, roles, access checks
    audit/       audit log
    api/         health probes, auth + scope dependencies
    cache/       Redis: gateway with circuit breaker, rate limiter, idempotency claims, API-key cache
    streaming/   Kafka: topics, async publisher, consumer loop with dead-lettering, admin
    ingestion/   ingestion API: schemas, normalization, publishing
    telemetry/   message contract, models, idempotent storage consumer, read API
    migrations/  Alembic environment and revisions (shipped inside the package)
    cli.py       `ii` admin CLI
  tests/{unit,integration,cache,kafka,firebase}/
examples/              example client script
infra/postgres/init/   first-run database init (creates the test database)
infra/firebase-emulator/  optional local Firebase Auth emulator image
docs/                  architecture and decisions
```

## Configuration

See [.env.example](.env.example).

- `API_KEY_PEPPER` is required, with a minimum of 32 characters. **Changing it invalidates every existing API key.** Only the API container receives it.
- `KAFKA_BOOTSTRAP_SERVERS` is `localhost:9094` from the host. Containers use `kafka:9092`, set in compose.
- `REDIS_URL` is `redis://localhost:6379/0` from the host. The API container uses `redis://redis:6379/0`, set in compose.
- `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW_SECONDS` set the per-key limit. `RATE_LIMIT_FAIL_OPEN`, `IDEMPOTENCY_TTL_SECONDS` and `API_KEY_CACHE_TTL_SECONDS` are optional.
- `FIREBASE_PROJECT_ID` enables sign-in for people. `FIREBASE_AUTH_EMULATOR_HOST` switches to the local emulator (never in production).
- Real environment variables override `.env`, and `.env` is git-ignored. Never commit it.

## Known limitations

- **No invitations yet.** An organization has one user, its owner. Other members can only be added directly in the database until a later slice.
- **No web frontend yet** (slice 3b). User endpoints can be called with any valid Firebase ID token.
- **Token revocation isn't checked.** A Firebase ID token stays valid until it expires (up to an hour) even if the account is disabled in Firebase. Access still ends immediately when the membership is removed in this system.
- **Users, organizations and projects can't be deleted or renamed** through the API yet.
- **Rate limiting is per API key or user, after authentication.** Requests with invalid credentials aren't limited (no per-IP limit yet), and each one with a well-formed but unknown key costs a database lookup.
- **Rate-limit bursts:** the fixed window allows up to twice the limit across a window boundary.
- **Idempotency memory is 24 hours.** A key reused with different data after that, or while Redis is down, is caught by the consumer and dead-lettered instead of getting a 409.
- **`last_used_at`** for an API key is updated when the key is loaded from the database, so it can lag by the cache TTL (60 s).
- **Hot partitions:** Kafka messages are keyed by project, so one project's batches are processed in order but by a single consumer at a time. This is a throughput ceiling for very large tenants, revisited if measurements show it.
- **Units:** a series records its unit from its first point. A different unit later isn't flagged.
- **No retention job:** PostgreSQL metric data isn't yet deleted after the planned 30 days.
- **Consumer monitoring:** the storage consumer has no health endpoint, and consumer lag isn't yet exported (hardening slice).
- **Topic config:** `ii kafka init` doesn't reconcile the settings of topics that already exist.
- **Pepper rotation:** rotating the API-key pepper isn't supported, because hashes aren't versioned yet.
- **Row-level security:** tenant isolation is enforced in the application layer and by composite foreign keys. PostgreSQL row-level security is planned for the hardening slice.
- **CI:** there's no CI workflow yet.
