"""
fdq_commons/tasks/celery_app.py
---------------------------------
Central Celery application for all FDQ async tasks.

All services import this single Celery instance. This ensures:
  - One broker connection config (from settings — no hardcoding)
  - One beat schedule definition (materialized view refresh lives here)
  - Consistent task serialization and retry settings across all services

Celery beat replaces pg_cron for the materialized view refresh.
No Dockerfile changes needed — beat runs as a separate worker process:
    celery -A fdq_commons.tasks.celery_app beat --loglevel=info
    celery -A fdq_commons.tasks.celery_app worker --loglevel=info

Tasks are autodiscovered from every service package that follows the
naming convention:  <service_package>/tasks.py
"""

from __future__ import annotations

import re
from celery import Celery
from celery.schedules import crontab
from kombu import Queue, Exchange

from fdq_commons.config import settings

# ---------------------------------------------------------------------------
# Celery application instance
# ---------------------------------------------------------------------------

celery_app = Celery(
    "fdq",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

# ---------------------------------------------------------------------------
# Configuration — all values from settings
# ---------------------------------------------------------------------------

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    # Task behaviour
    task_acks_late=True,              # acknowledge after completion, not on receipt
    task_reject_on_worker_lost=True,  # re-queue if worker dies mid-task
    task_track_started=True,

    # Result TTL — keep results for 1 hour then expire
    result_expires=settings.celery_result_expires_seconds,

    # Queue routing architectures
    task_default_queue="fdq_default",
    task_queues=(
        Queue(
            "fdq_default",
            Exchange("fdq_default", type="direct"),
            routing_key="fdq_default",
        ),
        Queue(
            "fdq_notifications",          # high-priority queue for notification delivery (Teams/Email)
            Exchange("fdq_notifications", type="direct"),
            routing_key="fdq_notifications",
        ),
        Queue(
            "fdq_logging",                # async log ingestion queue
            Exchange("fdq_logging", type="direct"),
            routing_key="fdq_logging",
        ),
        Queue(
            "fdq_maintenance",            # low-priority queue for scheduled jobs
            Exchange("fdq_maintenance", type="direct"),
            routing_key="fdq_maintenance",
        ),
    ),

    # Hardened Task Routing using explicit string regex mapping to prevent wildcard drops
    task_routes=[
        {
            re.compile(r"fdq_commons\.tasks\.maintenance\..*"): {
                "queue": "fdq_maintenance"
            },
            re.compile(r"services\.notification_service\.tasks\..*"): {
                "queue": "fdq_notifications"
            },
            re.compile(r"services\.activity_logging\.tasks\..*"): {
                "queue": "fdq_logging"
            },
            re.compile(r"services\.error_logging\.tasks\..*"): {
                "queue": "fdq_logging"
            },
        }
    ],

    # ---------------------------------------------------------------------------
    # Celery Beat Schedule — replaces pg_cron (no Dockerfile change needed)
    # ---------------------------------------------------------------------------
    beat_schedule={
        # Refresh materialized view every 15 minutes — spec §3.3.4
        # "Back it with a materialised view refreshed every 15 minutes
        #  to meet the NFR 20.1 response time requirement."
        "refresh-activity-summary-view": {
            "task": "fdq_commons.tasks.maintenance.refresh_activity_summary",
            "schedule": crontab(minute="*/15"),          # every 15 minutes
            "options": {"queue": "fdq_maintenance"},
        },

        # Weekly audit chain integrity verification — spec §11.2
        # "Schedule integrity verification as a weekly automated job
        #  covering the previous 7 days of audit events."
        "weekly-audit-chain-verify": {
            "task": "fdq_commons.tasks.maintenance.verify_audit_chain_integrity",
            "schedule": crontab(
                hour=settings.audit_integrity_check_hour,
                minute=0,
                day_of_week=settings.audit_integrity_check_day,
            ),
            "options": {"queue": "fdq_maintenance"},
        },

        # Heartbeat — proves the beat scheduler is alive (every 5 minutes)
        "beat-heartbeat": {
            "task": "fdq_commons.tasks.maintenance.beat_heartbeat",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "fdq_maintenance"},
        },
    },
)

# ---------------------------------------------------------------------------
# Autodiscover tasks from all service packages
# ---------------------------------------------------------------------------
# Explicit imports — autodiscover only finds files named tasks.py
# maintenance.py must be imported directly so its tasks register
import fdq_commons.tasks.maintenance  # noqa: F401

# Service tasks — uncomment each one as you build the service
import services.activity_logging.tasks  # noqa: F401
# import services.error_logging.tasks
# import services.audit_trail.tasks
# import services.notification_service.tasks