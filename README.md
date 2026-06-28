# FDQ — Logging, Audit Trail & Notification Services

> Fiducia DQMS | Cross-cutting infrastructure services
> Spec: FDQ Technical Specification v1.0
> **Status: Phases 0–6 complete and tested (Teams webhook pending org access)**

---

## 1. What This Is

Four services every other FDQ microservice depends on:

| Service | Port | Purpose | Status |
|---|---|---|---|
| Activity Logging | 8001 | Operational audit trail | ✅ Tested |
| Error Logging | 8002 | Exception capture + dedup | ✅ Tested |
| Audit Trail | 8003 | Immutable hash-chained ledger | ✅ Tested |
| Notification | 8004 | Email + Teams alerts | ✅ Email tested, Teams pending |

---

## 2. Architecture

```
API Gateway → validates JWT signature → forwards to service
  ├── activity_logging :8001 (async writes via Celery)
  ├── error_logging    :8002 (sync writes — authoritative record)
  ├── audit_trail       :8003 (sync + pg_advisory_xact_lock)
  └── notification      :8004 (async via Celery)
        │
        └── fdq_commons (shared auth, errors, pagination, db, cache)
              ├── PostgreSQL (psycopg2, RLS enabled)
              ├── Redis (Celery broker + idempotency cache)
              └── Celery (workers + beat scheduler)
```

---

## 3. Key Design Decisions

| Decision | Why |
|---|---|
| Activity logs: **async** | Spec §3.1 — fire-and-forget, must never block the caller |
| Error logs: **sync** | Spec §4.1 — "authoritative record," must confirm write before responding |
| Audit events: **sync + advisory lock** | Spec §5.3.1/§8.2 — prevents two writes racing on the same hash chain |
| Notifications: **async** | Spec §6.1 — suppression logic runs inside the Celery task, not the caller |
| No ORM | Raw psycopg2 — deterministic SQL for audit hash computation |
| JWT is a second check | API Gateway validates signature; services re-verify scopes only |

---

## 4. Quick Start

```bash
cp .env.template .env
docker compose up -d
python scripts/run_migrations.py

# Terminal 2
celery -A fdq_commons.tasks.celery_app worker --pool=solo --loglevel=info -Q fdq_default,fdq_logging,fdq_notifications,fdq_maintenance

# Terminal 3
celery -A fdq_commons.tasks.celery_app beat --loglevel=info

# Terminal 4 — run any service (Django WSGI)
python manage.py runserver 0.0.0.0:8001  # Activity Logging (8001)
python manage.py runserver 0.0.0.0:8002  # Error Logging (8002)
python manage.py runserver 0.0.0.0:8003  # Audit Trail (8003)
python manage.py runserver 0.0.0.0:8004  # Notification Service (8004)
```

**Generate a test token:**
```bash
python -c "
import jwt, time
from pathlib import Path
private_key = Path('keys/private.pem').read_text()
now = int(time.time())
token = jwt.encode({'sub':'00000000-0000-0000-0000-000000000001','iat':now,'exp':now+3600,
'scope':'logs:write logs:read audit:append audit:read audit:verify notifications:send notifications:read notifications:configure',
'role':'system_admin'}, private_key, algorithm='RS256')
print(token)
"
```

---

## 5. Database Schema

| Table | Purpose |
|---|---|
| `activity_logs` | Operational actions, async writes |
| `error_logs` | Exceptions, sync writes, deduplication |
| `audit_events` | Immutable hash chain, sync + advisory lock |
| `notification_logs` | Delivery record for every notification |
| `notification_templates` | Jinja2 templates, version-controlled |
| `notification_preferences` | Per-user alert settings |

`prevent_audit_mutation()` PostgreSQL trigger blocks UPDATE/DELETE on `audit_events` — enforced at the database level, not just the API.

---

## 6. Notification Templates — How They Work

Templates live in `notification_templates`, seeded by `002_seed_data.py`. Each row has `subject_template` and `body_template` written in **Jinja2** syntax (e.g. `{{ scan_id }}`).

**Why only PUT, never POST:**

Templates are **pre-seeded** at migration time — every valid `template_id` already exists in the database before any service runs. There is intentionally no "create a new template" endpoint via the API. New templates are added through a migration (a deliberate, reviewed, version-controlled change), not on the fly by a caller. This matches spec §6.3.7: templates are **updated** (content changes, new version), never freely created by arbitrary API calls — that would let any service invent unreviewed, unaudited message content.

`PUT /api/v1/notifications/templates/{template_id}` replaces the body and **increments the version** automatically. Pass `dry_run: true` with `sample_data` to preview the rendered output without saving — useful for testing wording changes before committing them.

---

## 7. Auth Scopes

| Scope | Who |
|---|---|
| `logs:write` / `logs:read` | Internal services / Compliance, Admin |
| `audit:append` / `audit:read` / `audit:verify` | Internal services / DGS, Compliance, Admin |
| `notifications:send` | Internal services |
| `notifications:read` / `notifications:configure` | Admin, Compliance / Admin only |

---

## 8. Known Pending Items

- **Teams webhook** — code complete (`teams_sender.py`, `/teams` endpoint), blocked on org-tier Teams access. Free personal Teams doesn't support Incoming Webhooks; needs either a paid plan or Microsoft's free 90-day Developer Program sandbox.
- **Phase 7** — full integration test (activity log → audit event → notification, end to end), Locust load testing, k8s deployment configs.

---

## 9. Verified Test Endpoints

**Activity Logging (8001)**
```
POST /api/v1/activity-logs/          → 201, async write via Celery
GET  /api/v1/activity-logs/summary   → materialized view, refreshed every 15 min
GET  /api/v1/activity-logs/          → paginated list
GET  /api/v1/activity-logs/{log_id}  → single record
```

**Error Logging (8002)**
```
POST  /api/v1/error-logs/                  → 201, sync write, dedup confirmed
GET   /api/v1/error-logs/stats             → grouped by severity/service/error_code
PATCH /api/v1/error-logs/{id}/status       → resolution workflow
```

**Audit Trail (8003)**
```
POST /api/v1/audit-events/                              → 201, hash chain + advisory lock
GET  /api/v1/audit-events/entity/{type}/{id}             → full chronological history
POST /api/v1/audit-events/verify                         → valid: true confirmed
```

**Notification Service (8004)**
```
POST /api/v1/notifications/email      → 202, real Gmail SMTP delivery confirmed
POST /api/v1/notifications/dispatch   → 202, async via Celery, suppression tested
GET  /api/v1/notifications/{id}/status
GET  /api/v1/notifications/history/
```

All endpoints tested with real data via ThunderClient against live PostgreSQL + Redis + Celery. Idempotency, deduplication, hash chain integrity, and suppression logic all independently verified.

---

## 10. Project Structure

```
fdq/
├── fdq_commons/
│   ├── config.py                  # All env vars — single source of truth
│   ├── logging_setup.py
│   ├── models/                    # errors.py, pagination.py
│   ├── middleware/                 # jwt_auth, rate_limit_headers, request_context, health
│   ├── utils/                      # ip_validator, sanitiser (PII masking)
│   ├── db/                         # session (psycopg2 pool), base_model, redis_client
│   ├── notifications/              # email_sender.py, teams_sender.py
│   └── tasks/                      # celery_app, maintenance
│
├── migrations/versions/
│   ├── 001_baseline_schema.py
│   └── 002_seed_data.py
│
├── services/
│   ├── activity_logging/           ✅
│   ├── error_logging/              ✅
│   ├── audit_trail/                ✅
│   └── notification_service/       ✅
│
└── scripts/run_migrations.py
```

---

## 11. Windows-Specific Notes

- Python 3.14 breaks Celery 5.6 — use **3.12**
- Worker requires `--pool=solo` (prefork fails on Windows)
- If `celerybeat-schedule*` corrupts after sleep/restart, delete all `celerybeat-schedule*` files and restart beat