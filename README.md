# FDQ — Logging, Audit Trail & Notification Services

> Fiducia DQMS | Cross-cutting infrastructure services  
> Spec: FDQ Technical Specification v1.0, May 2026  
> Status: **Phase 2 / 7 in progress**

---

## Overview

Implements four services that every other FDQ microservice depends on:
`activity_logging` · `error_logging` · `audit_trail` · `notification_service`

Satisfies: `FR-SEC-05` `FR-STORE-02/03` `FR-VER-10` `FR-NOTIF-01/02/04/05/06` `FR-ETL-06` `FR-REM-14/17`

---

## Stack

| | |
|---|---|
| API | FastAPI 0.111, Python 3.11 |
| DB | PostgreSQL 16, psycopg2 (raw — no ORM) |
| Queue | Celery 5.3 + Redis 7 |
| Auth | JWT RS256 (PyJWT) — second-check only, gateway validates first |
| Migrations | Alembic — raw SQL, no SQLAlchemy models |
| Logging | structlog — JSON → ELK in production |
| Validation | Pydantic v2 |

---

## Quick Start

```bash
cp .env.template .env          # defaults work for local dev
docker compose up -d           # postgres:5432, redis:6379
python scripts/run_migrations.py
celery -A fdq_commons.tasks.celery_app worker -Q fdq_default,fdq_logging,fdq_notifications,fdq_maintenance &
celery -A fdq_commons.tasks.celery_app beat &
uvicorn services.activity_logging.main:app --port 8001 --reload
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
  └── validates RS256 signature
  └── forwards token + request
        │
        ├── activity_logging :8001
        ├── error_logging    :8002
        ├── audit_trail      :8003  (Phase 5)
        └── notification     :8004  (Phase 6)
              │
              └── fdq_commons  (shared: auth, errors, pagination, db, cache)
                    │
                    ├── PostgreSQL  (psycopg2 pool, RLS enabled)
                    ├── Redis       (Celery broker + idempotency cache)
                    └── Celery      (async writes, beat scheduler)
```

**Design decisions worth noting:**

- **No ORM.** Raw psycopg2 throughout. The audit trail hash computation and
  deduplication logic need deterministic, inspectable SQL — an ORM adds
  abstraction that makes auditing harder and performance less predictable
  for a write-heavy system.

- **Async writes via Celery.** No log write ever blocks a request. If the
  logging service is degraded, the primary operation still succeeds.
  Per spec §3.1: *"If the logging call fails, it must not fail the primary operation."*

- **Audit immutability at the trigger level.** `prevent_audit_mutation()`
  is a PostgreSQL trigger — it cannot be bypassed by application code, a
  misconfigured service, or a developer with DB access. This is a hard
  NDPR/CBN requirement, not a preference.

- **JWT is a second check.** The gateway already validated the signature.
  Services re-verify to enforce OAuth 2.0 scopes at the endpoint level.
  Services hold only the public key — they cannot issue tokens.

- **One `.env`, one settings object.** `fdq_commons.config.settings` is
  imported everywhere. No service hardcodes any value. Change one env var
  and it propagates across all services on restart.

---

## Project Structure

```
fdq/
├── fdq_commons/
│   ├── config.py               # Pydantic settings — all env vars
│   ├── logging_setup.py        # structlog, JSON prod / pretty dev
│   ├── models/
│   │   ├── errors.py           # ErrorEnvelope, FDQException, ErrorCode registry
│   │   └── pagination.py       # Offset + cursor pagination
│   ├── middleware/
│   │   ├── jwt_auth.py         # RS256 verify, require_scope(), CallerContext
│   │   ├── rate_limit_headers.py
│   │   ├── request_context.py  # correlation_id per request → structlog context
│   │   └── health.py           # /health, /ready
│   ├── utils/
│   │   ├── ip_validator.py     # INET validation + Pydantic type
│   │   └── sanitiser.py        # Log injection prevention, PII masking (BVN/NIN/acct)
│   ├── db/
│   │   ├── session.py          # ThreadedConnectionPool, get_db_conn(), db_connection()
│   │   ├── base_model.py       # BaseRecord dataclass + from_row()
│   │   └── redis_client.py     # Redis client, namespaced IdempotencyCache
│   └── tasks/
│       ├── celery_app.py       # App, 4 queues, beat schedule
│       └── maintenance.py      # Mat view refresh (15 min), audit chain verify (weekly)
│
├── migrations/
│   ├── env.py                  # DSN from settings, include_object isolation hook
│   └── versions/
│       ├── 001_baseline_schema.py   # 6 tables, indexes, RLS, triggers, mat view
│       └── 002_seed_data.py         # event_type_registry, notification_templates
│
├── services/
│   ├── activity_logging/       # Phase 3 — in progress
│   ├── error_logging/          # Phase 4 — pending
│   ├── audit_trail/            # Phase 5 — pending
│   └── notification_service/   # Phase 6 — pending
│
└── tests/
    └── commons/
        └── test_phase1_commons.py   # JWT, IP, scopes, errors, pagination, sanitiser
```

---

## Database Schema

Six tables — all created in `001_baseline_schema`, seeded in `002_seed_data`.

| Table | Purpose | Notes |
|---|---|---|
| `activity_logs` | Operational audit — who did what, when, from where | Partitioning recommended >100M rows/year |
| `error_logs` | Exception capture with deduplication | Dedup window: `ERROR_DEDUP_WINDOW_SECONDS` (default 300s) |
| `audit_events` | Immutable hash-chained state change log | UPDATE/DELETE blocked at trigger level |
| `notification_logs` | Outbound notification delivery record | FK → audit_events |
| `notification_templates` | Jinja2 templates per channel | Version-controlled, audit-logged on change |
| `notification_preferences` | Per-user alert preferences | Suppression windows, digest mode |

RLS is enabled on `activity_logs` and `audit_events`.
`fdq_user` — read/write. `fdq_readonly` — select only.

Materialized view `activity_logs_summary` powers the compliance dashboard
summary endpoint. Refreshed `CONCURRENTLY` every 15 min via Celery beat
(unique index `idx_als_concurrent_refresh` required for concurrent refresh).

---

## Celery Queues

| Queue | Used for |
|---|---|
| `fdq_logging` | Async activity and error log writes |
| `fdq_notifications` | Notification delivery tasks |
| `fdq_maintenance` | Mat view refresh, audit chain verify, heartbeat |
| `fdq_default` | Everything else |

---

## Auth Scopes

| Scope | Assigned to |
|---|---|
| `logs:write` | All internal services (client credentials) |
| `logs:read` | Data Analyst, Compliance Officer, System Admin |
| `audit:append` | All internal services (client credentials) |
| `audit:read` | DGS, Compliance Officer, System Admin |
| `audit:verify` | System Admin, Compliance Officer |
| `notifications:send` | All internal services (client credentials) |
| `notifications:read` | System Admin, Compliance Officer |
| `notifications:configure` | System Admin only |

---

## API Conventions

**Every error response:**
```json
{
  "error": {
    "code": "INSUFFICIENT_SCOPE",
    "message": "Token is missing required scope: logs:write.",
    "details": [],
    "trace_id": "a3f8c21d-...",
    "timestamp": "2026-05-30T10:22:05Z"
  }
}
```

**Every list response:**
```json
{
  "data": [...],
  "pagination": {
    "page": 1, "page_size": 50, "total": 4200,
    "total_pages": 84, "has_next": true, "next_cursor": "uuid"
  }
}
```

Idempotency via `Idempotency-Key` header on all POST endpoints.
Rate limit headers (`X-RateLimit-Limit/Remaining/Reset`) on all responses per FR-REM-17.

---

## Migrations

```bash
python scripts/run_migrations.py           # upgrade to head
python scripts/run_migrations.py --check   # current revision
python scripts/run_migrations.py --history
```

`AUDIT_HASH_ALGORITHM` must not change post go-live — it breaks all existing hash chains.

---

## Tests

```bash
pytest tests/commons/test_phase1_commons.py -v
pytest tests/ --cov=fdq_commons --cov-report=term-missing
```

Covers: JWT validation, RS256 key loading, scope enforcement, IP validation,
PII masking, sanitisation, error envelope structure, pagination meta,
idempotency cache, settings DSN construction.

---

## Progress

| Phase | Scope | Status |
|---|---|---|
| 0 | Environment, Docker, project scaffold | ✅ Done |
| 1 | `fdq_commons` — all shared infrastructure | ✅ Done |
| 2 | Alembic migrations, schema, seed data, Celery beat | 🔄 In progress |
| 3 | Activity Logging Service | ⏳ Next |
| 4 | Error Logging Service | ⏳ Pending |
| 5 | Audit Trail Service | ⏳ Pending |
| 6 | Notification Service | ⏳ Pending |
| 7 | Integration tests, Locust load tests, k8s deployment | ⏳ Pending |

---

## Security

- PII masking enforced at Pydantic serialisation layer — cannot be bypassed by returning a raw DB row
- Audit event immutability enforced at PostgreSQL trigger level — not just API level
- Free-text fields sanitised on input (newline stripping, HTML escaping) — log injection prevention
- Stack traces never returned to callers — trace_id only, full detail to ELK
- `SWAGGER_UI_ENABLED=false` required in production (spec §10.4)