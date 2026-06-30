# API Gateway Testing Guide

## Base URL

```
http://localhost:8000
```

All endpoints are prefixed with `/api/v1`.

---



## 0. Pre-Requisite: How Mode Auto-Detection Works

**Previous issue (now resolved):** All five Django processes — gateway and every backend service — previously loaded the *same* `django_project/urls.py`. When port 8000 proxied a request to, e.g., port 8001, port 8001 loaded the same proxy routes and forwarded the request back to itself, producing an infinite loop until the 30-second read timeout expired.

**The fix:** `django_project/urls.py` now auto-detects the service mode from the port in your `runserver` command:

- `python manage.py runserver 127.0.0.1:8000` → **gateway** mode (proxy routes + auth)
- `python manage.py runserver 127.0.0.1:8001` → **activity** mode (business logic only)
- `python manage.py runserver 127.0.0.1:8002` → **error** mode (business logic only)
- `python manage.py runserver 127.0.0.1:8003` → **audit** mode (business logic only)
- `python manage.py runserver 127.0.0.1:8004` → **notification** mode (business logic only)

Each backend process registers **only its own URL routes** — no proxy rules exist on ports 8001–8004, so the loop cannot form. You can also override this explicitly via the `FDQ_SERVICE_MODE` environment variable.

**Requirement:** Start exactly one service per terminal on its matching port. Thundertest Client / Postman calls should always target port **8000** (the gateway) for all protected downstream requests.

---



## 1. Register a New User and Capture the Instant Signup Token

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/auth/signup/`

**Headers:**
- `Content-Type: application/json`

**Request Body:**

```json
{
  "name": "David Engineer",
  "email": "David@fiducia.internal",
  "password": "SecurePass123!",
  "role": "user"
}
```

**Expected Response (201 Created):**

```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "role": "user",
  "scope": "logs:write logs:read audit:append audit:read audit:verify notifications:configure notifications:read notifications:send"
}
```

> **Save the `access_token`.** It is valid for 1 hour and has a Redis-backed transactional limit balance (default: 1000 uses). Copy it into a global variable (e.g. `TOKEN`) for all subsequent downstream requests.

---

## 2. Authenticate an Existing Account via Token Login

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/auth/token/`

**Headers:**
- `Content-Type: application/json`

**Request Body:**

```json
{
  "email": "David@fiducia.internal",
  "password": "SecurePass123!"
}
```

**Expected Response (200 OK):**

```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "role": "user",
  "scope": "logs:write logs:read audit:append audit:read audit:verify notifications:send notifications:read notifications:configure"
}
```

---



## 3. Activity Logging (→ port 8001)

All requests below require `Authorization: Bearer <token>` (scope `logs:read` for GET, `logs:write` for POST).

Use the `TOKEN` captured from Section 1 or 2.

### 3a. Record an Activity Log (Async Architecture)

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/activity-logs/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`
- `Idempotency-Key: springs-demo-key-101` *(use a fresh string for a new test)*

**Request Body:**

```json
{
  "service_name": "springscircle-backend",
  "event_type": "USER_LOGIN",
  "actor_user_id": "4a5b6c7d-8e9f-0a1b-2c3d-4e5f6a7b8c9d",
  "actor_role": "ADMIN",
  "actor_ip_address": "192.168.1.50",
  "actor_device_info": {
    "user_agent": "Mozilla/5.0",
    "device_type": "DESKTOP",
    "os": "Windows"
  },
  "target_entity_type": "USER_PROFILE",
  "target_entity_id": "9999",
  "action": "User login demonstration",
  "status": "SUCCESS"
}
```

**Expected Response (201 Created):**

```json
{
  "log_id": "a1b2c3d4-1234-5678-90ab-cdef01234567",
  "created_at": "2026-06-29T09:15:00Z"
}
```

---

### 3b. Fetch the Real-Time Created Record

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/activity-logs/{LOG_ID_FROM_3a}`

**Headers:**
- `Authorization: Bearer <your_token>`

Query path parameter `LOG_ID_FROM_3a` with the `log_id` returned from the POST above.

**Expected Response (200 OK):**

```json
{
  "log_id": "a1b2c3d4-1234-5678-90ab-cdef01234567",
  "correlation_id": null,
  "session_id": null,
  "service_name": "springscircle-backend",
  "event_type": "USER_LOGIN",
  "actor_user_id": "4a5b6c7d-8e9f-0a1b-2c3d-4e5f6a7b8c9d",
  "actor_role": "ADMIN",
  "actor_ip_address": "192.168.1.50",
  "actor_device_info": {
    "user_agent": "Mozilla/5.0",
    "device_type": "DESKTOP",
    "os": "Windows"
  },
  "target_entity_type": "USER_PROFILE",
  "target_entity_id": "9999",
  "action": "User login demonstration",
  "status": "SUCCESS",
  "failure_reason": null,
  "metadata": null,
  "environment": "development",
  "created_at": "2026-06-29T09:15:00Z"
}
```

---

### 3c. View Analytical Summary Statistics (Materialized View)

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/activity-logs/summary?group_by=event_type`

**Headers:**
- `Authorization: Bearer <your_token>`

**Expected Response (200 OK):** Returns an array of pre-computed aggregates from the PostgreSQL materialized view.

```json
[
  {
    "group_key": "USER_LOGIN",
    "group_by": "event_type",
    "count": 42,
    "failure_count": 2,
    "last_occurrence": "2026-06-29T09:10:00Z"
  }
]
```

---

### 3d. Structured Querying & Pagination Engine

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/activity-logs/?page=1&page_size=10&event_type=USER_LOGIN`

**Headers:**
- `Authorization: Bearer <your_token>`

**Expected Response (200 OK):** Paginated list with full metadata envelope.

```json
{
  "data": [],
  "meta": {
    "page": 1,
    "page_size": 10,
    "total": 0
  }
}
```

---

### 3e. Input Validation Check (Invalid Event Type)

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/activity-logs/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "service_name": "springscircle-backend",
  "event_type": "FAKE_INVALID_EVENT_TYPE",
  "actor_user_id": "4a5b6c7d-8e9f-0a1b-2c3d-4e5f6a7b8c9d",
  "actor_role": "ADMIN",
  "actor_ip_address": "192.168.1.50",
  "target_entity_type": "USER_PROFILE",
  "target_entity_id": "9999",
  "action": "Testing validation barrier",
  "status": "SUCCESS"
}
```

**Expected Response (422 Unprocessable Entity):** The request is blocked at the API level before it reaches the Celery queue.

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "event_type 'FAKE_INVALID_EVENT_TYPE' is not registered. Add it to the event_type_registry table before use.",
    "details": [],
    "trace_id": "..."
  }
}
```

---



## 4. Error Logging (→ port 8002)

All requests below require `Authorization: Bearer <token>` (scope `logs:write` for POST, `logs:read` for GET/PATCH).

Use the same `TOKEN` captured from Section 1 or 2.

### 4a. Fire a Critical Incident (The Ingestion Engine)

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/error-logs/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "service_name": "springscircle-backend",
  "error_code": "ETL_PROPAGATION_FAILED",
  "error_message": "Failed to sync transaction blocks due to database connection timeout.",
  "stack_trace": "Traceback (most recent call last):\n  File \"/services/etl/main.py\", line 45, in execute\n    db.connect()\nTimeoutError: Connection timed out after 3000ms",
  "severity": "CRITICAL",
  "request_context": {
    "endpoint": "/api/v1/dispatch",
    "method": "POST",
    "ip_address": "127.0.0.1"
  }
}
```

**Expected Response (201 Created):**

```json
{
  "error_log_id": "b2c3d4e5-2345-6789-01bc-def234567890",
  "deduplicated": false,
  "recurrence_count": 1
}
```

---

### 4b. Fetch the Real-Time Error Feed

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/error-logs/?page=1&page_size=10`

**Headers:**
- `Authorization: Bearer <your_token>`

Look at the top of the list, find the `ETL_PROPAGATION_FAILED` record you just created, and copy its `id` UUID value for the next steps.

**Expected Response (200 OK):** Paginated feed of error entries.

```json
{
  "data": [],
  "meta": {
    "page": 1,
    "page_size": 10,
    "total": 0
  }
}
```

---

### 4c. Retrieve a Single Error Log Entry (The Deep-Dive Debugger)

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/error-logs/{ERROR_ID_FROM_4b}`

**Headers:**
- `Authorization: Bearer <your_token>`

Replace `{ERROR_ID_FROM_4b}` with the actual `id` UUID from the first error you created.

**Expected Response (200 OK):**

```json
{
  "id": "b2c3d4e5-2345-6789-01bc-def234567890",
  "correlation_id": null,
  "service_name": "springscircle-backend",
  "error_code": "ETL_PROPAGATION_FAILED",
  "error_message": "Failed to sync transaction blocks due to database connection timeout.",
  "stack_trace": "Traceback (most recent call last)...",
  "severity": "CRITICAL",
  "request_context": {
    "endpoint": "/api/v1/dispatch",
    "method": "POST",
    "ip_address": "127.0.0.1"
  },
  "actor_user_id": null,
  "resolution_status": "OPEN",
  "resolved_by": null,
  "resolved_at": null,
  "resolution_notes": null,
  "recurrence_count": 1,
  "first_occurrence": "2026-06-29T09:15:00Z",
  "last_occurrence": "2026-06-29T09:15:00Z",
  "metadata": null,
  "created_at": "2026-06-29T09:15:00Z"
}
```

---

### 4d. The State Machine Transition (Update Status)

**Method:** PATCH  
**URL:** `http://localhost:8000/api/v1/error-logs/{ERROR_ID_FROM_4b}/status`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "resolution_status": "RESOLVED",
  "resolution_notes": "Database connection pooling variables optimized via DevOps settings; transaction buffer limits safely scaled."
}
```

**Expected Response (200 OK):**

```json
{
  "error_log_id": "b2c3d4e5-2345-6789-01bc-def234567890",
  "resolution_status": "RESOLVED",
  "resolved_at": "2026-06-29T09:20:00Z"
}
```

---

### 4e. View Aggregated Error Metrics & Health Checks

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/error-logs/stats?group_by=service_name`

**Headers:**
- `Authorization: Bearer <your_token>`

**Expected Response (200 OK):**

```json
[
  {
    "group_key": "springscircle-backend",
    "total_errors": 1,
    "open_count": 1,
    "critical_count": 1,
    "resolved_count": 0,
    "avg_recurrence": 1.0
  }
]
```

---



## 5. Audit Trail (→ port 8003)

All requests below require `Authorization: Bearer <token>`.

Use the same `TOKEN` captured from Section 1 or 2. Some tests require a specific role claim (`compliance_officer` or `system_admin`) for PII masking checks.

### 5a. Append a Cryptographically Chained Audit Event

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/audit-events/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "idempotency_key": "c4d3b1a2-fa45-442d-b787-3328fe0598a1",
  "event_type": "SCAN_JOB_COMPLETED",
  "aggregate_type": "SCAN_JOB",
  "aggregate_id": "acc_88301fd2",
  "actor_user_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "actor_role": "compliance_officer",
  "actor_ip_address": "192.168.1.45",
  "actor_device_info": {
    "device_type": "DESKTOP",
    "os": "Windows",
    "browser": "Chrome"
  },
  "payload": {
    "exported_fields": ["email", "phone_number", "tax_identifier"],
    "reason": "Regulatory compliance check"
  },
  "schema_version": 1,
  "occurred_at": "2026-06-20T08:15:00Z"
}
```

**Expected Response (201 Created):**

```json
{
  "event_id": "d4e5f6a7-3456-7890-12cd-ef345678901",
  "sequence_number": 1,
  "event_hash": "a1b2c3d4e5f6...",
  "recorded_at": "2026-06-29T09:15:00Z"
}
```

---

### 5b. Verify Idempotency Engine & Anti-Race Guards

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/audit-events/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**
Send the **exact same JSON payload** from 5a above (same `idempotency_key`).

**What to look for:** You receive a `201 Created` status with identical structural data to the first execution. No second row is inserted in PostgreSQL — the idempotency engine detected the duplicate key.

---

### 5c. Extract Chronological Data Lineage (Entity History)

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/audit-events/entity/ACCOUNT_PROFILE/acc_88301fd2?page=1&page_size=10`

**Headers:**
- `Authorization: Bearer <your_token>`

**Expected Response (200 OK):** Chronologically ordered events for the given aggregate.

```json
{
  "data": [],
  "meta": {
    "page": 1,
    "page_size": 10,
    "total": 0
  }
}
```

---

### 5d. Cryptographic Chain Verification

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/audit-events/verify/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "aggregate_type": "ACCOUNT_PROFILE",
  "aggregate_id": "acc_88301fd2",
  "from_sequence": 1,
  "to_sequence": 100
}
```

**Expected Response (200 OK):**

```json
{
  "valid": true,
  "events_verified": 0,
  "first_sequence": null,
  "last_sequence": null,
  "broken_at_sequence": null,
  "verification_timestamp": "2026-06-29T09:16:00Z"
}
```

---

### 5e. Full Database Verification (Async via Celery)

**Step 1 — Trigger the background job:**

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/audit-events/verify-all/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Expected Response (202 Accepted):**

```json
{
  "task_id": "a1b2c3d4-5678-90ab-cdef-012345678901",
  "status": "QUEUED",
  "message": "Full database verification started. Poll /verify-all/{task_id} for the result."
}
```

**Step 2 — Poll the result (replace `{task_id}` above):**

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/audit-events/verify-all/{task_id}`

**Headers:**
- `Authorization: Bearer <your_token>`

---

### 5f. The Immutability Block (Negative Test)

**Method:** DELETE  
**URL:** `http://localhost:8000/api/v1/audit-events/{ANY_EVENT_ID}`

**Headers:**
- `Authorization: Bearer <your_token>`

**Expected Response (405 / 404):** The microservice has no update or delete routes. Any attempt returns a structural HTTP error out of the box. The database trigger `prevent_audit_mutation` also blocks raw UPDATE/DELETE at the PostgreSQL level.

---

### 5g. Field-Level PII Masking (Compliance Auditing)

Run this test **twice** with different credential sets.

**Run 1 — Compliance Officer or System Admin**
**Method:** GET  
**URL:** `http://localhost:8000/api/v1/audit-events/{ANY_EVENT_ID}`

**Headers:**
- `Authorization: Bearer <admin_or_compliance_token>`

**Expected:** The `payload` field contains the unmasked, raw dictionary.

**Run 2 — Standard Engineer**
**Method:** GET  
**URL:** `http://localhost:8000/api/v1/audit-events/{ANY_EVENT_ID}`

**Headers:**
- `Authorization: Bearer <standard_user_token>`

**Expected:** The `payload` field has PII fields replaced with masks (e.g. `***` or `[MASKED]`).

---



## 6. Notification Service (→ port 8004)

All requests below require `Authorization: Bearer <token>` (scope `notifications:config` for write endpoints, `notifications:read` for GET).

Use the same `TOKEN` captured from Section 1 or 2.

### 6a. Send Direct Email

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/notifications/email/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "notification_id": "11111111-0001-0001-0001-000000000001",
  "to": ["your-real-email@gmail.com"],
  "subject": "FDQ Test — Direct Email",
  "html_body": "<h2>FDQ Notification Service</h2><p>Test email from Fiducia DQMS.</p>",
  "text_body": "FDQ Notification Service — test email."
}
```

**Expected Response (202 Accepted):**

```json
{
  "notification_id": "11111111-0001-0001-0001-000000000001",
  "status": "QUEUED",
  "queued_at": "2026-06-29T09:15:00Z"
}
```

Check your inbox for delivery. Watch the Celery worker terminal for `notification_delivered`.

---

### 6b. Dispatch with Template (Tests Celery + Gmail SMTP Together)

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/notifications/dispatch/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "notification_id": "22222222-0002-0002-0002-000000000002",
  "channel": "EMAIL",
  "recipient": "your-real-email@gmail.com",
  "template_id": "scan_completed_email",
  "template_data": {
    "scan_id": "SCAN-001",
    "issue_count": 42,
    "severity": "WARNING",
    "completed_at": "2026-06-14T10:00:00Z"
  },
  "priority": "HIGH",
  "suppress_within_seconds": 0
}
```

**Expected Response (202 Accepted):**

```json
{
  "notification_id": "22222222-0002-0002-0002-000000000002",
  "status": "QUEUED",
  "queued_at": "2026-06-29T09:15:00Z"
}
```

Watch the Celery worker terminal for `notification_delivered`.

---

### 6c. Check Delivery Status

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/notifications/22222222-0002-0002-0002-000000000002/status/`

**Headers:**
- `Authorization: Bearer <your_token>`

**Expected Response (200 OK):**

```json
{
  "notification_id": "22222222-0002-0002-0002-000000000002",
  "channel": "EMAIL",
  "status": "DELIVERED",
  "delivery_attempts": 1,
  "delivered_at": "2026-06-29T09:15:05Z",
  "provider_message_id": "..."
}
```

---

### 6d. Suppression Test

Use a **NEW** `notification_id` each time you dispatch. The suppression key is `template_id` + `recipient` within the window.

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/notifications/dispatch/`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "notification_id": "33333333-0003-0003-0003-000000000003",
  "channel": "EMAIL",
  "recipient": "your-real-email@gmail.com",
  "template_id": "scan_completed_email",
  "template_data": {
    "scan_id": "SCAN-002",
    "issue_count": 5,
    "severity": "INFO",
    "completed_at": "2026-06-14T10:05:00Z"
  },
  "suppress_within_seconds": 300
}
```

**Expected:** Send once → status `DELIVERED`. Send again immediately with a different `notification_id` but the same `template_id`/`recipient` within 300 s → status `SUPPRESSED`.

---

### 6e. Notification History

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/notifications/history`

**Headers:**
- `Authorization: Bearer <your_token>`

**Expected Response (200 OK):** Paginated history log of all dispatched notifications.

```json
{
  "data": [],
  "meta": {
    "page": 1,
    "page_size": 50,
    "total": 0
  }
}
```

---

### 6f. Teams Webhook Integration

Only runs if your Teams webhook URL is configured in `.env`.

**Method:** POST  
**URL:** `http://localhost:8000/api/v1/notifications/teams`

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <your_token>`

**Request Body:**

```json
{
  "notification_id": "44444444-0004-0004-0004-000000000004",
  "channel_key": "dq-alerts",
  "title": "Test Alert",
  "summary": "Testing the Teams webhook integration.",
  "facts": [
    { "name": "Environment", "value": "Local Test" }
  ],
  "severity": "INFO"
}
```

**Expected Response (202 Accepted):**

```json
{
  "notification_id": "44444444-0004-0004-0004-000000000004",
  "status": "QUEUED",
  "queued_at": "2026-06-29T09:15:00Z"
}
```

---



## 7. Expected Error Responses

### Missing Authorization Header (401 Unauthorized)

**Method:** GET  
**URL:** `http://localhost:8000/api/v1/activity-logs/summary/`

**Headers:** *(no Authorization header)*

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Authorization header is missing. Provide a Bearer token.",
    "details": [],
    "trace_id": "c3f1e8a0-1234-5678-90ab-cdef01234567",
    "timestamp": "2026-06-28T19:30:44.123456Z"
  }
}
```

---

### Invalid or Expired Token (401 Unauthorized)

```json
{
  "error": {
    "code": "INVALID_TOKEN",
    "message": "Token signature verification failed.",
    "details": [],
    "trace_id": "d4a2b9c1-2345-6789-01bc-def012345678",
    "timestamp": "2026-06-28T19:31:15.654321Z"
  }
}
```

---

### Token Balance Exhausted (401 Unauthorized)

```json
{
  "error": {
    "code": "INVALID_TOKEN",
    "message": "Token balance exhausted. Request a new token.",
    "details": [],
    "trace_id": "e5b3cad2-3456-7890-12cd-ef0123456789",
    "timestamp": "2026-06-28T19:32:05.987654Z"
  }
}
```

---

### Downstream Service Unreachable (503 Service Unavailable)

```json
{
  "error": {
    "code": "SERVICE_UNAVAILABLE",
    "message": "Downstream service is unreachable.",
    "details": [],
    "trace_id": "f6c4dbe3-4567-8901-23de-f01234567890",
    "timestamp": "2026-06-28T19:33:00.112233Z"
  }
}
```

---



## 8. Quick Test Sequence (Thunder Client / Postman)

Open Thunder Client or Postman and create the requests below in order.

**1. Signup — capture token**

```
POST  http://localhost:8000/api/v1/auth/signup/
```

```json
{
  "name": "Test User",
  "email": "test@fiducia.internal",
  "password": "TestPass123!",
  "role": "user"
}
```

Copy the `access_token` from the response into a collection/environment variable called `TOKEN`.

---

**2. Activity log write (uses 1 token balance)**

```
POST  http://localhost:8000/api/v1/activity-logs/
```

```json
{
  "service_name": "springscircle-backend",
  "event_type": "USER_LOGIN",
  "actor_user_id": "4a5b6c7d-8e9f-0a1b-2c3d-4e5f6a7b8c9d",
  "actor_role": "ADMIN",
  "actor_ip_address": "192.168.1.50",
  "actor_device_info": {
    "user_agent": "Mozilla/5.0",
    "device_type": "DESKTOP",
    "os": "Windows"
  },
  "target_entity_type": "USER_PROFILE",
  "target_entity_id": "9999",
  "action": "User login demonstration",
  "status": "SUCCESS"
}
```

---

**3. Error log write (uses 1 token balance)**

```
POST  http://localhost:8000/api/v1/error-logs/
```

```json
{
  "service_name": "springscircle-backend",
  "error_code": "ETL_PROPAGATION_FAILED",
  "error_message": "Failed to sync transaction blocks due to database connection timeout.",
  "stack_trace": "Traceback (most recent call last):\n  File \"/services/etl/main.py\", line 45, in execute\n    db.connect()\nTimeoutError: Connection timed out after 3000ms",
  "severity": "CRITICAL",
  "request_context": {
    "endpoint": "/api/v1/dispatch",
    "method": "POST",
    "ip_address": "127.0.0.1"
  }
}
```

---

**4. Audit event append (uses 1 token balance)**

```
POST  http://localhost:8000/api/v1/audit-events/
```

```json
{
  "idempotency_key": "c4d3b1a2-fa45-442d-b787-3328fe0598a1",
  "event_type": "SCAN_JOB_COMPLETED",
  "aggregate_type": "SCAN_JOB",
  "aggregate_id": "acc_88301fd2",
  "actor_user_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "actor_role": "compliance_officer",
  "actor_ip_address": "192.168.1.45",
  "actor_device_info": {
    "device_type": "DESKTOP",
    "os": "Windows",
    "browser": "Chrome"
  },
  "payload": {
    "exported_fields": ["email", "phone_number", "tax_identifier"],
    "reason": "Regulatory compliance check"
  },
  "schema_version": 1,
  "occurred_at": "2026-06-20T08:15:00Z"
}
```

---



## 9. Required Headers (All Downstream Requests)

Every request to port 8000 for a downstream endpoint must include:

| Header | Value | Required |
|--------|-------|----------|
| `Content-Type` | `application/json` | Always (skip for GET) |
| `Authorization` | `Bearer <your_token>` | Always |

Optional headers used by specific endpoints:
- `Idempotency-Key` — Activity logs (POST) and Audit events (POST). Prevents duplicate writes on retry. Use a fresh UUID string per new record.

---



## 10. Service Startup Commands

Run **one terminal per service**, matching the port to the service. Auto-detection from the port number selects the correct routing table automatically.

### PowerShell (Windows)

```powershell
# Terminal 1 — API Gateway (port 8000)
python manage.py runserver 127.0.0.1:8000 --nothreading

# Terminal 2 — Activity Logging (port 8001)
python manage.py runserver 127.0.0.1:8001 --nothreading

# Terminal 3 — Error Logging (port 8002)
python manage.py runserver 127.0.0.1:8002 --nothreading

# Terminal 4 — Audit Trail (port 8003)
python manage.py runserver 127.0.0.1:8003 --nothreading

# Terminal 5 — Notification Service (port 8004)
python manage.py runserver 127.0.0.1:8004 --nothreading
```

### Bash (Linux / macOS / Git Bash)

```bash
# Terminal 1 — API Gateway (port 8000)
python manage.py runserver 0.0.0.0:8000 --nothreading

# Terminal 2 — Activity Logging (port 8001)
python manage.py runserver 0.0.0.0:8001 --nothreading

# Terminal 3 — Error Logging (port 8002)
python manage.py runserver 0.0.0.0:8002 --nothreading

# Terminal 4 — Audit Trail (port 8003)
python manage.py runserver 0.0.0.0:8003 --nothreading

# Terminal 5 — Notification Service (port 8004)
python manage.py runserver 0.0.0.0:8004 --nothreading
```

> **Also required:** A Celery worker and Celery beat process in separate terminals (see `README.md`).

---



## 11. Configuration Reference

Gateway behaviour is driven entirely by environment variables consumed via `fdq_commons.config.Settings`. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `FDQ_SERVICE_MODE` | `gateway` | Explicit override for route selection. Auto-detected from port if not set. Valid values: `gateway`, `activity`, `error`, `audit`, `notification`. |
| `GATEWAY_DOMAIN` | `localhost:8000` | Public-facing domain for the gateway |
| `GATEWAY_TOKEN_BALANCE_LIMIT` | `1000` | Max transactions per issued token |
| `SERVICE_ACTIVITY_LOGGING_URL` | `http://localhost:8001` | Downstream activity logging port |
| `SERVICE_ERROR_LOGGING_URL` | `http://localhost:8002` | Downstream error logging port |
| `SERVICE_AUDIT_TRAIL_URL` | `http://localhost:8003` | Downstream audit trail port |
| `SERVICE_NOTIFICATION_SERVICE_URL` | `http://localhost:8004` | Downstream notification port |

No hardcoded values should be introduced outside `fdq_commons/config.py`.
