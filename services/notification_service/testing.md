Test 1 — Direct email
POST http://localhost:8004/api/v1/notifications/email
Authorization: Bearer <token>
Content-Type: application/json
json{
  "notification_id": "11111111-0001-0001-0001-000000000001",
  "to": ["your-real-email@gmail.com"],
  "subject": "FDQ Test — Direct Email",
  "html_body": "<h2>FDQ Notification Service</h2><p>Test email from Fiducia DQMS.</p>",
  "text_body": "FDQ Notification Service — test email."
}
Expect 202, then check your inbox.

Test 2 — Dispatch with template (tests Celery + Gmail SMTP together)
POST http://localhost:8004/api/v1/notifications/dispatch
Authorization: Bearer <token>
Content-Type: application/json
json{
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
Watch Terminal 2 (worker) for notification_delivered.

Test 3 — Check delivery status
GET http://localhost:8004/api/v1/notifications/22222222-0002-0002-0002-000000000002/status
Authorization: Bearer <token>
Expect status: "DELIVERED".

Test 4 — Suppression (use a NEW notification_id each time you test)
POST http://localhost:8004/api/v1/notifications/dispatch
Authorization: Bearer <token>
Content-Type: application/json
json{
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
Send it once → should be DELIVERED. Send it again immediately with a different notification_id but same template_id/recipient → should be SUPPRESSED.

Test 5 — Notification history
GET http://localhost:8004/api/v1/notifications/history
Authorization: Bearer <token>

Test 6 — Teams (only if your webhook URL is in .env)
POST http://localhost:8004/api/v1/notifications/teams
Authorization: Bearer <token>
Content-Type: application/json
json{
  "notification_id": "44444444-0004-0004-0004-000000000004",
  "channel_key": "dq-alerts",
  "title": "Test Alert",
  "summary": "Testing the Teams webhook integration.",
  "facts": [{"name": "Environment", "value": "Local Test"}],
  "severity": "INFO"
}
