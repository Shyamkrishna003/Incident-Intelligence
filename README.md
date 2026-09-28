# Incident Intelligence

AI-assisted incident detection, investigation, root-cause analysis, and reliability learning.

**Observe → Detect → Correlate → Investigate → Explain → Act → Verify → Learn**

- Product requirements: [PRD.md](PRD.md)
- Engineering rules: [CLAUDE.md](CLAUDE.md)
- Architecture and roadmap: [docs/architecture-proposal.md](docs/architecture-proposal.md)

## Status

**Slice 1 of the roadmap: foundation and tenancy primitives.** It includes:

- A FastAPI backend with typed configuration, structured JSON logs (with secret redaction), and request IDs.
- PostgreSQL with Alembic migrations.
- Organizations, projects, and project-scoped API keys. Only an HMAC of each key is stored, and create/revoke actions are audit-logged.
- Health and readiness probes.
- An authenticated endpoint that returns the caller's project.

Telemetry ingestion (Kafka + Redis), sign-in (Firebase), the frontend, detection, and AI investigation arrive in later slices. See the architecture doc, §11.

## Prerequisites

- Python 3.12 (with `venv`)
- Docker with Compose v2. Your user must be able to run `docker` without sudo.
- GNU Make

## Quick start

```bash
make env          # create .env with a generated API-key pepper (never overwrites an existing .env)
make venv         # create .venv and install hash-pinned dependencies
make db           # start PostgreSQL (host port 5433) and create the *_test database
make migrate      # apply migrations
make bootstrap ORG=acme PROJECT=payments   # prints an API key ONCE on stdout
```

Run the API. Use either:

```bash
make run          # on the host, with auto-reload, at http://localhost:8000
# or
make up           # in Docker, alongside PostgreSQL
```

Try it:

```bash
curl localhost:8000/healthz
curl localhost:8000/readyz
curl -H "Authorization: Bearer $KEY" localhost:8000/v1/project
```

The OpenAPI docs are at http://localhost:8000/docs. They're disabled when `ENVIRONMENT=production`.

## API (slice 1)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | none | Liveness. The process is up. Never touches the database. |
| GET | `/readyz` | none | Readiness. Returns 200 when the DB is reachable and at the expected migration head, and 503 with the failing check otherwise. |
| GET | `/v1/project` | `Bearer <API key>` | The organization, project, and key that the credential belongs to |

Errors share one envelope: `{"error": {"code", "message", "request_id"}}`. Authentication failures always return the same generic 401. The specific reason (missing, malformed, unknown, revoked, expired) is logged server-side only.

## Admin CLI

Run from `backend/` with `../.venv/bin/ii`, or use `make bootstrap`.

```bash
ii bootstrap --org acme --project payments [--org-name "Acme"] [--project-name "Payments"]
ii api-keys create --org acme --project payments --name ci [--scope ingest:write] [--expires-in-days 90]
ii api-keys list   --org acme --project payments
ii api-keys revoke --prefix <12-char prefix>
```

The plaintext key is written to **stdout once**. Messages and logs go to stderr, so `KEY=$(ii api-keys create ...)` captures just the key.

## Development

| Command | What it does |
|---|---|
| `make check` | lint + type check + all tests |
| `make lint` / `make fmt` | ruff check / ruff format |
| `make typecheck` | mypy `--strict` |
| `make test` | pytest. Integration tests need `make db`. |
| `make test-unit` | tests that need no database |
| `make lock` | re-pin dependencies after editing `backend/pyproject.toml` |

### Testing

- Integration tests use the database in `TEST_DATABASE_URL`. Its name must end in `_test`, otherwise the run is refused.
- Migrations are applied once per test session.
- Each test runs inside a transaction that is rolled back afterwards.
- One test checks that the SQLAlchemy models and the migrations define exactly the same schema.

### Layout

```
backend/
  src/incident_intel/
    core/        config, logging + redaction, errors, request-context middleware
    db/          engine/session lifecycle, base model, migration helpers
    tenancy/     organizations, projects, API keys, tenant context
    audit/       audit log
    api/         health probes, auth dependencies
    migrations/  Alembic environment and revisions (shipped inside the package)
    cli.py       `ii` admin CLI
  tests/{unit,integration}/
infra/postgres/init/   first-run database init (creates the test database)
docs/                  architecture and decisions
```

## Configuration

See [.env.example](.env.example).

- `API_KEY_PEPPER` is required, with a minimum of 32 characters. **Changing it invalidates every existing API key.**
- Real environment variables override `.env`.
- `.env` is git-ignored. Never commit it.

## Known limitations (slice 1)

- There's no request-body size limit yet. It arrives with the first endpoint that accepts a body (ingestion, slice 2).
- There's no rate limiting yet (Redis, slice 2).
- Rotating the API-key pepper isn't supported. Hashes aren't versioned yet.
- Tenant isolation is enforced in the application layer and by composite foreign keys. PostgreSQL row-level security is planned for the hardening slice.
- There's no CI workflow yet. It will be added once a GitHub remote exists.
