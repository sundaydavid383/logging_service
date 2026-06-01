"""
migrations/versions/001_baseline_schema.py
-------------------------------------------
Baseline migration — creates all Phase 2 tables exactly as specified in the
Fiducia DQMS Technical Specification.
"""

from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ------------------------------------------------------------------
    # 0. ROLE BOOTSTRAPPING & EXTENSION CONFIGURATION
    # fdq_user is created securely by Docker infrastructure on startup.
    # fdq_readonly is custom internal reporting infrastructure and is handled here.
    # ------------------------------------------------------------------
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fdq_readonly') THEN
                CREATE ROLE fdq_readonly;
            END IF;
        END
        $$
    """)

    # Ensure crypto capabilities exist for explicit unique token tasks if required
    op.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";")

    # ------------------------------------------------------------------
    # 1. EVENT TYPE REGISTRY TABLE
    # Base configuration table required for logical validation references.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS event_type_registry (
            id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            event_type      VARCHAR(150) NOT NULL UNIQUE,
            source_service  VARCHAR(100) NOT NULL,
            description     TEXT,
            is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_etr_event_type ON event_type_registry(event_type)")

    # ------------------------------------------------------------------
    # 2. ACTIVITY LOGS  (spec §3.2)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            correlation_id      UUID,
            session_id          UUID,
            service_name        VARCHAR(100) NOT NULL,
            event_type          VARCHAR(100) NOT NULL,
            actor_user_id       UUID,
            actor_role          VARCHAR(80),
            actor_ip_address    INET NOT NULL,
            actor_device_info   JSONB,
            target_entity_type  VARCHAR(100),
            target_entity_id    VARCHAR(255),
            action              VARCHAR(150) NOT NULL,
            status              VARCHAR(20)  NOT NULL
                                CHECK (status IN ('SUCCESS', 'FAILURE', 'PARTIAL')),
            failure_reason      TEXT,
            metadata            JSONB,
            environment         VARCHAR(20)  NOT NULL DEFAULT 'production',
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_al_actor_user   ON activity_logs(actor_user_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_al_entity       ON activity_logs(target_entity_type, target_entity_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_al_event_type   ON activity_logs(event_type, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_al_correlation  ON activity_logs(correlation_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_al_created_at   ON activity_logs(created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_al_service      ON activity_logs(service_name, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_al_status       ON activity_logs(status, created_at DESC)")

    # ------------------------------------------------------------------
    # 3. ERROR LOGS  (spec §4.2)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            correlation_id      UUID,
            service_name        VARCHAR(100) NOT NULL,
            error_code          VARCHAR(100) NOT NULL,
            error_message       TEXT         NOT NULL,
            stack_trace         TEXT,
            severity            VARCHAR(20)  NOT NULL
                                CHECK (severity IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
            request_context     JSONB,
            actor_user_id       UUID,
            resolution_status   VARCHAR(20)  NOT NULL DEFAULT 'OPEN'
                                CHECK (resolution_status IN ('OPEN','ACKNOWLEDGED','RESOLVED','SUPPRESSED')),
            resolved_by         UUID,
            resolved_at         TIMESTAMPTZ,
            resolution_notes    TEXT,
            recurrence_count    INTEGER      NOT NULL DEFAULT 1,
            first_occurrence    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            last_occurrence     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            metadata            JSONB,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_el_service_sev  ON error_logs(service_name, severity, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_el_status       ON error_logs(resolution_status, severity)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_el_correlation  ON error_logs(correlation_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_el_error_code   ON error_logs(error_code, service_name)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_el_created_at   ON error_logs(created_at DESC)")

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_el_dedup
        ON error_logs(error_code, service_name, last_occurrence DESC)
        WHERE resolution_status = 'OPEN'
    """)

    # ------------------------------------------------------------------
    # 4. AUDIT EVENTS  (spec §5.2)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_events (
            id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            sequence_number     BIGSERIAL    NOT NULL UNIQUE,
            idempotency_key     UUID         NOT NULL UNIQUE,
            event_type          VARCHAR(150) NOT NULL,
            aggregate_type      VARCHAR(100) NOT NULL,
            aggregate_id        VARCHAR(255) NOT NULL,
            actor_user_id       UUID,
            actor_role          VARCHAR(80),
            actor_ip_address    INET         NOT NULL,
            actor_device_info   JSONB,
            payload             JSONB        NOT NULL,
            schema_version      INTEGER      NOT NULL DEFAULT 1,
            previous_event_hash VARCHAR(64),
            event_hash          VARCHAR(64)  NOT NULL,
            occurred_at         TIMESTAMPTZ  NOT NULL,
            recorded_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            metadata            JSONB
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_ae_aggregate   ON audit_events(aggregate_type, aggregate_id, sequence_number)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ae_actor       ON audit_events(actor_user_id, occurred_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ae_event_type  ON audit_events(event_type, occurred_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ae_recorded_at ON audit_events(recorded_at DESC)")

    # Trigger Functions for Audit Table Immutability
    op.execute("""
        CREATE OR REPLACE FUNCTION prevent_audit_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'Audit events are immutable. Direct mutation is denied. '
                'This is a regulatory requirement (NDPR, CBN). '
                'Attempted operation: % on audit_events', TG_OP;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        CREATE TRIGGER trg_audit_no_update
        BEFORE UPDATE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation()
    """)

    op.execute("""
        CREATE TRIGGER trg_audit_no_delete
        BEFORE DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation()
    """)

    # ------------------------------------------------------------------
    # 5. NOTIFICATION LOGS  (spec §6.2)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS notification_logs (
            id                      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            notification_id         UUID         NOT NULL UNIQUE,
            channel                 VARCHAR(20)  NOT NULL
                                    CHECK (channel IN ('EMAIL','TEAMS','SMS','DASHBOARD')),
            recipient               VARCHAR(500) NOT NULL,
            template_id             VARCHAR(100),
            subject                 VARCHAR(500),
            body_preview            TEXT,
            status                  VARCHAR(20)  NOT NULL DEFAULT 'QUEUED'
                                    CHECK (status IN ('QUEUED','SENT','DELIVERED','FAILED','SUPPRESSED')),
            provider_message_id     VARCHAR(255),
            delivery_attempts       INTEGER      NOT NULL DEFAULT 0,
            last_attempt_at         TIMESTAMPTZ,
            delivered_at            TIMESTAMPTZ,
            error_message           TEXT,
            triggered_by_event_id   UUID REFERENCES audit_events(id),
            triggered_by_user_id    UUID,
            metadata                JSONB,
            created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_nl_status     ON notification_logs(status, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_nl_channel    ON notification_logs(channel, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_nl_event      ON notification_logs(triggered_by_event_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_nl_recipient  ON notification_logs(recipient, created_at DESC)")

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_nl_suppression
        ON notification_logs(template_id, recipient, created_at DESC)
        WHERE status NOT IN ('FAILED', 'SUPPRESSED')
    """)

    # ------------------------------------------------------------------
    # 6. NOTIFICATION TEMPLATES  (spec §6.3.7)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS notification_templates (
            id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            template_id     VARCHAR(100) NOT NULL UNIQUE,
            channel         VARCHAR(20)  NOT NULL
                            CHECK (channel IN ('EMAIL','TEAMS','SMS','DASHBOARD')),
            subject_template TEXT,
            body_template   TEXT         NOT NULL,
            version         INTEGER      NOT NULL DEFAULT 1,
            is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
            created_by      UUID,
            updated_by      UUID,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_nt_template_id  ON notification_templates(template_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_nt_channel        ON notification_templates(channel)")

    # ------------------------------------------------------------------
    # 7. NOTIFICATION PREFERENCES  (spec §6.3.6)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS notification_preferences (
            id                      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id                 UUID         NOT NULL UNIQUE,
            email_enabled           BOOLEAN      NOT NULL DEFAULT TRUE,
            teams_enabled           BOOLEAN      NOT NULL DEFAULT FALSE,
            sms_enabled             BOOLEAN      NOT NULL DEFAULT FALSE,
            dashboard_enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
            digest_mode             BOOLEAN      NOT NULL DEFAULT FALSE,
            digest_interval_minutes INTEGER      NOT NULL DEFAULT 60,
            suppression_windows     JSONB        NOT NULL DEFAULT '[]',
            alert_types             JSONB        NOT NULL DEFAULT '[]',
            created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_np_user_id ON notification_preferences(user_id)")

    # ------------------------------------------------------------------
    # 8. ROW-LEVEL SECURITY  (spec §10.3)
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE activity_logs ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY fdq_activity_app_policy ON activity_logs
        FOR ALL TO fdq_user USING (TRUE)
    """)
    op.execute("""
        CREATE POLICY fdq_activity_readonly_policy ON activity_logs
        FOR SELECT TO fdq_readonly USING (TRUE)
    """)

    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY fdq_audit_app_policy ON audit_events
        FOR ALL TO fdq_user USING (TRUE)
    """)
    op.execute("""
        CREATE POLICY fdq_audit_readonly_policy ON audit_events
        FOR SELECT TO fdq_readonly USING (TRUE)
    """)

    # ------------------------------------------------------------------
    # 9. MATERIALIZED VIEW — activity_logs_summary  (spec §3.3.4)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS activity_logs_summary AS
        SELECT
            event_type                          AS group_key,
            'event_type'                        AS group_by,
            COUNT(*)                            AS count,
            COUNT(*) FILTER (WHERE status = 'FAILURE') AS failure_count,
            MAX(created_at)                     AS last_occurrence,
            DATE_TRUNC('hour', created_at)      AS time_bucket
        FROM activity_logs
        GROUP BY event_type, DATE_TRUNC('hour', created_at)

        UNION ALL

        SELECT
            actor_role                          AS group_key,
            'actor_role'                        AS group_by,
            COUNT(*)                            AS count,
            COUNT(*) FILTER (WHERE status = 'FAILURE') AS failure_count,
            MAX(created_at)                     AS last_occurrence,
            DATE_TRUNC('hour', created_at)      AS time_bucket
        FROM activity_logs
        WHERE actor_role IS NOT NULL
        GROUP BY actor_role, DATE_TRUNC('hour', created_at)

        UNION ALL

        SELECT
            service_name                        AS group_key,
            'service_name'                      AS group_by,
            COUNT(*)                            AS count,
            COUNT(*) FILTER (WHERE status = 'FAILURE') AS failure_count,
            MAX(created_at)                     AS last_occurrence,
            DATE_TRUNC('hour', created_at)      AS time_bucket
        FROM activity_logs
        GROUP BY service_name, DATE_TRUNC('hour', created_at)

        UNION ALL

        SELECT
            status                              AS group_key,
            'status'                            AS group_by,
            COUNT(*)                            AS count,
            COUNT(*) FILTER (WHERE status = 'FAILURE') AS failure_count,
            MAX(created_at)                     AS last_occurrence,
            DATE_TRUNC('hour', created_at)      AS time_bucket
        FROM activity_logs
        GROUP BY status, DATE_TRUNC('hour', created_at)
        WITH DATA
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_als_concurrent_refresh
        ON activity_logs_summary(group_by, group_key, time_bucket DESC)
    """)
    
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_als_time_bucket
        ON activity_logs_summary(time_bucket DESC)
    """)


def downgrade() -> None:
    # 1. ALWAYS drop views and dependent entities first to avoid blocking locks
    op.execute("DROP MATERIALIZED VIEW IF EXISTS activity_logs_summary")

    # 2. Drop access governance structures safely
    op.execute("DROP POLICY IF EXISTS fdq_activity_app_policy ON activity_logs")
    op.execute("DROP POLICY IF EXISTS fdq_activity_readonly_policy ON activity_logs")
    op.execute("DROP POLICY IF EXISTS fdq_audit_app_policy ON audit_events")
    op.execute("DROP POLICY IF EXISTS fdq_audit_readonly_policy ON audit_events")

    # 3. Drop relational table security checks explicitly
    op.execute("DROP TRIGGER IF EXISTS trg_audit_no_update ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_no_delete ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_mutation()")

    # 4. Drop structures in clean reverse-dependency order
    op.execute("DROP TABLE IF EXISTS notification_preferences CASCADE")
    op.execute("DROP TABLE IF EXISTS notification_templates CASCADE")
    op.execute("DROP TABLE IF EXISTS notification_logs CASCADE")  # Drops before audit_events dependency
    op.execute("DROP TABLE IF EXISTS audit_events CASCADE")
    op.execute("DROP TABLE IF EXISTS error_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS activity_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS event_type_registry CASCADE")