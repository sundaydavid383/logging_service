# FDQ — Logging, Audit Trail & Notification Services

> Fiducia DQMS | Cross-cutting infrastructure services
> Spec: FDQ Technical Specification v1.0, May 2026
> Status: **Phases 0–5 complete, tested. Phase 6 (Notification) next.**

---

## Overview

Implements four services that every other FDQ microservice depends on:
`activity_logging` (8001) · `error_logging` (8002) · `audit_trail` (8003) · `notification_service` (8004, pending)

Satisfies: `FR-SEC-05` `FR-STORE-02/03` `FR-VER-10` `FR-NOTIF-01/02/04/05/06` `FR-ETL-06` `FR-REM-14/17`

---

## Stack

| | |
|---|---|
| API | FastAPI, Python 3.12 |
| DB | PostgreSQL 16, psycopg2 (raw — no ORM) |
| Queue | Celery 5.6 (`--pool=solo` on Windows) + Redis 7 |
| Auth | JWT RS256 (PyJWT) — second-check only, gateway validates first |
| Migrations | Alembic — raw SQL, no SQLAlchemy models |
| Logging | structlog — JSON → ELK in production |
| Validation | Pydantic v2 |

---

## Quick Start (4 terminals)

```bash
cp .env.template .env
docker compose up -d                                  # postgres:5432, redis:6379
python scripts/run_migrations.py

# Terminal 2 — worker (Windows requires --pool=solo)
celery -A fdq_commons.tasks.celery_app worker --pool=solo --loglevel=info -Q fdq_default,fdq_logging,fdq_notifications,fdq_maintenance

# Terminal 3 — beat
celery -A fdq_commons.tasks.celery_app beat --loglevel=info

# Terminal 4 — a service
uvicorn services.activity_logging.main:app --port 8001 --reload
uvicorn services.error_logging.main:app    --port 8002 --reload
uvicorn services.audit_trail.main:app      --port 8003 --reload
```

RS256 key pair for local dev:
```bash
mkdir -p keys
openssl genrsa -out keys/private.pem 2048
openssl rsa -in keys/private.pem -pubout -out keys/public.pem
```

> Production: keys mounted as Kubernetes Secrets, never on disk.

---

## Architecture

```
API Gateway (Kong/NGINX)
  └── validates RS256 signature, forwards token + request
        │
        ├── activity_logging :8001  ✅ tested
        ├── error_logging    :8002  ✅ tested
        ├── audit_trail      :8003  ✅ tested
        └── notification     :8004  ⏳ pending
              │
              └── fdq_commons (shared: auth, errors, pagination, db, cache)
                    │
                    ├── PostgreSQL (psycopg2 pool, RLS enabled)
                    ├── Redis      (Celery broker + idempotency cache)
                    └── Celery     (async writes, beat scheduler)
```

**Design decisions:**

- **No ORM.** Raw psycopg2 throughout — deterministic, inspectable SQL for audit hash computation and dedup logic.
- **Activity logs: async via Celery.** Fire-and-forget per spec §3.1 — "if the logging call fails, it must not fail the primary operation."
- **Error logs: synchronous.** Spec §4.1 calls them "the authoritative record of system failures." No Celery — write must succeed or fail before responding.
- **Audit events: synchronous + `pg_advisory_xact_lock`.** Spec §5.3.1/§8.2 mandates the hash chain computation runs inside a serialised DB transaction. Async would risk two events racing on `previous_event_hash` and forking the chain. Idempotency replay also requires the caller to get `sequence_number`/`event_hash` back immediately.
- **Audit immutability at the trigger level.** `prevent_audit_mutation()` is a PostgreSQL trigger — cannot be bypassed by application code or DB access.
- **JWT is a second check.** Gateway validates signature; services re-verify scopes. Services hold only the public key.
- **One `.env`, one settings object.** `fdq_commons.config.settings` imported everywhere — nothing hardcoded.

---

## Project Structure

```
fdq/
├── fdq_commons/
│   ├── config.py               # All env vars — single source of truth
│   ├── logging_setup.py        # structlog, JSON prod / pretty dev
│   ├── models/                 # errors.py, pagination.py
│   ├── middleware/              # jwt_auth, rate_limit_headers, request_context, health
│   ├── utils/                   # ip_validator, sanitiser (PII masking)
│   ├── db/                      # session (psycopg2 pool), base_model, redis_client
│   └── tasks/                   # celery_app (4 queues + beat schedule), maintenance
│
├── migrations/versions/
│   ├── 001_baseline_schema.py   # 6 tables, indexes, RLS, triggers, mat view
│   └── 002_seed_data.py         # event_type_registry, notification_templates
│
├── services/
│   ├── activity_logging/        ✅ models, schemas, service, tasks, routes, main
│   ├── error_logging/            ✅ models, schemas, service, routes, main (no tasks — sync)
│   ├── audit_trail/               ✅ models, schemas, service, routes, main (no tasks — sync)
│   └── notification_service/     ⏳ pending
│
└── tests/commons/test_phase1_commons.py
```

---

## Database Schema

| Table | Purpose | Notes |
|---|---|---|
| `activity_logs` | Operational audit trail | Async writes via Celery |
| `error_logs` | Exceptions with deduplication | `ERROR_DEDUP_WINDOW_SECONDS` (default 300s); sync writes |
| `audit_events` | Immutable hash-chained log | UPDATE/DELETE blocked at trigger level; sync writes with advisory lock |
| `notification_logs` | Outbound delivery record | FK → audit_events |
| `notification_templates` | Jinja2 templates per channel | Pending |
| `notification_preferences` | Per-user alert settings | Pending |

Materialized view `activity_logs_summary` refreshed `CONCURRENTLY` every 15 min via Celery beat.

---

## Verified Endpoints

**Activity Logging (8001)** — `POST/GET /api/v1/activity-logs/`, `GET .../summary`, `GET .../{id}`
**Error Logging (8002)** — `POST/GET /api/v1/error-logs/`, `PATCH .../{id}/status`, `GET .../stats` — dedup confirmed (`deduplicated: true`, `recurrence_count` increments)
**Audit Trail (8003)** — `POST/GET /api/v1/audit-events/`, `GET .../{id}`, `GET .../entity/{type}/{id}`, `POST .../verify` — hash chaining, idempotency replay, and chain verification (`valid: true`) all confirmed

---

## Auth Scopes

| Scope | Assigned to |
|---|---|
| `logs:write` | All internal services |
| `logs:read` | Data Analyst, Compliance Officer, System Admin |
| `audit:append` | All internal services |
| `audit:read` | DGS, Compliance Officer, System Admin |
| `audit:verify` | System Admin, Compliance Officer |
| `notifications:send` | All internal services |
| `notifications:read` | System Admin, Compliance Officer |
| `notifications:configure` | System Admin only |

---

## API Conventions

**Error response:**
```json
{"error": {"code": "INSUFFICIENT_SCOPE", "message": "...", "details": [], "trace_id": "...", "timestamp": "..."}}
```

**List response:**
```json
{"data": [...], "pagination": {"page": 1, "page_size": 50, "total": 4200, "total_pages": 84, "has_next": true}}
```

Idempotency via `idempotency_key` (audit) / `Idempotency-Key` header (activity). Rate limit headers on all responses per FR-REM-17.

---

## Migrations

```bash
python scripts/run_migrations.py           # upgrade to head
python scripts/run_migrations.py --check
python scripts/run_migrations.py --history
```

`AUDIT_HASH_ALGORITHM` must not change post go-live — breaks all existing hash chains.

---

## Windows Notes

- Python 3.14 breaks Celery 5.6 — use **Python 3.12**
- Celery worker requires `--pool=solo` on Windows (prefork pool causes `PermissionError`)
- If `celerybeat-schedule*` files get corrupted after a PC restart, delete all `celerybeat-schedule*` files and restart beat

---

## Progress

| Phase | Scope | Status |
|---|---|---|
| 0 | Environment, Docker, project scaffold | ✅ Done |
| 1 | `fdq_commons` — shared infrastructure | ✅ Done |
| 2 | Migrations, schema, seed data, Celery beat | ✅ Done |
| 3 | Activity Logging Service | ✅ Done, tested |
| 4 | Error Logging Service | ✅ Done, tested |
| 5 | Audit Trail Service | ✅ Done, tested |
| 6 | Notification Service | ⏳ Next |
| 7 | Integration tests, Locust, k8s deployment | ⏳ Pending |

---

## Security

- PII masking enforced at Pydantic serialisation — cannot be bypassed by returning a raw DB row
- Audit immutability enforced at PostgreSQL trigger level
- Free-text fields sanitised on input — log injection prevention
- Stack traces never returned to callers — trace_id only, full detail to ELK
- `SWAGGER_UI_ENABLED=false` required in production