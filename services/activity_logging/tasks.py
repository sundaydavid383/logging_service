"""
services/activity_logging/tasks.py
------------------------------------
Celery tasks for the Activity Logging Service.
"""
from __future__ import annotations

import structlog
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError, Retry

log = structlog.get_logger()


@shared_task(
    # FIXED: Dot separator matches your celery_app.py regex routing perfectly
    name="services.activity_logging.tasks.write_activity_log",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    queue="fdq_logging",
    acks_late=True,
)
def write_activity_log(self, payload: dict) -> None:
    from .schemas import ActivityLogCreate
    from .service import ActivityLoggingService

    try:
        body = ActivityLogCreate(**payload)
        svc  = ActivityLoggingService()
        svc.create(body)

    except (Retry, MaxRetriesExceededError):
        # Allow Celery's internal signaling exceptions to pass through seamlessly
        raise

    except Exception as exc:
        log.error(
            "activity_log_task_failed",
            attempt=self.request.retries + 1,
            max_retries=self.max_retries,
            error=str(exc),
            event_type=payload.get("event_type"),
        )
        try:
            # Clean exponential backoff calculation: 5s, 10s, 20s...
            countdown = int(self.default_retry_delay * (2 ** self.request.retries))
            raise self.retry(exc=exc, countdown=countdown)
        except MaxRetriesExceededError:
            log.critical(
                "activity_log_task_exhausted_dropping",
                error=str(exc),
                event_type=payload.get("event_type"),
            )
            raise


def dispatch_activity_log(payload: dict) -> None:
    """
    Fire-and-forget entry point called by routes.py after any
    meaningful user or system action.
    """
    # FIXED: Pass payload directly as a positional argument array
    write_activity_log.apply_async(
        args=[payload],
        queue="fdq_logging",
    )