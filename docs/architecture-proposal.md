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

---

## 15. Slice 2b (Redis): implementation record

Redis now does three jobs at the API edge. It is never the source of truth and runs without persistence.

| Job | How | When Redis is down |
|---|---|---|
| Rate limiting per API key | Fixed-window counter (`INCR` + `EXPIRE` in one `MULTI`), key `ii:rl:<api key id>:<window>`. Default 600 requests per 60 s. Over the limit → 429 with `Retry-After`. | Requests are let through (`RATE_LIMIT_FAIL_OPEN=false` answers 503 instead). |
| Idempotency claims | `SET NX` of `<content hash>:pending` under `ii:idem:<project>:<key>` for 24 h, switched to `published` after Kafka acknowledges. Different content → 409. A retry of a `published` batch returns the same `batch_id` without publishing. | The check is skipped. The storage consumer still stores each batch once and dead-letters conflicts. |
| API-key cache | The verified key record (HMAC, tenant ids, scopes, expiry) under `ii:apikey:<prefix>` for 60 s. Only active keys are cached. Revocation deletes the entry. | Authentication uses PostgreSQL. |

Decisions made during implementation:

| Decision | Why |
|---|---|
| A retry of a `pending` claim publishes again | The first attempt may have died before reaching Kafka. Skipping the publish there could lose data; a duplicate message is harmless because the consumer dedupes. Only a Kafka-acknowledged batch is skipped on retry. |
| The claim is made after validation | A batch rejected with 422 doesn't use up its Idempotency-Key. |
| `RedisGateway` with a circuit breaker | One error type for callers, and after a failure Redis is skipped for 5 s, so an outage costs one short timeout (0.25 s) instead of one per request. Client-side retries are off. |
| `/readyz` reports Redis but doesn't require it | Every use has a fallback, so an outage must not take the API out of rotation. |
| Fixed window instead of a token bucket | No Lua script, easy to reason about. It allows up to 2x the limit across a window boundary, which is acceptable for flood protection. |
| Rate limit applied after authentication, per key | One key can't use up another's budget. Unauthenticated floods aren't limited yet (a per-IP limit belongs at the edge, in the hardening slice). |
| Revocation invalidates the cache from the CLI | Immediate when Redis is reachable. Otherwise the CLI warns that the key may work for up to the cache TTL. |
| In-memory `FakeCache` plus a contract test suite | Most tests need no Redis. The same behavioral tests run against the fake and real Redis, so the fake can't drift. |
| Kafka end-to-end tests run with the cache "unavailable" | They prove the pipeline's own guarantees hold without the API-edge check. |
| Dockerfile: pip cache mount, longer timeouts | Image builds failed on a slow connection to PyPI. |

Verified on the running stack:
- a retried batch reaches Kafka once; different data under the same key gets 409
- the ninth request against a limit of 8 gets 429 with `Retry-After`
- a cached key is rejected immediately after `ii api-keys revoke`
- with Redis stopped: requests and ingestion succeed in milliseconds, `/readyz` stays ready and reports `redis: unavailable`, and the CLI warns on revoke
- after Redis restarts, the API recovers on its own (`redis_unavailable` and `redis_recovered` are each logged once)

Pinned: `redis` (Python client) 8.1.0, image `redis:8-alpine` (8.10.2 at implementation).

---

## 16. Slice 3a (users, sign-in, roles): implementation record

People can now call the API. Firebase Authentication proves identity; PostgreSQL decides authorization.

- **Migration `0003`:** `users` (Firebase UID, email, verified flag, display name) and `memberships` (organization, user, role).
- **`auth/tokens.py`:** a `TokenVerifier` interface with a Firebase implementation (official `firebase-admin` SDK), a disabled one (no project ID configured), and a fake for tests.
- **`tenancy/access.py`:** `authorize_organization` and `authorize_project` check the caller's membership and role. Non-members get 404; members with too low a role get 403.
- **`tenancy/console_router.py`** and a second telemetry router: the user endpoints under `/v1/me`, `/v1/organizations` and `/v1/projects/{project_id}`.
- **`TenantScope`:** the organization and project a request may touch. API-key requests and user requests both produce one, and the telemetry queries accept either.

Decisions made during implementation:

| Decision | Why |
|---|---|
| Any user with a verified email may create an organization; invitations are left out | The recommended options from the plan. Each user may own at most 10 organizations, as an abuse limit. |
| The SDK is given an explicit anonymous credential | Verifying a token needs only Google's public keys, but the SDK otherwise looks for Google application credentials and fails slowly without them. Found by a test. |
| Authorization never uses Firebase custom claims | They can be up to an hour stale. Reading memberships from PostgreSQL on each request makes role changes immediate. |
| Separate endpoints for API keys and users | A credential of one kind is never accepted where the other is expected, so there is no ambiguity about what a bearer token is. |
| `revoke_api_key` takes an optional `project_id` | A user acting on one project can't revoke another project's key by guessing its prefix. |
| The organization and its owner membership are created in one transaction | A user-created organization can't be left without an owner. |
| The user row is written only on first sign-in or when the token's profile fields change | No database write on ordinary requests. |
| Emulator mode is refused when `ENVIRONMENT=production` | The emulator accepts unsigned tokens and doesn't check expiry. |
| The emulator is an optional Compose profile | The image is large, and a real Firebase project is available for development. |

Costs:
- `firebase-admin` adds 31 packages to the runtime lock (Google API, gRPC and Firestore clients come with it), which makes the image larger and its first build slower.

Verified:
- every role against every user endpoint, and 404 for other organizations' resources
- a key created through the API works as a machine credential and stops working when revoked
- against the real Firebase project ID: a forged token with the right audience and issuer is rejected after the SDK fetches Google's public keys (about 0.25 s once, then about 3 ms from cache); no user row is created and the token doesn't appear in logs

Pinned: `firebase-admin` 7.7.0, `firebase-tools` 15.32.1 (emulator image).

---

## 17. Slice 3b (web app): implementation record

A React single-page app in `frontend/`: React 19, TypeScript (strict), Vite, Tailwind CSS, TanStack Query, React Router, Recharts and the Firebase JS SDK.

- **Auth:** `auth/AuthContext.tsx` wraps Firebase Authentication. The API client asks it for the current ID token on every request and retries once with a fresh token after a 401.
- **Data:** `hooks/queries.ts` holds every query and mutation. Cache keys include the user id, and the cache is cleared on sign-out.
- **Screens:** sign-in, setup, services, service (chart), API keys. Each handles loading, empty and error states.
- **Backend addition:** `GET .../services/{service}/metrics` lists a service's metrics. The chart page needs it to offer a metric picker.

Decisions made during implementation:

| Decision | Why |
|---|---|
| One `.env` for the repo; Vite reads it with `envDir: ".."` | Vite exposes only `VITE_`-prefixed variables to the browser, so backend secrets in the same file stay out of the bundle. |
| The dev server proxies `/v1` to the API | The browser sees one origin, so the API needs no CORS configuration. |
| Account linking waits for the user to sign in the original way | Firebase no longer reveals which sign-in method an email uses (email enumeration protection), so the app can't pick the method for them. |
| Email verification is a step in setup, for every sign-in method | The API requires a verified email to create an organization, and Firebase doesn't treat GitHub emails as verified. |
| Role checks in the app only hide controls | The API enforces roles. A user who forces a hidden page gets a 403 or 404 from the server. |
| The services and metrics lists refetch on every visit | New services appear when they first send data, outside the app. Found in the browser run: a cached empty list hid a service that had just started reporting. |
| The chart page is loaded on demand | The chart library is about a third of the JavaScript. |
| Chart: validated 8-colour categorical palette, 2px lines, legend for two or more series, hover readout of every series, table view | The colours pass a colour-blindness check in light and dark themes. Three light-theme colours are low-contrast against the background, so the table view is always available. |
| At most 8 series per chart | The palette has 8 validated colours. The chart says when series are left out. |

Verified in a real browser (Chrome, driven by a script, against the Firebase emulator):
- sign up, verify email, create an organization and project, create an API key
- ingest 93 points with that key, then see the service and its chart with two series
- the hover readout, dark theme, table view, and a 390px-wide phone layout with no horizontal overflow
- no failed API requests and no page errors

Not verified: sign-in with real Google and GitHub accounts, and the account-linking flow against real providers.

---

## 18. Slice 4 (logs, deployments, simulator): implementation record

The *Observe* stage now covers metrics, logs and deployments, and there is a repeatable source of test telemetry.

- **Ingestion:** `POST /v1/ingest/logs` and `POST /v1/ingest/deployments`, sharing one code path with metrics (`ingestion/service.py`). Only validation, message type and topic differ.
- **Kafka:** topics `telemetry.logs.v1` and `telemetry.deployments.v1`, each with its own dead-letter topic. One storage consumer subscribes to all three telemetry topics and routes by topic.
- **Storage (migration `0004`):** `log_records`, `deployments`, and a `kind` column on `ingest_batches`.
- **Reads:** logs per service (severity and text filters) and deployments per project, for API keys and for signed-in users.
- **Simulator:** `simulator/scenario.py` (pure functions of time), `client.py` (sender) and `runner.py` (backfill and live modes), behind `ii simulate`.
- **Web app:** logs panel and deployment markers on the service page, and a deployments page.

Decisions made during implementation:

| Decision | Why |
|---|---|
| Logs stay in PostgreSQL | As planned for the MVP. One store, tenant-scoped the same way as everything else. Revisit (for example Loki) when volume requires it. |
| Logs are deduplicated per batch only | Log lines have no natural identity, and identical lines at the same instant are legitimate. The batch claim and the records are written in one transaction, so a redelivered batch stores nothing twice. |
| Deployments are also unique on (project, service, version, time) | A retried CI job may report the same deployment under a new idempotency key. |
| Idempotency keys are scoped per kind (`logs/<key>`, `deployments/<key>`) | The same key on two endpoints must not collide. Metrics keep the bare key, so existing batch ids are unchanged. A client key can't contain `/`, so the scopes can't overlap. |
| Deployments go through Kafka too, on a single partition | The detection and correlation consumers will read them from the stream, in order. The volume is tiny. |
| The consumer's dead-letter target is a mapping from source topic | Each topic keeps its own dead-letter topic. A missing mapping entry is retried and logged, never dropped. |
| Severity is stored as an OpenTelemetry-style number | "This level and above" is a simple comparison, and OTLP ingestion can map onto it later. |
| Service dependencies are deferred to the correlation slice | Nothing uses them yet. |
| The simulator's values depend only on the seed and the timestamp | Re-running over the same period yields identical batches, which the API recognizes as retries. It also makes the scenario usable as a fixed evaluation case for detection. |
| The scenario includes an unrelated deployment and a healthy control service | Correlation must not blame the red herring, and detection must not flag the control. |
| Synthetic data is labelled at the data level (`source=simulator`) | The label travels with the data into every chart, log view and, later, every AI prompt. |
| The simulator's API key comes from an environment variable | Command-line arguments are visible to other users on the machine. |

Verified:
- on the Docker stack: a simulator run stored 3 metric batches, 1 log batch and 1 deployment batch; a second run over the same period was answered as 5 replays with nothing republished
- in a real browser against the emulator: the service page shows the latency rise starting at the 2.43.0 deployment marker with the error logs beneath it; the deployments page lists the cause and the red herring; a 390px layout has no horizontal overflow; no failed requests or page errors

A mistake during verification, recorded so it isn't repeated: the first browser run reached a dev server already running on port 5173 that was configured for the real Firebase project, and attempted a sign-up there. Test runs now use their own ports and refuse to start unless the served app is in emulator mode.

---

## 19. Slice 5 (anomaly detection): implementation record

The *Detect* stage: the system now notices abnormal metric behaviour by itself.

- **`detection/detectors.py`:** pure detectors (robust z-score, EWMA, static threshold).
- **`detection/engine.py`:** pure logic turning point-by-point judgments into anomalies that open, extend and close.
- **`detection/service.py`:** rebuilds the engine's state from PostgreSQL, runs it on the points stored since the last evaluation, and writes the result.
- **`detection/consumer.py`:** runs the service for each "metrics stored" event (consumer group `detection`).
- **`detection/evaluation.py`:** labelled synthetic scenarios and scoring, behind `ii eval detection`.
- **Migration `0005`:** `anomalies` and `detection_state`.
- **API and web app:** anomalies list, chart shading.

Decisions made during implementation:

| Decision | Why |
|---|---|
| Detection consumes a "metrics stored" event, not the raw metrics topic (a change from §6) | Reading the raw stream, detection could run before the storage consumer had written the data it needs (history, and the series row an anomaly points to). The event is published after the storage commit, and also for a redelivered batch, so detection always sees stored data and no batch goes unannounced. |
| Detector state lives in PostgreSQL, not Redis (a change from §6) | The baseline window is one indexed query per series per event. No measured need for Redis yet, and one store keeps detection deterministic and testable. Revisit if the query cost shows up. |
| The engine's state is defined so it can be rebuilt from stored rows | Evaluating a series in many small steps must give the same anomalies as one pass. A test asserts this equivalence on four scenarios. |
| A per-series "evaluated through" position | Makes detection idempotent under redelivery. The cost is that late points are not evaluated. |
| The baseline excludes points inside anomalies, and is frozen while one is open | Otherwise a sustained problem contaminates the baseline and the anomaly "ends" while things are still broken. |
| An anomaly open for 6 hours closes as "persisted" and the baseline restarts | A level that never returns is eventually the new normal. Without this a legitimate permanent change would stay flagged forever. |
| Two anomalous points in a row to open | Single-point glitches are common and not actionable. It costs one sampling interval of detection delay. |
| Default: robust z-score at threshold 6 | Chosen from the evaluation: the best recall of the candidates with no false positives. Threshold 4 raised a false alarm on the slow wave. |
| Severity is a rule on the score; the API and UI say it is not a probability | CLAUDE.md: no calibrated-looking confidence without calibration. The PRD's `confidence = 0.94` is not reproduced. |
| The static threshold exists only in the evaluation | It is the simple-rule baseline the statistical detectors must beat. User-configured thresholds need a rules model and UI, deferred. |
| One Kafka partition per project serializes a project's detection | Events are keyed by project, so two consumers never evaluate the same series at once. A partial unique index (one open anomaly per series and detector) is the backstop. |

Verified:
- on the Docker stack, with the simulator: all 7 degrading series were flagged within 30 seconds of the 2.43.0 deployment (the downstream service about 2 minutes later, as designed), the healthy control service was not flagged, and re-running the simulator created no duplicate anomalies
- in a real browser against the emulator: the Anomalies page lists the seven with their evidence; the latency chart is shaded from the deployment marker onward; the control service's chart has no shading; no failed requests or page errors

Evaluation results at implementation (synthetic scenarios only): robust z-score 10/11 problems found with 0 false positives; static 2× rule 9/11; EWMA 8/11. The default misses the gradual drift.

---

## 20. Slice 6 (incidents): implementation record

The *Correlate* stage: related anomalies become one incident, with an explainable timeline.

- **`incidents/rules.py`:** the pure grouping rules.
- **`incidents/service.py`:** applies them against PostgreSQL inside the detection transaction; records the timeline; links candidate deployments; resolves, reopens and merges.
- **`incidents/dependencies.py`:** declared service dependencies (replace-all, written directly to PostgreSQL).
- **Migration `0006`:** `incidents`, `incident_events`, `incident_deployments`, `service_dependencies`, and `anomalies.incident_id`.
- **API and web app:** incident list and detail; dependency endpoints.

Decisions made during implementation:

| Decision | Why |
|---|---|
| Grouping runs in the detection consumer's transaction | An anomaly and its place in an incident commit together, and detection's idempotency (the per-series position) covers grouping too. |
| Rule: within 15 minutes, and same service or one direct dependency | As planned in §6. Explainable, and narrow enough not to merge unrelated problems that happen to coincide. |
| Incidents merge when one anomaly relates to two | Anomalies don't arrive in dependency order. Without merging, a problem whose middle service is detected last would stay split in two. |
| A resolved incident can be reopened for one window | A problem that recovers briefly and returns is one incident, not two. |
| Candidate deployments are limited to services already in the incident | The scenario's unrelated deployment must not be linked. The link is recorded as a candidate with the rule that linked it. |
| Statuses are `open`, `resolved`, `merged` only | These are what the system can determine by itself. Human workflow states (acknowledged, false positive) belong with feedback. |
| Timeline entries carry a sequence number | Several anomalies are detected in the same instant; ordering by time alone showed the reasons out of order. Found on the Docker run. |
| Dependencies are declared, not inferred | Inference needs traces, which aren't ingested yet. Declaring is replace-all so it is idempotent. |
| The incident page has a "Not yet known" section | CLAUDE.md: distinguish observed facts, hypotheses and unknowns. Grouping establishes neither cause nor direction. |
| The landing page is now Incidents | The product is incident-centred. |

Verified:
- on the Docker stack, with the simulator: the seven anomalies became one incident across three services; the only candidate deployment is `payment-api 2.43.0`; the unrelated `inventory-api` deployment is not linked; the timeline gives the rule for each anomaly, in order
- in a real browser against the emulator: the incident list and detail page on desktop and at 390px, light and dark, with no horizontal overflow, failed requests or page errors

---

## 21. Slice 7 (AI investigation): implementation record

The *Investigate* and *Explain* stages: a cited, checked analysis of an incident.

- **`investigation/evidence.py`:** deterministic evidence collection and snapshots.
- **`investigation/llm.py`:** provider interface; Gemini over its REST API with `httpx`; a disabled provider when no key is set.
- **`investigation/prompts.py`:** the two system prompts and the delimited data blocks; `PROMPT_VERSION`.
- **`investigation/validation.py`:** the pure checks on model output.
- **`investigation/orchestrator.py`:** the fixed five-step run, each step recorded.
- **`investigation/service.py`, `worker.py`:** queue and worker (`ii work`).
- **Migration `0007`:** `investigations`, `investigation_evidence`, `investigation_steps`.

Decisions made during implementation:

| Decision | Why |
|---|---|
| Gemini instead of Ollama (changes D7) | The user's choice: much better reasoning than a model that fits in 6 GB of GPU memory. The cost is that evidence leaves the machine. All data is synthetic for now. Ollama stays planned behind the same interface. |
| Gemini is called over REST with `httpx`, without an SDK | `httpx` is already a dependency, and the call is one endpoint. |
| The reply format is described in the prompt and checked with Pydantic, not enforced by a provider-specific schema feature | Works the same for any provider. One correction attempt handles malformed replies. |
| The `investigations` table is also the queue (`FOR UPDATE SKIP LOCKED`, lease, attempts), instead of a generic `jobs` table (changes D9) | There is one kind of job. A generic table can come when there is a second. |
| Citations are checked in code and stored in the report JSON, instead of `report_claims` and `claim_evidence` tables (changes §4) | The check gives the same guarantee with far less schema. Normalized claims can be added when feedback needs to attach to individual claims. |
| Investigations are started by a person | Cost and rate limits are under the user's control, and an incident that just opened has little evidence yet. |
| Only the worker receives the LLM key | Least privilege, as with the API-key pepper. |
| Step records hold counts and outcomes, not prompts or replies | Enough to audit a run. The evidence snapshot already records what the model saw, and prompts are versioned in code. |
| Verification can lower an assessment but never raise it | A second model call shouldn't be able to make a weakly supported claim look stronger. |
| `<` is escaped in the data blocks | Found by a test: JSON quoting alone let a log line contain the literal closing tag. |

Verified:
- by tests with a scripted model (see README)
- end to end with the real worker and Gemini client against a stand-in HTTP server returning a canned reply, in a real browser: the uncited fact and an invented reference were removed and listed, the second check overturned one hypothesis, citations open their evidence, and the page works at 390px

Verified against the real Gemini API, once a key was available (the simulated payment incident, 15 evidence items):
- `gemini-3.5-flash` produced a report in about 105 s (7,405 input and 1,122 output tokens, one provider retry). Its one hypothesis, marked supported, matches the cause the simulator plants: version 2.43.0's query on `customer_ref` causing sequential scans, pool exhaustion and cascading errors. All 8 facts cited real evidence; the checks changed nothing; the second check returned "holds".
- `gemini-3.8-flash` answered 503 "high demand" on all four tries, and the investigation failed with that message, as designed.
- The first report's summary stated the cause as established. Prompt version 2 tells the model to write "the evidence suggests"; a second real run did.
- This is one scenario, which the model could solve largely from the log text. It is not an evaluation.
