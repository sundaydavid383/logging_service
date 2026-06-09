"""
fdq_commons/tasks/maintenance.py
----------------------------------
Scheduled maintenance tasks run by Celery beat.

Tasks:
  1. refresh_activity_summary  — refreshes the activity_logs_summary
                                  materialized view every 15 minutes
                                  (spec §3.3.4, replaces pg_cron)

  2. verify_audit_chain_integrity — weekly audit chain check across all
                                     aggregates (spec §11.2). If the chain
                                     is broken, fires a CRITICAL error log
                                     and sends a Teams security alert.

  3. beat_heartbeat             — lightweight task proving beat is alive.
                                  Logged to ELK so ops can alert on absence.
"""

from __future__ import annotations

import structlog
from datetime import datetime, timezone, timedelta
from celery import shared_task

from fdq_commons.db.session import db_connection
from fdq_commons.config import settings

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Task 1 — Materialized view refresh  (spec §3.3.4)
# ---------------------------------------------------------------------------

@shared_task(
    name="fdq_commons.tasks.maintenance.refresh_activity_summary",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    queue="fdq_maintenance",
)
def refresh_activity_summary(self) -> dict:
    """
    Refresh the activity_logs_summary materialized view.

    CONCURRENTLY means the view is not locked during refresh — reads
    continue to be served from the old data while the new data is computed.
    Requires at least one unique index on the view (idx_als_group_key).

    Called every 15 minutes by Celery beat — replaces pg_cron.
    """
    import time
    start = time.perf_counter()

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "REFRESH MATERIALIZED VIEW CONCURRENTLY activity_logs_summary"
                )
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log.info(
            "materialized_view_refreshed",
            view="activity_logs_summary",
            duration_ms=duration_ms,
        )
        return {"status": "ok", "duration_ms": duration_ms}

    except Exception as exc:
        log.error(
            "materialized_view_refresh_failed",
            view="activity_logs_summary",
            error=str(exc),
        )
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Task 2 — Weekly audit chain integrity verification  (spec §11.2)
# ---------------------------------------------------------------------------

@shared_task(
    name="fdq_commons.tasks.maintenance.verify_audit_chain_integrity",
    bind=True,
    max_retries=1,
    queue="fdq_maintenance",
)
def verify_audit_chain_integrity(self) -> dict:
    """
    Re-verify the hash chain for all audit aggregates touched in the
    last 7 days (configurable via settings.audit_integrity_check_days).
    """
    

    log.info("audit_chain_integrity_check_started")

    window_days = settings.audit_integrity_check_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    broken_aggregates = []

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                # Find all distinct aggregates with events in the window
                cur.execute("""
                    SELECT DISTINCT aggregate_type, aggregate_id
                    FROM audit_events
                    WHERE recorded_at >= %s
                    ORDER BY aggregate_type, aggregate_id
                """, (cutoff,))
                aggregates = cur.fetchall()

                log.info(
                    "audit_chain_integrity_aggregates_found",
                    count=len(aggregates),
                    window_days=window_days,
                )

                # Reuse the same open connection to prevent pool starvation
                for row in aggregates:
                    agg_type = row["aggregate_type"]
                    agg_id = row["aggregate_id"]
                    broken_at = _verify_chain_for_aggregate(cur, agg_type, agg_id)
                    
                    if broken_at is not None:
                        broken_aggregates.append({
                            "aggregate_type": agg_type,
                            "aggregate_id": agg_id,
                            "broken_at_sequence": broken_at,
                        })

        if broken_aggregates:
            _handle_broken_chains(broken_aggregates)

        log.info(
            "audit_chain_integrity_check_completed",
            aggregates_checked=len(aggregates),
            broken_count=len(broken_aggregates),
        )

        return {
            "status": "ok" if not broken_aggregates else "broken",
            "aggregates_checked": len(aggregates),
            "broken_count": len(broken_aggregates),
            "broken_aggregates": broken_aggregates,
        }

    except Exception as exc:
        log.error("audit_chain_integrity_check_failed", error=str(exc))
        raise self.retry(exc=exc)


def _verify_chain_for_aggregate(
    cur,
    aggregate_type: str,
    aggregate_id: str,
) -> int | None:
    """
    Walk the hash chain for a single aggregate using the shared cursor.
    Returns the sequence_number where the chain breaks, or None if valid.
    """
    import hashlib
    import json

    cur.execute("""
        SELECT
            sequence_number,
            idempotency_key,
            aggregate_id,
            event_type,
            occurred_at,
            payload,
            previous_event_hash,
            event_hash
        FROM audit_events
        WHERE aggregate_type = %s AND aggregate_id = %s
        ORDER BY sequence_number ASC
    """, (aggregate_type, aggregate_id))
    events = cur.fetchall()

    previous_hash: str | None = None

    for event in events:
        # Standardize ISO time format to guarantee hash match regardless of DB driver behavior
        dt = event["occurred_at"]
        if dt.tzinfo is None:
            formatted_date = f"{dt.isoformat()}Z"
        else:
            formatted_date = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        canonical = json.dumps({
            "idempotency_key": str(event["idempotency_key"]),
            "aggregate_id": event["aggregate_id"],
            "event_type": event["event_type"],
            "occurred_at": formatted_date,
            "payload": event["payload"],
            "previous_hash": previous_hash or "",
        }, sort_keys=True, ensure_ascii=True)

        expected_hash = hashlib.new(
            settings.audit_hash_algorithm,
            canonical.encode(),
        ).hexdigest()

        if event["event_hash"] != expected_hash:
            return event["sequence_number"]

        previous_hash = event["event_hash"]

    return None


def _handle_broken_chains(broken_aggregates: list[dict]) -> None:
    """
    Per spec §11.2: log CRITICAL + send Teams security notification
    when a broken chain is detected.
    """


    detected_at = datetime.now(timezone.utc).isoformat()

    for broken in broken_aggregates:
        log.critical(
            "audit_chain_broken",
            aggregate_type=broken["aggregate_type"],
            aggregate_id=broken["aggregate_id"],
            broken_at_sequence=broken["broken_at_sequence"],
            detected_at=detected_at,
        )

    try:
        from fdq_commons.tasks.celery_app import celery_app
        celery_app.send_task(
            "services.notification_service.tasks.send_teams_notification",
            kwargs={
                "channel_key": settings.audit_security_alert_channel,
                "title": "CRITICAL: Audit Chain Integrity Failure",
                "summary": (
                    f"{len(broken_aggregates)} aggregate(s) have a broken hash chain. "
                    "Immediate investigation required."
                ),
                "facts": [
                    {"name": a["aggregate_type"], "value": a["aggregate_id"]}
                    for a in broken_aggregates
                ],
                "severity": "CRITICAL",
            },
            queue="fdq_notifications",
        )
    except Exception as exc:
        log.error("audit_chain_alert_notification_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Task 3 — Beat heartbeat
# ---------------------------------------------------------------------------

@shared_task(
    name="fdq_commons.tasks.maintenance.beat_heartbeat",
    queue="fdq_maintenance",
)
def beat_heartbeat() -> dict:
    """
    Lightweight task that proves Celery beat is alive and processing.
    """
    log.info("celery_beat_heartbeat")
    return {"status": "alive"}