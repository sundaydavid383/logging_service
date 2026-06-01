"""
migrations/versions/002_seed_data.py
--------------------------------------
Seed reference data required before any service can run.

Inserts:
  1. FDQ Event Type Registry    — from spec Appendix A
     All services MUST use event_type values from this table.
     New event types must be registered here before use.

  2. Default Notification Templates — one per alert type per channel
     Jinja2 templates stored in notification_templates table (spec §6.3.7)
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Event Type Registry Data — spec Appendix A
# ---------------------------------------------------------------------------
_EVENT_TYPES = [
    # (event_type, source_service, description)
    ("SCAN_JOB_TRIGGERED",      "scan_engine",          "A DQ scan job was manually or automatically triggered."),
    ("SCAN_JOB_COMPLETED",      "scan_engine",          "Scan job finished; issues_found count in metadata."),
    ("RECORD_FLAGGED",          "flag_quarantine",       "A customer record was flagged and quarantined."),
    ("RECORD_FIELD_CORRECTED",  "remediation_service",  "A field correction was submitted via any channel."),
    ("RECORD_APPROVED",         "verification_portal",  "DGS approved a corrected field value."),
    ("RECORD_REJECTED",         "verification_portal",  "DGS rejected a submitted correction."),
    ("RECORD_VERIFIED",         "verified_storage",     "Record stored with VERIFIED status."),
    ("RECORD_PROPAGATED",       "etl_orchestrator",     "Verified record pushed to core banking system."),
    ("ETL_JOB_STARTED",         "etl_orchestrator",     "Batch ETL job commenced."),
    ("ETL_JOB_COMPLETED",       "etl_orchestrator",     "ETL batch completed; success/failure counts in metadata."),
    ("ETL_JOB_FAILED",          "etl_orchestrator",     "ETL job failed; error_code and affected_batch_id in metadata."),
    ("RULE_CREATED",            "rule_engine",          "A new validation rule was created."),
    ("RULE_MODIFIED",           "rule_engine",          "An existing rule was updated (new version created)."),
    ("RULE_ACTIVATED",          "rule_engine",          "A rule was activated and will run in subsequent scans."),
    ("USER_LOGIN",              "uam_service",          "User authenticated successfully."),
    ("USER_LOGIN_FAILED",       "uam_service",          "Authentication failed (wrong credentials)."),
    ("USER_PROVISIONED",        "uam_service",          "New user account created."),
    ("USER_ROLE_CHANGED",       "uam_service",          "User role or permissions updated."),
    ("CONFIG_CHANGED",          "admin_console",        "System configuration parameter updated."),
    ("NOTIFICATION_SENT",       "notification_service", "Outbound notification dispatched."),
    ("AUDIT_CHAIN_VERIFIED",    "audit_trail_service",  "Integrity verification completed; result in metadata."),
]

# ---------------------------------------------------------------------------
# Default Notification Templates Data — Jinja2 strings  (spec §6.3.7)
# ---------------------------------------------------------------------------
_TEMPLATES = [
    # (template_id, channel, subject_template, body_template)
    (
        "scan_completed_email",
        "EMAIL",
        "FDQ | Scan Job Completed — {{ scan_id }}",
        """<h2>Scan Job Completed</h2>
<p>Scan ID: <strong>{{ scan_id }}</strong></p>
<p>Issues Found: <strong>{{ issue_count }}</strong></p>
<p>Severity: <strong>{{ severity }}</strong></p>
<p>Completed At: {{ completed_at }}</p>
{% if issue_count > 0 %}
<p style="color:red;">Action required — review flagged records in the FDQ portal.</p>
{% endif %}""",
    ),
    (
        "scan_completed_teams",
        "TEAMS",
        None,
        """{
  "type": "scan_completed",
  "scan_id": "{{ scan_id }}",
  "issue_count": {{ issue_count }},
  "severity": "{{ severity }}",
  "completed_at": "{{ completed_at }}"
}""",
    ),
    (
        "etl_failure_email",
        "EMAIL",
        "FDQ ALERT | ETL Job Failed — {{ batch_id }}",
        """<h2>ETL Job Failure</h2>
<p>Batch ID: <strong>{{ batch_id }}</strong></p>
<p>Error Code: <strong>{{ error_code }}</strong></p>
<p>Affected Records: <strong>{{ affected_records }}</strong></p>
<p>Failed At: {{ failed_at }}</p>
<p>Service: {{ service_name }}</p>
<p>Please investigate immediately. Check error logs for trace_id: {{ trace_id }}</p>""",
    ),
    (
        "etl_failure_teams",
        "TEAMS",
        None,
        """{
  "type": "etl_failure",
  "batch_id": "{{ batch_id }}",
  "error_code": "{{ error_code }}",
  "affected_records": {{ affected_records }},
  "service_name": "{{ service_name }}",
  "trace_id": "{{ trace_id }}"
}""",
    ),
    (
        "sla_breach_email",
        "EMAIL",
        "FDQ CRITICAL | SLA Breach Detected — {{ entity_type }}",
        """<h2>SLA Breach Detected</h2>
<p>Entity Type: <strong>{{ entity_type }}</strong></p>
<p>Entity ID: <strong>{{ entity_id }}</strong></p>
<p>Breach Type: <strong>{{ breach_type }}</strong></p>
<p>Threshold: {{ threshold }}</p>
<p>Current Value: {{ current_value }}</p>
<p>Detected At: {{ detected_at }}</p>""",
    ),
    (
        "critical_issue_email",
        "EMAIL",
        "FDQ CRITICAL | Data Quality Issue — {{ issue_count }} Records Affected",
        """<h2>Critical Data Quality Issue</h2>
<p>Scan ID: <strong>{{ scan_id }}</strong></p>
<p>Affected Records: <strong>{{ issue_count }}</strong></p>
<p>Severity: <strong>CRITICAL</strong></p>
<p>Detected At: {{ detected_at }}</p>
<p>Immediate review required in the FDQ Remediation Portal.</p>""",
    ),
    (
        "user_provisioned_email",
        "EMAIL",
        "FDQ | Your Account Has Been Created",
        """<h2>Welcome to Fiducia DQMS</h2>
<p>Dear {{ full_name }},</p>
<p>Your account has been created with the role: <strong>{{ role }}</strong></p>
<p>Please log in and change your temporary password immediately.</p>
<p>If you did not request this account, contact your system administrator.</p>""",
    ),
    (
        "audit_chain_broken_email",
        "EMAIL",
        "FDQ SECURITY ALERT | Audit Chain Integrity Failure",
        """<h2>CRITICAL: Audit Chain Integrity Failure</h2>
<p>Aggregate Type: <strong>{{ aggregate_type }}</strong></p>
<p>Aggregate ID: <strong>{{ aggregate_id }}</strong></p>
<p>Broken at sequence: <strong>{{ broken_at_sequence }}</strong></p>
<p>Detected At: {{ detected_at }}</p>
<p><strong>Immediate investigation required. Escalate to security team.</strong></p>""",
    ),
]


def upgrade() -> None:
    now = datetime.now(timezone.utc)

    # 1. Insert Event Types securely using parameterized bindings
    for event_type, source_service, description in _EVENT_TYPES:
        stmt = sa.text("""
            INSERT INTO event_type_registry (event_type, source_service, description)
            VALUES (:event_type, :source_service, :description)
            ON CONFLICT (event_type) DO NOTHING
        """)
        op.execute(stmt, {
            "event_type": event_type,
            "source_service": source_service,
            "description": description
        })

    # 2. Insert Notification Templates securely using parameterized bindings
    for template_id, channel, subject_template, body_template in _TEMPLATES:
        stmt = sa.text("""
            INSERT INTO notification_templates
                (template_id, channel, subject_template, body_template, version, is_active, created_at, updated_at)
            VALUES 
                (:template_id, :channel, :subject_template, :body_template, 1, TRUE, :created_at, :updated_at)
            ON CONFLICT (template_id) DO NOTHING
        """)
        op.execute(stmt, {
            "template_id": template_id,
            "channel": channel,
            "subject_template": subject_template,  # Seamlessly maps None to an SQL NULL
            "body_template": body_template,
            "created_at": now,
            "updated_at": now
        })


def downgrade() -> None:
    # Target and remove only the injected template rows during down-revisions
    for template_id, _, _, _ in _TEMPLATES:
        stmt = sa.text("DELETE FROM notification_templates WHERE template_id = :template_id")
        op.execute(stmt, {"template_id": template_id})

    # Clear out seeded registry configurations cleanly without destroying table DDL
    stmt = sa.text("TRUNCATE TABLE event_type_registry CASCADE")
    op.execute(stmt)