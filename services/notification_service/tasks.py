"""
services/notification_service/tasks.py
-----------------------------------------
Celery tasks for async notification delivery per spec §6.1.
Suppression logic lives here — not in the caller (spec §6.1).
"""
from __future__ import annotations

import structlog
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from fdq_commons.notifications.email_sender import send_email, EmailSendError
from fdq_commons.notifications.teams_sender import send_teams_message, TeamsSendError
from fdq_commons.config import settings

log = structlog.get_logger()


@shared_task(
    name="services.notification_service.tasks.deliver_notification",
    bind=True,
    max_retries=settings.notification_retry_max,
    default_retry_delay=settings.notification_retry_backoff_base_seconds,
    queue="fdq_notifications",
    acks_late=True,
)
def deliver_notification(self, notification_log_id: str, dispatch_payload: dict) -> dict:
    from .service import NotificationService
    from .schemas import NotificationDispatch

    svc     = NotificationService()
    body    = NotificationDispatch(**dispatch_payload)
    notif_id = body.notification_id

    # Suppression check — spec §6.1: "in the Celery task, not in the caller"
    if svc.is_suppressed(body.template_id, body.recipient, body.suppress_within_seconds):
        svc.update_log_after_delivery(notif_id, "SUPPRESSED",
                                      error_message="Suppressed within window.")
        log.info("notification_suppressed", notification_id=str(notif_id))
        return {"status": "SUPPRESSED"}

    # Render template
    rendered_subject = body.subject
    rendered_body    = None

    if body.template_id:
        try:
            rendered_subject, rendered_body = svc.render_template(body.template_id, body.template_data)
        except Exception as exc:
            svc.update_log_after_delivery(notif_id, "FAILED", error_message=str(exc))
            return {"status": "FAILED", "error": str(exc)}

    # Deliver
    try:
        channel = body.channel

        if channel == "EMAIL":
            if not rendered_body:
                raise ValueError("No body — provide template_id with a body_template.")
            result = send_email(
                to=[body.recipient],
                subject=rendered_subject or "FDQ Notification",
                html_body=rendered_body,
            )
            svc.update_log_after_delivery(notif_id, "DELIVERED",
                                          provider_message_id=result.get("provider_message_id"))

        elif channel == "TEAMS":
            send_teams_message(
                channel_key=body.recipient,
                title=rendered_subject or "FDQ Alert",
                summary=rendered_body or "Notification",
                severity="INFO",
            )
            svc.update_log_after_delivery(notif_id, "DELIVERED")

        elif channel == "DASHBOARD":
            svc.update_log_after_delivery(notif_id, "DELIVERED")

        else:
            raise ValueError(f"Unsupported channel: {channel}")

        log.info("notification_delivered", notification_id=str(notif_id), channel=channel)
        return {"status": "DELIVERED"}

    except (EmailSendError, TeamsSendError, ValueError) as exc:
        log.error("notification_delivery_failed", notification_id=str(notif_id),
                  attempt=self.request.retries + 1, error=str(exc))
        svc.update_log_after_delivery(notif_id, "FAILED", error_message=str(exc))
        try:
            countdown = settings.notification_retry_backoff_base_seconds * (2 ** self.request.retries)
            raise self.retry(exc=exc, countdown=countdown)
        except MaxRetriesExceededError:
            log.critical("notification_max_retries_exceeded", notification_id=str(notif_id))
            return {"status": "FAILED"}


@shared_task(
    name="services.notification_service.tasks.send_teams_notification",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    queue="fdq_notifications",
)
def send_teams_notification(self, channel_key: str, title: str, summary: str,
                             facts: list | None = None, severity: str = "INFO") -> dict:
    """Direct Teams send used by maintenance.py for audit chain alerts."""
    try:
        result = send_teams_message(channel_key=channel_key, title=title,
                                    summary=summary, facts=facts or [], severity=severity)
        log.info("teams_alert_sent", channel_key=channel_key, title=title)
        return result
    except TeamsSendError as exc:
        try:
            raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))
        except MaxRetriesExceededError:
            log.critical("teams_alert_max_retries_exceeded", channel_key=channel_key)
            return {"status": "FAILED"}