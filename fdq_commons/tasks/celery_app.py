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
if getattr(settings, "redis_url", None) is None:
    redis_host = getattr(settings, "redis_host", "127.0.0.1") or "127.0.0.1"
    redis_port = getattr(settings, "redis_port", 6379) or 6379
    redis_db = getattr(settings, "redis_db", 0) or 0
    computed_broker = f"redis://{redis_host}:{redis_port}/{redis_db}"
else:
    computed_broker = settings.redis_url 
    
celery_app = Celery("fdq")

# ---------------------------------------------------------------------------
# Configuration — all values from settings
# ---------------------------------------------------------------------------
celery_app.conf.update(
    broker_url=computed_broker,        #  Use the computed variable here
    result_backend=computed_broker,    #  Use the computed variable here
    
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

    task_routes={
        "fdq_commons.tasks.maintenance.*": {
            "queue": "fdq_maintenance"
        },
        "services.notification_service.tasks.*": {
            "queue": "fdq_notifications"
        },
        "services.activity_logging.tasks.*": {
            "queue": "fdq_logging"
        },
        "services.error_logging.tasks.*": {
            "queue": "fdq_logging"
        },
    },

    beat_schedule={
        "refresh-activity-summary-view": {
            "task": "fdq_commons.tasks.maintenance.refresh_activity_summary",
            "schedule": crontab(minute="*/15"),
            "options": {"queue": "fdq_maintenance"},
        },
        "weekly-audit-chain-verify": {
            "task": "fdq_commons.tasks.maintenance.verify_audit_chain_integrity",
            "schedule": crontab(
                hour=settings.audit_integrity_check_hour,
                minute=0,
                day_of_week=settings.audit_integrity_check_day,
            ),
            "options": {"queue": "fdq_maintenance"},
        },
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
# import services.error_logging.tasks  # noqa: F401
# import services.audit_trail.tasks  # noqa: F401
import services.notification_service.tasks  # noqa: F401