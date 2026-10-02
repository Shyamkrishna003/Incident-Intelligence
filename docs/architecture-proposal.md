# Architecture Proposal — Incident Intelligence

**Status:** Approved (revision 2), 2026-09-27. Slice 1 is implemented (see §13).
**Sources:** `PRD.md` (product), `CLAUDE.md` (engineering rules).

This remains the working architecture document until it is split into `docs/architecture.md` and per-decision ADRs.

---

## 0. Decision log

| # | Decision | Status |
|---|---|---|
| D1 | Modular monolith: one Python package, run as `api` + worker/consumer processes, plus a React SPA | Accepted |
| D2 | Backend: FastAPI, Pydantic v2, async SQLAlchemy 2.0 + asyncpg, Alembic, PostgreSQL 17 (pgvector image) | Accepted |
| D3 | Frontend: React + strict TypeScript + Vite, TanStack Query, React Router, Tailwind, Recharts | Accepted |
| D4 | **Kafka is in the MVP.** It is the durable telemetry pipeline between the ingestion API and its consumers (storage writer, detection). KRaft single broker locally. | **Accepted (user)** |
| D5 | **Redis is in the MVP.** Uses: ingestion rate limiting, idempotency-key replay cache, API-key verification cache, and later detector state and locks. Cache only, never the source of truth. | **Accepted (user)** |
| D6 | **Firebase Authentication** for human sign-in: email/password, Google, and GitHub. Firebase provides *identity only*. Authorization (orgs, projects, roles) stays in PostgreSQL. | **Accepted (user)** |
| D7 | **Ollama (local)** as the LLM provider for the testing phase, behind an `LLMProvider` interface. Hugging Face deferred. | **Accepted (user), pending model selection** |
| D8 | Python environment: `venv` at `<repo>/.venv` (git-ignored). pip-tools lock files for reproducibility. No `uv`. | **Accepted (user)** |
| D9 | Long-running jobs (investigations, retention) use a PostgreSQL job table. RabbitMQ/Celery deferred. | Accepted |
| D10 | Project API keys (machine credentials for ingestion) are separate from Firebase user identity | Accepted |

---

## 1. Repository assessment

| Item | State |
|---|---|
| Source code / tests / CI / Docker | None |
| Git | Initialized on `main`, no commits yet |
| Secrets / `.env` files | None present |
| Docs | `PRD.md`, `CLAUDE.md`, this proposal |
| Toolchain | Python 3.12.3 (venv works), Node 24.14, Docker 29.8 + Compose v5.5, `psql` client |
| Hardware | 16 cores, 14 GiB RAM, NVIDIA RTX 4050 Laptop (6 GiB VRAM), ~35 GB free disk |
| Not installed | Ollama, Java (needed by the Firebase emulator; we run it in a container instead) |

### PRD and CLAUDE.md inconsistencies, and how they're resolved

1. **Kafka/Redis in the MVP vs "only when justified".** Resolved by D4/D5, which give each a concrete MVP responsibility (§5, §8).
2. **Angular or React.** → React (D3).
3. **Anomaly `confidence = 0.94`.** Detectors emit a documented `score` and an ordinal `severity`. "Confidence" isn't used until calibration exists and has been evaluated.
4. **The PRD entity list is missing tables the workflow needs.** Added in §4.
5. **"Historical incidents" in the MVP vs pgvector in V1.5.** In the MVP this means filterable past incidents with their RCA and feedback. Similarity search comes later.
6. **No telemetry source for development.** A scenario simulator sends synthetic telemetry through the real ingestion API, always labelled synthetic.
7. **The PRD pairs Kafka with RabbitMQ/Celery.** Only Kafka is adopted now (D9, §8).

---

## 2. Stack

| Concern | Choice | Reason |
|---|---|---|
| Backend | FastAPI, Pydantic v2, SQLAlchemy 2.0 async + asyncpg, Alembic | CLAUDE.md direction. Async suits I/O-bound ingestion, Kafka, and LLM calls. |
| Frontend | React + TS (strict) + Vite, TanStack Query, React Router, Tailwind, Recharts | A single-page app, no SSR needed. Explicit loading, error, and stale states. |
| Database | PostgreSQL 17 (`pgvector/pgvector:pg17`) | Source of truth. The image is ready for V1.5 vector search. |
| Event streaming | Apache Kafka, official `apache/kafka` image, KRaft mode, single broker locally. Client: `confluent-kafka` (librdkafka) behind a `TelemetryPublisher` / consumer interface. | Durable, replayable, multi-consumer telemetry stream. librdkafka's idempotent producer and mature consumer semantics. |
| Cache / fast state | Redis 7, `redis-py` asyncio client | Rate limits, idempotency cache, auth cache. No persistence needed. |
| Auth (humans) | Firebase Authentication (JS SDK in the SPA, `firebase-admin` in the API). Firebase Auth Emulator for local dev and tests. | Three sign-in methods with little custom security code. |
| Auth (machines) | Project-scoped API keys: HMAC-SHA256 with a server pepper, prefix lookup, scopes | Ingestion clients aren't humans. Stays independent of Firebase. |
| LLM | Ollama on the host (GPU), via its HTTP API with JSON-schema structured output | Free, local, and incident data never leaves the machine (§7). |
| Background jobs | A worker process polling a Postgres `jobs` table (`FOR UPDATE SKIP LOCKED`) | Delayed retries of long LLM jobs, without a second broker. |
| Logging / metrics | structlog JSON, `prometheus-client` `/metrics` | |
| Python tooling | `.venv` at repo root. `pyproject.toml` + pip-tools lock files (`requirements.lock`, `requirements-dev.lock`). ruff, mypy `--strict`, pytest + pytest-asyncio + httpx. | Assumption: pip-tools for pinned, reproducible installs, also used by the Docker image. |
| Frontend tooling | ESLint, `tsc --noEmit`, Vitest + React Testing Library. Playwright later. | |
| Agent orchestration | A hand-written explicit state machine. LangGraph evaluated in V1.5. | The v1 flow is linear. LangGraph pays off once tool selection, branching, and checkpointing arrive. |

### Why a modular monolith

There's one developer, one database, and no independent scaling need yet. Each module owns its tables and exposes a service interface. The same package runs as several process types:

- `api`: FastAPI.
- `storage-consumer`: Kafka → Postgres.
- `detection-consumer`: Kafka → anomalies/incidents.
- `worker`: the Postgres job table for investigations and retention.

In local dev these can share one container image, with different commands.

---

## 3. Modules

```
backend/src/incident_intel/
  core/           config, logging (+ redaction), errors, request context, clock
  db/             engine, session factory, base model, tenant-scoping helpers
  cache/          Redis client, rate limiter, idempotency store, small typed cache helpers
  streaming/      Kafka producer/consumer wrappers, envelopes, topic registry, DLQ publisher
  tenancy/        organizations, projects, users, memberships, roles, API keys, auth context
  auth/           Firebase ID-token verification (interface + firebase-admin impl + fake)
  ingestion/      request schemas, validation, normalization, publish to Kafka
  telemetry/      storage consumer (Kafka → Postgres) and scoped read queries
  detection/      detector interface, baseline detectors, detection consumer, anomalies
  incidents/      correlation rules, incident lifecycle, timeline events
  investigation/  orchestrator, bounded tools, evidence snapshots, LLM provider, report schemas/validation
  learning/       feedback, learning records, evaluation cases and runner
  jobs/           Postgres job queue and worker loop
  audit/          audit log writer
  api/            thin routers, dependencies, error mapping
  cli.py          bootstrap, API-key admin, topic setup, DLQ inspect/replay, eval runner, simulator
```

---

## 4. Data model and tenancy

### Tenancy

- `organization → project → {services, telemetry, incidents, …}`.
- `users` are global. Each row is keyed by our own UUID and stores `firebase_uid` (unique), email, and `email_verified`. `memberships(user_id, organization_id, role ∈ owner|admin|member|viewer)`. Per-project roles are deferred.
- **Firebase custom claims are not used for authorization.** They can be up to 1 hour stale and would split the source of truth. Removing a membership in Postgres takes effect on the next request.
- API keys belong to a project and carry scopes (`ingest:write`, `telemetry:read`).
- Every tenant-owned row has `organization_id` and `project_id`.

### Enforcement layers

1. **Application:** a FastAPI dependency resolves `TenantContext(org_id, project_id, actor, role|scopes)`. Repositories require it, so there are no unscoped helpers. A cross-tenant lookup returns **404**.
2. **Kafka:** tenant ids in the message envelope are set by the API **from the authenticated API key**, never from the request body. Consumers use only envelope tenant ids.
3. **Schema:** composite FKs such as `(project_id, service_id) → services(project_id, id)` prevent cross-project references.
4. **Database RLS:** added in the hardening slice as defense in depth.
5. **Tests:** a cross-tenant test for every data endpoint and consumer.

### Tables (introduced by slice)

- **Slice 1:**
  - `organizations`
  - `projects (org_id, slug unique per org)`
  - `api_keys (project_id, name, key_prefix unique, key_hash, scopes[], created_by, last_used_at, expires_at, revoked_at)`
  - `audit_logs (org_id, project_id?, actor_type, actor_id, action, target_type, target_id, metadata, request_id, created_at)`
- **Slice 2:**
  - `services (project_id, name unique per project)`
  - `ingest_batches (project_id, batch_id, idempotency_key, body_sha256, point_count, received_at, stored_at)` with UNIQUE `(project_id, idempotency_key)`
  - `metric_series (project_id, service_id, name, unit, attributes, attributes_hash)`
  - `metric_points (series_id, ts, value)`, PK `(series_id, ts)`
- **Slice 3:** `users`, `memberships`
- **Slice 4:** `log_records`, `deployments`, `service_dependencies`
- **Slice 5+:**
  - `anomalies`, `incidents`, `incident_events`
  - `jobs`, `agent_runs`, `agent_steps`
  - `evidence` (immutable snapshots), `rca_reports`, `report_claims`, `claim_evidence`
  - `feedback`, `learning_records`
  - `evaluation_cases`, `evaluation_runs`, `evaluation_results`
  - `models` / `model_versions` in V1.5

Column details for the later tables are unchanged from revision 1. They'll be finalized in each slice's plan.

### Retention (configurable)

- Raw metric points: 30 days. Logs: 7 days.
- Evidence snapshots, reports, feedback, and learning records: kept for the project's lifetime.
- Kafka topic retention: 3 days. Enough to replay after a consumer outage, and not a long-term store.

---

## 5. Telemetry ingestion and event lifecycle (Kafka + Redis)

```
client ─POST /v1/ingest/metrics (Bearer API key, Idempotency-Key)─▶ api
  1. authenticate key: Redis cache (60s TTL) → fallback Postgres; constant-time hash compare; scope check
  2. Redis rate limit per key → 429 + Retry-After when exceeded
  3. body limit + Pydantic validation + normalization (UTC, names, attributes hash)
  4. Redis idempotency check: SET NX idem:{project}:{key} → {body_sha256, batch_id}, TTL 24h
       - replay, same hash  → 202 with original batch_id
       - replay, other hash → 409
  5. produce envelope to Kafka `telemetry.metrics.v1`, key = "{project_id}:{service}"
     (acks=all, enable.idempotence=true); wait for broker ack
  6. 202 Accepted {batch_id, accepted}     ← durable in Kafka, not yet queryable

Kafka ─▶ consumer group `storage`   → Postgres (ingest_batches + series upsert + points), then commit offset
      └▶ consumer group `detection` → detectors → anomalies/incidents (slice 5)
```

### Envelope (versioned Pydantic model, JSON-encoded)

```json
{"schema": "telemetry.metrics.v1", "batch_id": "…", "organization_id": "…", "project_id": "…",
 "api_key_id": "…", "received_at": "…", "idempotency_key": "…", "body_sha256": "…",
 "points": [{"service": "payment-api", "metric": "http.server.duration.p95", "unit": "ms",
             "ts": "…", "value": 350.0, "attributes": {"region": "eu-west-1"}}]}
```

The Kafka message key is `project_id:service`, so all points for one service stay in order within one partition. That ordering matters for stateful detectors.

### Delivery guarantees

- **Client → API: at least once** (clients retry on 5xx or timeout).
- **API → Kafka:** a 202 is returned **only after** the broker acknowledges with `acks=all`. The idempotent producer prevents duplicates caused by producer retries.
- **Kafka → Postgres: at least once**, made **effectively once** by:
  - `ingest_batches` UNIQUE `(project_id, idempotency_key)`. If the batch already exists, the consumer skips it.
  - `metric_points` PK `(series_id, ts)` with `ON CONFLICT DO NOTHING` (first write wins, documented).
  - The offset is committed only after the DB transaction commits.
- **Read-after-write is eventual.** Data becomes queryable after the storage consumer processes it, typically well under a second locally. Tests poll with a timeout.

### Failure behavior

| Failure | Behavior |
|---|---|
| Kafka unavailable at produce | 503 + `Retry-After`. The idempotency key is released so the retry isn't mistaken for a duplicate. Nothing is silently dropped. |
| Redis unavailable | Auth falls back to Postgres. Idempotency falls back to the consumer's durable dedupe, so no duplicate rows. The rate limiter **fails open** with a warning log and metric (configurable to fail closed). `/readyz` reports degraded. |
| Postgres unavailable (consumer) | No offset commit. Retry with backoff and pause the partition. Messages wait in Kafka. |
| Poison message (bad schema version or undecodable) | Published to `telemetry.metrics.v1.dlq` with error headers, then the offset is committed. A CLI can inspect and replay. |
| Consumer crash | Its partitions are reassigned. Re-delivered messages are deduped as above. |

### Observability

- Produce latency and errors.
- Consumer lag per group and partition.
- DLQ count.
- Rate-limit rejections.
- Idempotency hits.

### Security

- Locally, Kafka is PLAINTEXT on the Docker network, published only to `localhost`. SASL/TLS and ACLs arrive with deployment.
- Only the API produces to telemetry topics.
- The envelope schema is enforced on both sides.

### Formats and topics

- The MVP uses a simple JSON ingestion format. An OTLP/HTTP adapter comes in V1.5 and feeds the same normalization layer.
- Topic creation is explicit: the CLI's `ii kafka init` sets partitions and retention. Broker auto-create is disabled.

---

## 6. Detection and correlation

- **Detection runs as a Kafka consumer group (`detection`)** on the same topic, independent of storage.
  - Detectors are pure functions: `(series window, state, config) → candidates, new state`. Each carries a name and version.
  - Per-series detector state (EWMA, rolling window summary) lives in Redis for speed. It can be rebuilt from Postgres history if Redis is flushed.
  - A short Redis lock per series prevents double processing during rebalances.
- **Baseline detectors:**
  - Static thresholds.
  - Rolling robust z-score (median/MAD).
  - EWMA deviation.
  - Severity comes from a documented rule, and open anomalies are extended rather than duplicated.
- **Evaluation first:**
  - Labelled synthetic scenarios, measured by precision, recall, false-positive rate, and time-to-detect.
  - ML detectors (for example Isolation Forest) come in only as challengers that must beat this baseline.
- **Correlation v1 (explainable rules):**
  - The same service or a direct dependency, within T minutes (default 15) of the incident's last activity, attaches to that incident. Otherwise a new incident is created.
  - Every decision is written to `incident_events` with the rule and its inputs.
  - Recent deployments are attached as *candidate* evidence, never as a presumed cause.

---

## 7. Controlled AI investigation (Ollama)

### Why Ollama, not Hugging Face, for the testing phase

| | Ollama (local) | Hugging Face Inference (hosted) |
|---|---|---|
| Cost | Free. Only your electricity and disk. | Free tier is a small monthly credit with rate limits, which a development loop of repeated runs quickly exhausts |
| Data | Incident telemetry never leaves the machine | Telemetry is sent to a third party |
| Structured output | JSON-schema `format` parameter and tool calling | Varies by provider and model |
| Offline / CI determinism | Works offline. CI still uses a fake provider. | Needs network and a token |
| Quality | Limited by what fits in 6 GiB VRAM (≈7–8B params, 4-bit) | Can reach larger models |

Ollama is sufficient **because of the design**, not despite it:
- The model never fetches data or acts.
- Code collects the evidence.
- The model only synthesizes from a small, labelled bundle.
- Code validates every citation.

Small models do make more reasoning mistakes. The evaluation suite (slice 8) measures that instead of assuming it. Switching to a hosted model later is a config change behind `LLMProvider`.

**Model selection (slice 7):**
- Shortlist two or three ~7–8B instruct models that support structured output (for example the current Qwen and Llama 8B-class instruct models at 4-bit quantization, ≈5 GB each).
- Run the same recorded evidence bundles through each.
- Pick the one with the best schema validity and citation correctness.
- Record the choice and its scores in an ADR.
- Ollama runs **natively on the host** so it gets the GPU without configuring Docker GPU passthrough. Containers reach it via `host.docker.internal:11434`.

### Investigation v1 flow

1. **Context:** incident, anomalies, affected services, dependencies.
2. **Evidence collection:** deterministic, no LLM. Metric windows with baselines, recent deployments, top error-log excerpts, 1-hop dependencies. Each is stored as an immutable `evidence` snapshot with an id.
3. **Hypothesis step (LLM):**
   - Evidence is delimited as **untrusted data**, and secrets/PII are redacted first.
   - The output must match a Pydantic JSON schema: facts, hypotheses, unknowns, conflicts, next steps, each citing evidence ids.
4. **Validation (code):**
   - Schema check.
   - Each cited id must exist in this run's bundle.
   - An uncited "fact" is downgraded or rejected.
   - One repair retry, then the run fails visibly.
5. **Verification step (LLM + rules):** tries to disprove the leading hypothesis (for example "latency rose before the deployment") and marks contradicted hypotheses.
6. **Persist** the report, claims, claim↔evidence links, and all `agent_steps`: inputs, output summaries, durations, errors, and token counts.

Investigation runs are **jobs in the Postgres job table**, triggered when an incident is created or updated. They get delayed retries and a dead state, and they avoid blocking a Kafka partition behind a long LLM call.

**Assessment language** is ordinal (`supported | weak | contradicted | untested`) with a rationale. No probabilities.

**V1.5:** the LLM-driven planner picks from allowlisted tools such as `get_metric_range` and `get_recent_logs`.
- Tenant scope is injected by the orchestrator.
- Arguments are validated and bounded.
- There's a step/token budget per run.
- LangGraph is evaluated at this point.

---

## 8. Infrastructure placement

| Component | MVP responsibility | Failure behavior | Operational cost |
|---|---|---|---|
| **PostgreSQL** | Source of truth, telemetry store, job queue | API readiness fails. Consumers pause (data waits in Kafka). | 1 container |
| **Kafka** | Durable telemetry stream between the API and independent consumers (storage, detection). Replay after consumer outages. Later: OTLP and domain-event topics. | API returns 503 (clients retry). Consumers resume from committed offsets. | 1 KRaft broker. Cap the JVM heap at ~512 MB locally to leave RAM for Ollama. |
| **Redis** | Rate limiting, idempotency cache, API-key cache, detector state, short locks | Degrades gracefully (§5). Nothing lives only in Redis. | 1 small container, no persistence |
| **Firebase Auth** | Identity for email/password, Google, and GitHub | If Firebase public keys can't be fetched and aren't cached, user sign-in fails. API-key ingestion is unaffected. | Hosted. No cost for these three providers at this scale. The emulator is used locally. |
| **Ollama** | Local LLM inference | Investigation fails visibly. Incidents and evidence stay usable. | ~5 GB disk per model, most of the 6 GiB VRAM |
| **RabbitMQ + Celery** | — | — | **Deferred.** The Postgres job table covers the MVP. Revisit if job variety, routing, or scheduling outgrow it. |
| **Prometheus / Grafana** | Monitoring the platform itself | — | `/metrics` now. Containers in the hardening slice. |
| **Kubernetes** | — | — | Deferred (V2) |

---

## 9. Authentication with Firebase (slice 3)

**Frontend:**
- Firebase JS SDK with `EmailAuthProvider` (email verification required before joining an org), `GoogleAuthProvider`, and `GithubAuthProvider`.
- The Firebase web config is a public identifier, not a secret. It's protected by authorized domains.
- The SPA sends `Authorization: Bearer <Firebase ID token>`. The SDK refreshes it hourly.

**Backend:**
- `firebase-admin` `verify_id_token()` checks signature, audience, issuer, and expiry against Google's cached public keys.
- **Only the Firebase project ID is needed.** No service-account key is required for verification (least privilege).
- Verification runs in a threadpool so it doesn't block the event loop.
- On first request the user is provisioned just in time into `users` by `firebase_uid`.

**Authorization:**
- Roles and memberships live in Postgres (§4).
- Sign-out plus membership removal takes effect immediately for authorization. An ID token alone grants nothing without a membership.
- Explicit token revocation checks (`check_revoked`) are deferred. They cost a network call per request.

**Account linking:**
- Firebase's default "one account per email" setting means a GitHub sign-in with an email already used via Google returns `auth/account-exists-with-different-credential`.
- The SPA handles this with an explicit linking flow. That flow is slice-3 work.

**Local dev and tests:**
- The Firebase Auth Emulator runs in a container (it needs Java, so it's kept off the host). It supports email/password and simulated Google/GitHub sign-in.
- The backend uses it when `FIREBASE_AUTH_EMULATOR_HOST` is set.
- Unit tests use a fake `TokenVerifier`. A few integration tests hit the emulator.

**What you'll need to do before slice 3** (not before):
1. Create a Firebase project.
2. Enable the three providers.
3. Create a GitHub OAuth App, with its callback URL taken from the Firebase console.
4. Put the GitHub client secret **only in the Firebase console**, never in this repo.

---

## 10. Local development, security, observability, testing

**Local dev:**
- `docker compose up` starts `postgres`, `redis`, `kafka`, `api`, `storage-consumer`, and later `detection-consumer`, `worker`, `firebase-emulator`, and `web`. Each service is added in the slice that needs it.
- Ollama runs on the host.
- Python dev uses `.venv`. The `Makefile` targets (`venv`, `up`, `migrate`, `test`, `lint`, `typecheck`, `fmt`, `bootstrap`, `kafka-init`) use `.venv/bin/*`.

**Security:**
- API keys: 256-bit random, shown once, stored as an HMAC with a pepper from env.
- Redaction of `Authorization`, API keys, and tokens in logs.
- An audit log for sensitive operations.
- RBAC in services.
- Webhook signatures verified when GitHub integration arrives.

**Observability:**
- A request_id propagated into Kafka headers and jobs.
- Tenant ids in logs.
- Prometheus metrics for ingest, lag, DLQ, jobs, and LLM calls.
- OpenTelemetry for the platform itself in the hardening slice.

**Testing:**
- Unit tests are pure: normalization, detectors, correlation, report validation, envelope schemas.
- Integration tests use real Postgres, Redis, and Kafka from compose.
- API tests use httpx.
- Tenant-isolation tests throughout.
- The LLM and Firebase are faked in CI. Real-model evaluations are a separate, explicit command.

---

## 11. Implementation sequence

1. **Foundation + tenancy primitives.** Postgres only (detailed in §12).
2. **Ingestion pipeline.** Kafka + Redis introduced. Metric ingestion API → Kafka → storage consumer → Postgres. Idempotency, rate limiting, DLQ, read endpoints, isolation tests.
3. **Human auth + frontend shell.** Firebase (emulator), users and memberships, React app with login (3 methods), org/project switcher, services and metric chart.
4. **Logs + deployments ingestion, service dependencies, scenario simulator.**
5. **Detection consumer.** Baseline detectors, anomalies, and the detection evaluation suite.
6. **Correlation → incidents.** Timeline, and the incident list/detail UI.
7. **Investigation v1.** Ollama provider, model selection ADR, evidence snapshots, validation, verification, job table and worker, evidence/claims UI.
8. **Feedback, learning records, evaluation cases and runner, RCA-accuracy metric.**
9. **Hardening.** RLS, retention jobs, Kafka SASL/TLS, Prometheus/Grafana, OTel, deployment, CI polish.
10. **V1.5, gated on measurement.** pgvector similar incidents, GitHub integration, the LLM tool-selecting planner (LangGraph evaluation), ML detector challengers, OTLP ingestion, Celery if needed.

---

## 12. Slice 1 — Foundation + tenancy primitives

**Goal:**
- A runnable, tested backend skeleton with config, structured logging, Postgres, and migrations.
- Organizations, projects, and hashed project API keys created through a CLI.
- An authenticated, project-scoped endpoint proving the tenant context works.

This gives slice 2 (Kafka/Redis ingestion) a verified auth and tenancy base.

Slice 1 is kept free of Kafka/Redis on purpose. Each gets introduced, tested, and explained in slice 2, where it has a job.

### Files

```
.gitignore  .env.example  README.md  Makefile  docker-compose.yml     # compose: postgres + api
backend/
  pyproject.toml  requirements.lock  requirements-dev.lock  Dockerfile  alembic.ini
  migrations/env.py
  migrations/versions/0001_tenancy_foundation.py
  src/incident_intel/
    main.py                 # app factory, request-id + body-limit middleware, routers
    core/config.py          # pydantic-settings (DATABASE_URL, API_KEY_PEPPER, LOG_LEVEL, …)
    core/logging.py         # structlog JSON + secret redaction processor
    core/errors.py          # domain errors → HTTP responses
    db/session.py  db/base.py
    api/health.py           # /healthz, /readyz
    api/deps.py             # DB session, TenantContext from API key
    tenancy/models.py       # Organization, Project, ApiKey
    tenancy/api_keys.py     # generate / hash / verify / revoke
    tenancy/service.py      # create org, project, key (with audit)
    tenancy/router.py       # GET /v1/project
    audit/models.py  audit/service.py
    cli.py                  # ii bootstrap | ii api-keys create | ii api-keys revoke
  tests/
    conftest.py
    unit/test_api_keys.py  unit/test_config.py  unit/test_log_redaction.py
    integration/test_health.py  integration/test_api_key_auth.py
    integration/test_tenant_isolation.py  integration/test_migrations.py
```

### Endpoints

| Method | Path | Auth | Behavior |
|---|---|---|---|
| GET | `/healthz` | none | 200 when the process is alive |
| GET | `/readyz` | none | 200 if DB reachable and migrations at head. Otherwise 503 with the failing check named. |
| GET | `/v1/project` | Bearer API key (any valid scope) | The key's org/project ids, names, and key scopes |

### Migration 0001

`organizations`, `projects`, `api_keys`, and `audit_logs`, with unique constraints and FK indexes. The downgrade is tested.

### Acceptance criteria

1. `make venv && make up && make migrate`. `/healthz` returns 200. `/readyz` returns 200, or 503 before migrating or with the DB stopped.
2. `ii bootstrap --org acme --project payments` creates the org, project, and an API key. The plaintext is printed once. Only the prefix and HMAC are stored. The action is audit-logged.
3. `GET /v1/project` with that key returns project `payments`. A missing, malformed, unknown, revoked, or expired key → 401 with a generic message.
4. With two bootstrapped tenants, each key sees only its own project.
5. API keys and `Authorization` headers never appear in logs, and a test asserts this.
6. `make lint`, `make typecheck`, and `make test` pass. The migration up/down test passes.

### Out of scope for slice 1

- Kafka, Redis, and ingestion (slice 2).
- Firebase, users, and the frontend (slice 3).
- Everything later in the sequence.
- CI workflow, until a GitHub remote is confirmed.

---

## 13. Slice 1 — implementation record

Implemented as planned in §12. Deviations and details decided during implementation:

| Planned | Implemented | Why |
|---|---|---|
| `backend/migrations/` | `backend/src/incident_intel/migrations/` (`script_location = incident_intel:migrations`) | The API resolves the expected migration head for `/readyz` without depending on the working directory or on files outside the installed package. |
| Body-size-limit middleware in slice 1 | Deferred to slice 2 | Slice 1 has no endpoint that accepts a body. The limit gets built and tested with ingestion. |
| Migration generated with autogenerate | Hand-written, then verified | Docker wasn't usable at implementation time. `tests/integration/test_migrations.py` asserts that the models and migrations produce an identical schema (Alembic `compare_metadata`) and that downgrade/upgrade round-trips. |
| — | `pytest` treats warnings as errors | Library deprecations (for example in SQLAlchemy 2.1) surface immediately. |
| — | `ii bootstrap` validates all slugs before writing | Each step commits, so late validation would leave an organization without a project. |
| CI workflow | Not added | Waiting on a GitHub remote. |

Pinned versions at implementation: FastAPI 0.141, Starlette 1.7, SQLAlchemy 2.1, Pydantic 2.13, Alembic 1.20, structlog 26.1, pytest 9.1, pytest-asyncio 1.4, mypy 2.3, ruff 0.16.

---

## 14. Slice 2a (Kafka ingestion pipeline): implementation record

Implemented as approved. Decisions and deviations made during implementation:

| Planned / assumed | Implemented | Why |
|---|---|---|
| Kafka message key `project_id:service` (§5) | Key = `project_id`, **one message per batch** | A batch can span several services. One message keeps a batch atomic (one produce, one ack, one DB transaction) and still orders each series. The cost is that one large tenant uses one partition at a time. Revisit if measured. |
| `ingest_batches` unique `(project_id, idempotency_key)` | `batch_id = uuid5(project_id, key)` as the primary key | Deterministic ids give retries the same `batch_id` before Redis exists. The primary key is the dedupe point. |
| `body_sha256` of the raw request | `content_sha256` of the **normalized** points | Formatting differences in a retried request don't create false conflicts. |
| — | `metric_points` carries `project_id` only (not `organization_id`) | It's the highest-volume table. The series carries the rest of the tenant chain, and a composite FK enforces project consistency. |
| — | Values must be JSON numbers (strict) | Lax parsing accepted `"350"` as 350.0. A test caught it. |
| — | 422s from batch rules use the same `validation_error` code as schema errors | Clients handle one kind of 422. |
| DB outage surfaced as 500 | `OSError` and SQLAlchemy `OperationalError`/`InterfaceError` → **503 + Retry-After** | Found during the Docker outage test. Unexpected errors are now answered by the request-context middleware, so 500s carry a request id too. |
| Migrations run by hand in Docker | One-shot `migrate` and `kafka-init` compose services; `api` and `storage-consumer` wait for both | `make up` gives a working stack from scratch. |
| — | The storage consumer's inherited HTTP health check is disabled | Consumers serve no HTTP. Consumer lag (hardening slice) is the right signal. |
| — | Worker processes load `RuntimeSettings` (no API-key pepper) | Least privilege: only the API holds the pepper. |
| `examples/metrics.json` | `examples/send-metrics.sh` | The API accepts only recent timestamps, so a static file would go stale. |

Verified on the Docker stack:
- ingest, then read back
- replay stored once
- PostgreSQL outage: API 503, consumer retries without committing, then stores on recovery
- poison message dead-lettered without blocking the following batch
- Kafka outage: API 503 within ~5 s; retry after recovery accepted
- topics survive a broker restart

Pinned: `confluent-kafka` 2.15.1 (librdkafka 2.15.1), image `apache/kafka:4.3.1`.
