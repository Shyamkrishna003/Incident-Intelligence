# Incident Intelligence

AI-assisted incident detection, investigation, root-cause analysis, and reliability learning.

**Observe → Detect → Correlate → Investigate → Explain → Act → Verify → Learn**

- Product requirements: [PRD.md](PRD.md)
- Engineering rules: [CLAUDE.md](CLAUDE.md)
- Architecture and roadmap: [docs/architecture-proposal.md](docs/architecture-proposal.md)

## Status

Implemented through **slice 7: *Observe*, *Detect*, *Correlate* and *Investigate* (AI-assisted, evidence-cited analysis), with sign-in for people, a web app, and a scenario simulator**:

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

- **Web app (slice 3b):**
  - Sign in or sign up with email/password, Google or GitHub, including email verification and linking a second sign-in method to an existing account.
  - First-time setup creates an organization and a project.
  - Browse services and chart their metrics over 15 minutes to 24 hours, with a table view of the same data.
  - Admins create and revoke API keys. A new key is shown once.

- **Logs, deployments and a simulator (slice 4):**
  - Services send log records and deployment events through the same pipeline as metrics.
  - Logs can be read per service, filtered by severity and text. Deployments can be read per project.
  - The web app shows a service's logs under its chart, marks deployments on the chart, and lists deployments.
  - `ii simulate` generates a clearly labelled synthetic incident and sends it through the real ingestion API.

- **Anomaly detection (slice 5):**
  - A detection consumer evaluates every new metric point against that series' recent normal range.
  - Abnormal points in a row become one anomaly that opens, extends and closes, with the evidence recorded (the extreme value and what was usual).
  - `ii eval detection` scores the detectors on labelled synthetic scenarios. The default was chosen from those results.
  - The web app lists anomalies and shades them on the metric chart.

- **Incidents (slice 6):**
  - Related anomalies are grouped into one incident, using time and declared service dependencies.
  - Every grouping decision is recorded on the incident's timeline with the rule that made it.
  - Deployments of the affected services shortly before or during the incident are linked as candidates, never stated as the cause.
  - An incident resolves when all its anomalies end, and reopens if a related one appears soon after.
  - The web app has an incident list and a detail page: what was observed, what changed, what isn't known, and the timeline.

- **AI investigation (slice 7):**
  - On request, a worker collects a fixed set of evidence for an incident and asks an LLM (Gemini) for a structured report: observed facts, hypotheses, unknowns and suggested checks.
  - Every statement must cite evidence. Code checks each citation and removes or downgrades what isn't backed.
  - A second model call tries to disprove each hypothesis.
  - The run, its steps and the exact evidence shown to the model are stored.

Next come human feedback, learning records and evaluation of the investigations. See the architecture doc, §11.

## How ingestion works

```
service ──POST /v1/ingest/{metrics,logs,deployments}──► API ── validate, stamp tenant ids
          (API key, Idempotency-Key)                     │
                                                         │ 202 once Kafka acknowledged
                                                         ▼
                              Kafka topics telemetry.{metrics,logs,deployments}.v1
                                                         │
                                                         ▼
                                    storage consumer ──► PostgreSQL
                                        │ (commit offset only after the DB commit)
                                        └─► <topic>.dlq (messages that can never be stored)
```

- **Tenant safety.** The organization and project come from the verified API key and are stamped onto the Kafka message. The request body can't set them, and the database rejects rows whose project doesn't match their parent's.
- **No duplicates.** The `batch_id` is derived from the project and the `Idempotency-Key`, so a retried request maps to the same batch. The API remembers each key in Redis for 24 hours: a retry of a published batch returns the same answer without a second Kafka message, and different data under the same key gets a 409. Independently of Redis, the consumer skips batches it has already stored, and each point is unique on (series, timestamp).
- **One path for all three.** Metrics, logs and deployments differ only in their validation rules, message type and topic. An `Idempotency-Key` is scoped to its kind, so the same key can be used for a metric batch and a log batch.
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
  - `telemetry.metrics.v1` and `telemetry.logs.v1`: 6 partitions each, keyed by project, 3-day retention.
  - `telemetry.deployments.v1`: 1 partition, 7-day retention.
  - `telemetry.metrics.stored.v1`: 6 partitions, 3-day retention. Published by the storage consumer after a metric batch is stored; read by the detection consumer.
  - Each has a dead-letter topic named `<topic>.dlq`: 1 partition, 14-day retention.
  - All have a 2 MiB max message size.
  - Topics are created by `ii kafka init`. The broker never auto-creates them.
- **Local setup:**
  - A single broker in KRaft mode (no ZooKeeper), with the JVM heap capped at 512 MB.
  - It runs PLAINTEXT, without authentication, and is published on localhost only. TLS, SASL and ACLs come with deployment hardening.
- **Operating cost:** one more service to run and monitor. Consumer lag is the key health metric (Prometheus arrives in the hardening slice).

## Anomaly detection

```
storage consumer ── stores a metric batch ──► "metrics stored" event (Kafka)
                                                      │
                                                      ▼
                         detection consumer ── reads the new points from PostgreSQL
                                                      │
                                                      ▼
                                                  anomalies
```

Detection runs after storage, on an event that names the series with new points. It reads the points from PostgreSQL, so it never evaluates data that isn't stored yet.

**How a point is judged (default detector: robust z-score).** Each new value is compared with the last 120 normal points of its series. The *score* is its distance from their median, measured in units of their typical spread (the median absolute deviation). A score of 6 or more, in either direction, is anomalous. A series is not judged until it has 30 points.

**How points become an anomaly.**

- **Opens** after 2 anomalous points in a row. A single odd point opens nothing.
- **Extends** with each further anomalous point. A short return to normal doesn't split it.
- **Closes as "recovered"** once a normal point arrives at least 2 minutes after the last anomalous one.
- **Closes as "persisted"** after 6 hours open: the level is then treated as the new normal.
- **The baseline is frozen** while an anomaly is open. Points inside an anomaly never enter the baseline, so an ongoing problem can't teach the detector that it's normal.
- At most one open anomaly exists per series, enforced by the database.

**What an anomaly records:** when it started, when it was detected, the most extreme value, what was usual just before (centre and spread), the direction, and a severity.

**Severity and score are not probabilities.** The score is only comparable to the detector's own threshold. Severity is a fixed rule on it: low (at the threshold), medium (2× the threshold), high (4×), critical (8×). An anomaly is a statistical flag to investigate, not a confirmed problem.

**Idempotent.** Each series has a recorded "evaluated through" time. Redelivered events and points at or before it are ignored, so nothing is detected twice.

### Evaluation

`make eval` (or `ii eval detection`) runs each detector over labelled synthetic scenarios and reports recall, precision, false positives and time to detect. Current results, at threshold 6 with 2 points in a row to open:

| Detector | Problems found | False positives | Median time to detect |
|---|---|---|---|
| Static threshold (alert above 2× the usual level) | 9 of 11 | 0 | 30 s |
| EWMA | 8 of 11 | 0 | 30 s |
| **Robust z-score (default)** | **10 of 11** | **0** | **30 s** |

The scenarios: the simulated payment incident (7 degrading series, 2 healthy), a healthy 6-hour day, a level shift, a brief spike, a gradual drift, a drop, isolated one-point glitches, a slow two-hour wave, bursty data, and extra-noisy data.

- **What the default misses:** the gradual drift (a slow climb to double over 40 minutes). With no sudden change, the baseline follows the drift.
- **Why not threshold 4:** it finds the same problems but raises a false alarm on the slow wave.
- **What the simple rule misses:** a 60% level shift and a drop to 30%, because neither doubles the value.
- **What EWMA misses:** it adapts to the change it should flag, so it misses the drift and some of the incident's slower series.

These results describe behaviour on synthetic scenarios only. They are not evidence of performance on real production telemetry. A test fails if the default detector's results on these scenarios get worse.

Detection settings (`DETECTION_*` in the environment) should be changed only together with a new evaluation run.

## Incidents

One problem usually shows up as many anomalies. Grouping turns them into one incident.

**When an anomaly opens, it joins an existing incident if both are true:**

1. **Time:** it started no more than 15 minutes before the incident started, and no more than 15 minutes after the incident's latest activity.
2. **Place:** its service is already in the incident (`same_service`), or a declared dependency directly connects it to one of the incident's services, in either direction (`dependency`).

Otherwise it opens a new incident. Two hops of dependency don't count: A and C are not grouped just because both relate to B.

- **Merging.** If one anomaly is related to two incidents (it bridges them), they are merged into the older one. The other is marked `merged` and points to it.
- **Resolving and reopening.** An incident resolves when none of its anomalies is open. A related anomaly starting within 15 minutes of that reopens it.
- **Candidate deployments.** Deployments of the incident's services from one hour before it started until its latest activity are linked, as `before` or `during`. A link means "this service is affected and was deployed around then". It is not a finding that the deployment caused anything.
- **Severity** is the worst severity among the incident's anomalies.
- **Timeline.** Each decision is recorded with its rule: `incident_opened`, `anomaly_attached` (with `same_service` or the dependency used), `deployment_linked`, `anomaly_ended`, `incident_resolved`, `incident_reopened`, `incidents_merged`.

Grouping happens in the detection consumer, in the same transaction that stores the anomalies, so an anomaly and its incident are saved together.

**These rules group; they don't diagnose.** The incident page says so: it lists what was observed, what changed, and states that the cause is not yet known.

### Service dependencies

Grouping across services needs to know which service calls which. Declare it with `PUT /v1/dependencies` (the whole set is replaced each time):

```bash
curl -X PUT localhost:8000/v1/dependencies -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"dependencies": [{"service": "checkout-web", "depends_on": "payment-api"},
                        {"service": "payment-api", "depends_on": "payments-db"}]}'
```

Without declared dependencies, anomalies on different services become separate incidents. The simulator declares its own.

## AI investigation

An investigation starts only when a member clicks **Investigate with AI** on an incident (or calls the API). It runs in the `investigation-worker`, outside any API request.

```
1. collect evidence   code only: the incident, its anomalies, deployments around it,
                      declared dependencies, unaffected services, and warning/error log
                      patterns of the affected services. Each item gets a reference (E1, E2, …)
2. analyze            model call → draft report (one correction attempt if malformed)
3. validate           code only: checks every citation
4. verify             model call → tries to disprove each hypothesis
5. store              the checked report, with what the checks changed
```

**What the model can and can't do.** It receives text and returns JSON. It has no tools: it can't query anything, fetch anything or act. There is no loop it controls.

**What code enforces on the model's reply:**

- A reference to evidence that doesn't exist is removed.
- An "observed fact" with no valid citation is removed.
- A hypothesis marked supported or weak without supporting evidence is shown as untested.
- The second check can only lower an assessment, and "contradicted" counts only if it cites real evidence.
- Everything removed or changed is listed with the report.

**Assessments are words** (supported, weakly supported, untested, contradicted), never probabilities. The report is labelled as AI-written, and hypotheses as suggestions to verify.

**Untrusted input.** Log messages and deployment descriptions come from monitored systems and may contain text that looks like instructions.
- They are passed to the model as JSON data inside a delimited block, with `<` escaped so the data can't close the block.
- The instructions tell the model to treat that block as data only.
- This reduces the risk of prompt injection but can't remove it. Because the model has no tools, the worst outcome is a wrong report, which the citation checks and the human reader are there to catch.

**Secrets.** Text from monitored systems is passed through the same redaction as application logs (API keys, bearer tokens, `password=...` and similar) and truncated before it is stored or sent. Redaction is pattern-based and won't catch every secret, so services still shouldn't log them.

**Audit.** Each investigation records the provider, model, prompt version, token counts, every step (kind, duration, outcome) and a snapshot of each evidence item exactly as the model saw it. Prompts and raw replies are not stored.

**Limits and failures:**

- One investigation per incident at a time, and at most 6 per incident per hour.
- A rate-limited or failing provider is retried with backoff (up to 4 tries per call), then the investigation fails with the provider's message.
- A malformed reply gets one correction attempt, then the investigation fails. The collected evidence stays viewable.
- If the second check fails, the report is kept and says the check didn't run.
- If the worker dies mid-run, the investigation is retried once after 10 minutes, then failed.
- Without `GEMINI_API_KEY`, investigations fail with a message saying so.

**Data leaves your infrastructure.** The evidence for an incident is sent to Google's Gemini API. Check the data-use terms of your Gemini plan before using this with real telemetry.

## Scenario simulator

`ii simulate` produces a synthetic incident modelled on the PRD's example and sends it through the real ingestion API, so detection and investigation can be built and evaluated without production data.

```bash
SIMULATOR_API_KEY=ii_... make simulate                  # the last 60 minutes; incident starts 45 minutes in
SIMULATOR_API_KEY=ii_... make simulate ARGS="--live"    # then keep sending until Ctrl+C
SIMULATOR_API_KEY=ii_... make simulate ARGS="--no-incident --backfill-minutes 180"   # healthy baseline
```

The scenario, relative to the incident start `T`:

| When | What happens |
|---|---|
| before `T` | Four services (`payment-api`, `payments-db`, `checkout-web`, `inventory-api`) are healthy, with small random variation. |
| `T − 25 min` | `inventory-api` deploys 1.8.2. It's unrelated: a deliberate red herring. |
| `T` | `payment-api` deploys 2.43.0. |
| `T` to `T + 10 min` | Database query time and sequential scans climb, the connection pool fills, and `payment-api` latency and errors rise. `checkout-web` follows about two minutes later. `inventory-api` stays healthy. Warning and error logs appear. |
| `T + 20 min` | `payment-api` rolls back to 2.42.3, and everything recovers over five minutes. |

- **Always labelled.** Every metric series and log record carries `source=simulator`, and deployments have `deployed_by: simulator` and say "synthetic" in their description.
- **Deterministic.** The same `--seed` and time period produce identical data, so re-running over the same period is recognized as a retry and stores nothing twice.
- **The API key** is read from the `SIMULATOR_API_KEY` environment variable, not a flag, because command lines are visible to other users on the machine. It needs the `ingest:write` scope.
- The simulator declares its services' dependencies (`checkout-web` → `payment-api` → `payments-db`, and `checkout-web` → `inventory-api`), so its incident is grouped the way a real one would be.
- The simulator waits and retries when the API answers 429 or 503.

## Two kinds of caller

| | Programs (services sending telemetry) | People (using the console) |
|---|---|---|
| Credential | Project API key: `Authorization: Bearer ii_...` | Firebase ID token: `Authorization: Bearer <token>` |
| Scope | Exactly one project, fixed by the key | Every organization the user is a member of |
| Permissions | Key scopes: `ingest:write`, `telemetry:read` | Role in the organization (below) |
| Endpoints | `/v1/project`, `/v1/ingest/...`, `/v1/services/...`, `/v1/deployments`, `/v1/anomalies`, `/v1/incidents`, `/v1/dependencies` | `/v1/me`, `/v1/organizations/...`, `/v1/projects/...` |

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

Run the web app (in another terminal, with the API running on port 8000):

```bash
make web          # http://localhost:5173
```

It needs the four `VITE_FIREBASE_*` values in `.env` (see [.env.example](.env.example)). The dev server forwards `/v1` requests to the API, so the browser talks to a single origin and the API needs no cross-origin configuration.

The OpenAPI docs are at http://localhost:8000/docs. They're disabled when `ENVIRONMENT=production`.

## Web app

| Screen | What it does |
|---|---|
| Sign in | Email/password, Google or GitHub. If the email already belongs to an account with a different method, the app asks the user to sign in the original way and then links the new method. |
| Setup | Shown until the user can reach a project. Asks for email verification first, then creates an organization and its first project. If setup was interrupted after the organization was created, it asks only for the project. |
| Incidents | The landing page. Incidents in the last 7 days, ongoing first, with severity, affected services, duration and anomaly count. |
| Incident (AI investigation) | Start an investigation, watch its progress, and read the report. Citations open the evidence they point to. Failures show the reason and a retry button. |
| Incident | **What was observed** (each anomaly with its evidence and a link to its chart), **What changed** (candidate deployments, labelled as candidates), **Not yet known** (the cause), and the **Timeline** with the reason for each grouping decision. |
| Services | The services that have sent telemetry to the selected project. Refreshes when opened and every 30 seconds. |
| Service | Pick a metric and a time range (15 minutes to 24 hours). One line per series (attribute set), a legend, a hover readout of every series, and the same values as a table. Deployments of the service are marked on the chart, and periods with a detected anomaly are shaded. Below it, the service's logs for the same time range, filterable by severity and text. Refreshes every 30 seconds. |
| Anomalies | Anomalies in the last 24 hours, ongoing first. Each row shows the evidence ("rose to 1,700 ms, usually about 120 ms") and links to the metric's chart. |
| Deployments | The project's deployments in the last 7 days, newest first, each linking to its service. |
| API keys | Admins and owners only. Create a key (shown once, with a copy button) and revoke keys after confirmation. |

- Every screen has loading, empty and error states. Errors show the server's message and a retry button.
- What a role may do is enforced by the API. The app only hides controls a user couldn't use.
- Light and dark themes follow the system setting. The chart colours are a colour-blind-safe set checked in both themes.
- A project the user can't access shows the same "not found" page as one that doesn't exist.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | none | Liveness. The process is up. Touches no dependencies. |
| GET | `/readyz` | none | Readiness. Returns 200 when the DB is reachable and migrated and Kafka is reachable with its topics, and 503 naming the failing check otherwise. Redis is reported but not required. |
| GET | `/v1/project` | API key | The organization, project, and key the credential belongs to |
| POST | `/v1/ingest/metrics` | API key with `ingest:write` | Accepts 1–1000 points (body ≤ 1 MiB). Requires an `Idempotency-Key` header. Returns **202** with `batch_id`, or **409** if the key was already used with different data. |
| POST | `/v1/ingest/logs` | API key with `ingest:write` | Accepts 1–1000 log records. Same headers, status codes and delivery behaviour as metrics. |
| POST | `/v1/ingest/deployments` | API key with `ingest:write` | Accepts 1–100 deployment events (service, version, time; optional commit, environment, who, description). |
| GET | `/v1/services/{service}/logs` | API key with `telemetry:read` | Log records in `[start, end)` (default: the last hour; at most 24 h), newest first. `severity` returns that level and above; `q` matches text in the message; `limit` is at most 1000. |
| GET | `/v1/deployments` | API key with `telemetry:read` | Deployments in `[start, end)` (default: the last 7 days; at most 31), newest first. `service` filters to one service. |
| GET | `/v1/anomalies` | API key with `telemetry:read` | Anomalies overlapping `[start, end)` (default: the last 24 hours; at most 31 days), ongoing first. Filters: `status` (`open`, `closed`, `all`), `service`, `metric`. |
| GET | `/v1/incidents` | API key with `telemetry:read` | Incidents overlapping `[start, end)` (default: the last 7 days; at most 31), ongoing first. `status` is `open`, `resolved` or `all`. Merged incidents are left out. |
| GET | `/v1/incidents/{incident_id}` | API key with `telemetry:read` | One incident with its anomalies, candidate deployments, timeline, and the declared dependencies among its services. |
| GET | `/v1/dependencies` | API key with `telemetry:read` | The project's declared service dependencies |
| PUT | `/v1/dependencies` | API key with `ingest:write` | Replace the declared dependencies (at most 500). Registers services that haven't sent telemetry yet. |
| GET | `/v1/services` | API key with `telemetry:read` | Services that have sent telemetry to this project |
| GET | `/v1/me` | signed-in user | The user, their organizations and roles, and each organization's projects. Creates the user record on first call. |
| POST | `/v1/organizations` | signed-in user, verified email | Create an organization. The caller becomes its owner. |
| POST | `/v1/organizations/{organization_id}/projects` | admin or owner | Create a project |
| GET | `/v1/projects/{project_id}/api-keys` | admin or owner | List the project's API keys (never the key or its hash) |
| POST | `/v1/projects/{project_id}/api-keys` | admin or owner | Create an API key. The full key is in this response only. |
| DELETE | `/v1/projects/{project_id}/api-keys/{prefix}` | admin or owner | Revoke a key of this project. Takes effect immediately. |
| GET | `/v1/projects/{project_id}/services` | any member | Services in the project |
| GET | `/v1/projects/{project_id}/services/{service}/metrics/{metric}` | any member | Metric points, same parameters as the API-key endpoint |
| GET | `/v1/projects/{project_id}/services/{service}/logs` | any member | Log records, same parameters as the API-key endpoint |
| GET | `/v1/projects/{project_id}/deployments` | any member | Deployments, same parameters as the API-key endpoint |
| GET | `/v1/projects/{project_id}/anomalies` | any member | Anomalies, same parameters as the API-key endpoint |
| GET | `/v1/projects/{project_id}/incidents` and `.../incidents/{incident_id}` | any member | Incidents, same as the API-key endpoints |
| POST | `/v1/projects/{project_id}/incidents/{incident_id}/investigations` | member, admin or owner | Queue an AI investigation. Returns **202**; **409** if one is already in progress; **429** over the hourly cap. |
| GET | `/v1/projects/{project_id}/incidents/{incident_id}/investigations` | any member | The incident's investigations, newest first |
| GET | `/v1/projects/{project_id}/investigations/{investigation_id}` | any member | One investigation: the checked report, what the checks changed, the evidence snapshots and the recorded steps |
| GET | `/v1/projects/{project_id}/dependencies` | any member | Declared service dependencies |
| PUT | `/v1/projects/{project_id}/dependencies` | admin or owner | Replace the declared dependencies |
| GET | `/v1/services/{service}/metrics` | API key with `telemetry:read` | The metrics a service has reported, with the number of series each has. Also at `/v1/projects/{project_id}/services/{service}/metrics` for signed-in users. |
| GET | `/v1/services/{service}/metrics/{metric}` | API key with `telemetry:read` | Points in `[start, end)` (default: the last hour; at most 24 h), grouped by attribute set. `limit` defaults to 1000 and can be at most 10 000; `truncated` is true if more points exist. |

### Ingestion rules

- **Names:** `service` and `metric` are lowercase letters, digits, `.`, `_` and `-`, up to 128 characters, starting with a letter or digit.
- **Values:** `value` must be a JSON number. Strings, booleans, NaN and infinity are rejected.
- **Timestamps:** `timestamp` must include a timezone and fall within the last 7 days or at most 5 minutes in the future. Timestamps are stored in UTC.
- **Attributes:** at most 16 string pairs, with keys up to 64 characters and values up to 256.
- **Whole-batch validation:** a batch is accepted or rejected as a whole. Any invalid point rejects it with 422, and `details` lists each problem by position. Duplicate points in one batch (same series and timestamp) are rejected too.
- **Logs:** `severity` is one of `trace`, `debug`, `info` (the default), `warn`, `error`, `fatal`. `message` is 1 to 8192 characters. `trace_id` is optional (32 lowercase hex characters). Identical lines at the same instant are kept.
- **Deployments:** `version` is up to 128 characters without spaces. `commit_sha` is 7 to 40 lowercase hex characters. The same service, version and time reported again, even in a different batch, is stored once.
- **Log content is stored as received.** Don't send secrets or personal data in log messages. The API and the web app treat messages as untrusted text.
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
ii consume detection               # run the detection consumer
ii work                            # run the investigation worker (holds the LLM API key)
ii eval detection                  # score the detectors on labelled synthetic scenarios
ii dlq inspect [--kind metrics|logs|deployments] [--limit 20] [--show-values]   # bodies hidden by default
ii simulate [--live] [--no-incident] [--backfill-minutes 60] [--incident-after-minutes 45] [--seed 1]
```

- The plaintext API key is written to **stdout once**. Messages and logs go to stderr, so `KEY=$(ii api-keys create ...)` captures just the key.
- The consumer and Kafka commands don't need, or read, the API-key pepper or Redis.
- `ii api-keys revoke` also removes the key from the Redis API-key cache.

## Development

| Command | What it does |
|---|---|
| `make check` | everything: backend lint, type check and tests, plus the frontend checks |
| `make web` / `make web-check` | run the web app / frontend lint, type check, tests and production build |
| `make lint` / `make fmt` | ruff check / ruff format |
| `make typecheck` | mypy `--strict` |
| `make test` | pytest: unit, integration (PostgreSQL), Redis, and end-to-end (Kafka). Needs `make infra`. Emulator tests also need `make emulator`. |
| `make test-unit` | tests that need no PostgreSQL, Kafka or Redis |
| `make eval` | score the detectors on labelled synthetic scenarios |
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
- **Frontend tests** (Vitest and React Testing Library) cover the API client, sign-in, setup, the services list, API keys, the chart's legend and table, and routing by sign-in state and role. They use a fake auth context and never contact Firebase.
- **Detection** is tested as pure logic (detectors, the anomaly engine), against PostgreSQL, and end to end through Kafka.
  - One test checks that evaluating a series a few points at a time through the database finds exactly the anomalies the pure engine finds in a single pass.
  - Another holds the default detector to its evaluated results.
- **Incident grouping** is tested as pure rules and through the real detection handler against PostgreSQL: same service, dependency, the time window, merging, resolving and reopening, candidate deployments, timeline order, and isolation between projects.
- **Investigations** are tested with a scripted fake model: the citation checks, the correction attempt, provider failures, hostile log text staying inside the data block, secret redaction, the queue's lease and retry, permissions and isolation. The Gemini client is tested against a stand-in HTTP server. No test calls a real model.
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
    telemetry/   message contracts, models, idempotent storage consumer, read APIs
    investigation/  evidence collection, LLM provider (Gemini), prompts, report checks, orchestrator, worker
    incidents/   grouping rules, incident lifecycle and timeline, service dependencies, incidents API
    detection/   detectors, anomaly engine, detection consumer, evaluation suite, anomalies API
    simulator/   synthetic incident scenario, sender, backfill and live runner
    migrations/  Alembic environment and revisions (shipped inside the package)
    cli.py       `ii` admin CLI
  tests/{unit,integration,cache,kafka,firebase}/
frontend/
  src/
    lib/         API client and types, Firebase setup, formatting, chart data shaping
    auth/        sign-in state and actions (Firebase), error messages
    hooks/       data fetching and mutations (TanStack Query)
    components/  shared UI, app shell, metric chart
    pages/       sign-in, setup, services, service (chart), API keys
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
- `VITE_FIREBASE_*` configure the web app. They are public identifiers that ship in the browser bundle. Vite exposes only `VITE_`-prefixed variables to the browser, so the secrets in the same `.env` file stay out of it.
- `GEMINI_API_KEY` is a **secret**. Only the investigation worker receives it. `GEMINI_MODEL` selects the model.
- `FIREBASE_PROJECT_ID` enables sign-in for people. `FIREBASE_AUTH_EMULATOR_HOST` switches to the local emulator (never in production).
- Real environment variables override `.env`, and `.env` is git-ignored. Never commit it.

## Known limitations

- **No invitations yet.** An organization has one user, its owner. Other members can only be added directly in the database until a later slice.
- **The web app runs only through the dev server** (`make web`). There's no production container or hosting setup yet (deployment slice).
- **No password reset screen and no profile or organization settings** in the web app yet.
- **Sign-in with real Google and GitHub accounts hasn't been tested end to end.** The flows were exercised against the Firebase emulator with email/password.
- **Charts show at most 8 series** and at most 5,000 points per request. Both limits are stated on the chart when they apply.
- **Token revocation isn't checked.** A Firebase ID token stays valid until it expires (up to an hour) even if the account is disabled in Firebase. Access still ends immediately when the membership is removed in this system.
- **Users, organizations and projects can't be deleted or renamed** through the API yet.
- **Rate limiting is per API key or user, after authentication.** Requests with invalid credentials aren't limited (no per-IP limit yet), and each one with a well-formed but unknown key costs a database lookup.
- **Rate-limit bursts:** the fixed window allows up to twice the limit across a window boundary.
- **Idempotency memory is 24 hours.** A key reused with different data after that, or while Redis is down, is caught by the consumer and dead-lettered instead of getting a 409.
- **`last_used_at`** for an API key is updated when the key is loaded from the database, so it can lag by the cache TTL (60 s).
- **Detection misses slow drifts** (see the evaluation above), and it has been evaluated on synthetic data only.
- **No seasonality awareness.** A metric with a strong daily pattern steeper than the tested slow wave may raise false alarms.
- **Late metric points are not evaluated.** A point older than what its series has already been evaluated through is stored but skipped by detection.
- **An anomaly on a series that stops reporting stays open** until a later point arrives.
- **No user-configured thresholds or alert rules yet,** and no notifications. Anomalies are visible in the app and the API only.
- **Real Gemini runs are slow and sometimes unavailable.** Two real investigations of the simulated incident with `gemini-3.5-flash` took about 105 and 130 seconds. `gemini-3.8-flash` answered "high demand" (503) four times in a row, which fails the investigation with that message.
- **The real model was run on one scenario only,** the simulated payment incident.
- **Report quality is not evaluated yet.** The checks guarantee that citations are real, not that the reasoning is right. An evaluation suite for investigations is the next slice.
- **Only Gemini is supported.** A local Ollama provider is planned.
- **Evidence is a fixed set.** The model can't ask for more (for example a different time window), and no metric time series beyond each anomaly's summary is included.
- **Investigations are manual.** None starts automatically when an incident opens.
- **Only signed-in users can start or read investigations;** there are no API-key endpoints for them.
- **Grouping depends on declared dependencies.** Without them, one problem spanning several services becomes several incidents. Dependencies aren't discovered from traces yet.
- **Grouping has not been evaluated** against labelled cases the way detection has. It's verified on the simulated incident and by tests of each rule.
- **No manual incident actions yet:** acknowledging, renaming, marking a false positive, merging or splitting by hand, and feedback all come with a later slice.
- **Candidate deployments are found only when the incident has activity** (an anomaly opening, extending or ending). A deployment reported late is linked at the next such moment.
- **Anomalies detected before this slice** have no incident.
- **Dependencies can only be declared through the API,** not in the web app.
- **No retention job.** Logs and metrics are kept indefinitely for now; the planned limits (7 days for logs, 30 for metrics) aren't enforced yet.
- **Log search is a plain text match** within one service and time range, with no index. It's fine at this volume and will need revisiting at higher volume.
- **Log queries return the newest matches only** (up to `limit`), with no paging to older ones.
- **No traces, and no service dependencies yet.** Dependencies arrive with incident correlation, where they're first used.
- **The simulator has one scenario** (the payment incident) and one healthy baseline.
- **Hot partitions:** Kafka messages are keyed by project, so one project's batches are processed in order but by a single consumer at a time. This is a throughput ceiling for very large tenants, revisited if measurements show it.
- **Units:** a series records its unit from its first point. A different unit later isn't flagged.
- **Consumer monitoring:** the storage consumer has no health endpoint, and consumer lag isn't yet exported (hardening slice).
- **Topic config:** `ii kafka init` doesn't reconcile the settings of topics that already exist.
- **Pepper rotation:** rotating the API-key pepper isn't supported, because hashes aren't versioned yet.
- **Row-level security:** tenant isolation is enforced in the application layer and by composite foreign keys. PostgreSQL row-level security is planned for the hardening slice.
- **CI:** there's no CI workflow yet.
