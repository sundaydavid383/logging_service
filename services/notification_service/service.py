"""
services/notification_service/service.py
------------------------------------------
Service layer for the Notification Service.
Handles idempotency, suppression, template rendering,
notification_logs DB operations, history, preferences, templates.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from jinja2 import Template, TemplateError
from psycopg2.extras import DictCursor

from fdq_commons.config import settings
from fdq_commons.db.session import db_connection
from fdq_commons.models.errors import ErrorCode, FDQException

from .schemas import (
    NotificationDispatch,
    NotificationPreferences,
    NotificationStatusResponse,
    NotificationTemplateRead,
    NotificationTemplateUpdate,
)

log = structlog.get_logger()


class NotificationService:

    def check_idempotency(self, notification_id: uuid.UUID) -> dict | None:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT notification_id, status, created_at
                    FROM notification_logs
                    WHERE notification_id = %(id)s
                      AND created_at >= NOW() - INTERVAL '24 hours'
                """, {"id": str(notification_id)})
                row = cur.fetchone()
        return dict(row) if row else None

    def is_suppressed(self, template_id: str | None, recipient: str, suppress_within_seconds: int) -> bool:
        if not template_id or suppress_within_seconds <= 0:
            return False
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT 1 FROM notification_logs
                    WHERE template_id = %(template_id)s
                      AND recipient   = %(recipient)s
                      AND status NOT IN ('FAILED', 'SUPPRESSED')
                      AND created_at >= NOW() - (%(window)s || ' seconds')::INTERVAL
                    LIMIT 1
                """, {"template_id": template_id, "recipient": recipient, "window": suppress_within_seconds})
                return cur.fetchone() is not None

    def create_log_entry(self, body: NotificationDispatch, status: str = "QUEUED", body_preview: str | None = None) -> uuid.UUID:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    INSERT INTO notification_logs (
                        notification_id, channel, recipient, template_id,
                        subject, body_preview, status,
                        triggered_by_event_id, triggered_by_user_id,
                        metadata, created_at
                    ) VALUES (
                        %(notification_id)s, %(channel)s, %(recipient)s,
                        %(template_id)s, %(subject)s, %(body_preview)s,
                        %(status)s, %(triggered_by_event_id)s,
                        %(triggered_by_user_id)s, %(metadata)s, NOW()
                    )
                    RETURNING id
                """, {
                    "notification_id":       str(body.notification_id),
                    "channel":               body.channel,
                    "recipient":             body.recipient,
                    "template_id":           body.template_id,
                    "subject":               body.subject,
                    "body_preview":          body_preview[:500] if body_preview else None,
                    "status":                status,
                    "triggered_by_event_id": str(body.triggered_by_event_id) if body.triggered_by_event_id else None,
                    "triggered_by_user_id":  str(body.triggered_by_user_id) if body.triggered_by_user_id else None,
                    "metadata":              json.dumps(body.metadata) if body.metadata else None,
                })
                return cur.fetchone()["id"]

    def update_log_after_delivery(self, notification_id: uuid.UUID, status: str, provider_message_id: str | None = None, error_message: str | None = None) -> None:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE notification_logs
                    SET status              = %(status)s,
                        provider_message_id = %(provider_message_id)s,
                        error_message       = %(error_message)s,
                        delivery_attempts   = delivery_attempts + 1,
                        last_attempt_at     = NOW(),
                        delivered_at        = CASE WHEN %(status)s = 'DELIVERED' THEN NOW() ELSE delivered_at END
                    WHERE notification_id = %(notification_id)s
                """, {
                    "notification_id":    str(notification_id),
                    "status":             status,
                    "provider_message_id": provider_message_id,
                    "error_message":      error_message,
                })

    def render_template(self, template_id: str, template_data: dict[str, Any] | None) -> tuple[str | None, str]:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT subject_template, body_template
                    FROM notification_templates
                    WHERE template_id = %(template_id)s AND is_active = TRUE
                """, {"template_id": template_id})
                row = cur.fetchone()
        if not row:
            raise FDQException(status_code=404, code=ErrorCode.NOT_FOUND,
                               message=f"Notification template '{template_id}' not found or inactive.")
        data = template_data or {}
        try:
            rendered_body    = Template(row["body_template"]).render(**data)
            rendered_subject = Template(row["subject_template"]).render(**data) if row["subject_template"] else None
        except TemplateError as exc:
            raise FDQException(status_code=422, code=ErrorCode.VALIDATION_ERROR,
                               message=f"Template rendering failed: {exc}")
        return rendered_subject, rendered_body

    def get_status(self, notification_id: uuid.UUID) -> NotificationStatusResponse:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT notification_id, channel, status, delivery_attempts,
                           delivered_at, provider_message_id
                    FROM notification_logs WHERE notification_id = %(id)s
                """, {"id": str(notification_id)})
                row = cur.fetchone()
        if not row:
            raise FDQException(status_code=404, code=ErrorCode.NOT_FOUND,
                               message=f"Notification '{notification_id}' not found.")
        return NotificationStatusResponse(**dict(row))

    def history(self, channel, status, recipient, template_id, start_date, end_date, page, page_size) -> tuple[list[dict], int]:
        filters: list[str] = []
        values:  dict[str, Any] = {}
        if channel:
            filters.append("channel = %(channel)s"); values["channel"] = channel.upper()
        if status:
            filters.append("status = %(status)s"); values["status"] = status.upper()
        if recipient:
            filters.append("recipient = %(recipient)s"); values["recipient"] = recipient
        if template_id:
            filters.append("template_id = %(template_id)s"); values["template_id"] = template_id
        if start_date:
            filters.append("created_at >= %(start_date)s"); values["start_date"] = start_date
        if end_date:
            filters.append("created_at <= %(end_date)s"); values["end_date"] = end_date
        where  = ("WHERE " + " AND ".join(filters)) if filters else ""
        limit  = min(page_size, settings.pagination_max_page_size_audit)
        offset = (page - 1) * limit
        values["limit"] = limit; values["offset"] = offset
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(f"SELECT COUNT(*) AS total FROM notification_logs {where}", values)
                total = cur.fetchone()["total"]
                cur.execute(f"SELECT * FROM notification_logs {where} ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s", values)
                rows = [dict(r) for r in cur.fetchall()]
        return rows, total

    def get_preferences(self, user_id: uuid.UUID) -> dict:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT * FROM notification_preferences WHERE user_id = %(user_id)s", {"user_id": str(user_id)})
                row = cur.fetchone()
        return dict(row) if row else {}

    def upsert_preferences(self, user_id: uuid.UUID, prefs: NotificationPreferences) -> dict:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    INSERT INTO notification_preferences (
                        user_id, email_enabled, teams_enabled, digest_mode,
                        digest_interval_minutes, suppression_windows, alert_types, updated_at
                    ) VALUES (
                        %(user_id)s, %(email_enabled)s, %(teams_enabled)s, %(digest_mode)s,
                        %(digest_interval_minutes)s, %(suppression_windows)s, %(alert_types)s, NOW()
                    )
                    ON CONFLICT (user_id) DO UPDATE SET
                        email_enabled           = EXCLUDED.email_enabled,
                        teams_enabled           = EXCLUDED.teams_enabled,
                        digest_mode             = EXCLUDED.digest_mode,
                        digest_interval_minutes = EXCLUDED.digest_interval_minutes,
                        suppression_windows     = EXCLUDED.suppression_windows,
                        alert_types             = EXCLUDED.alert_types,
                        updated_at              = NOW()
                    RETURNING *
                """, {
                    "user_id":                str(user_id),
                    "email_enabled":          prefs.email_enabled,
                    "teams_enabled":          prefs.teams_enabled,
                    "digest_mode":            prefs.digest_mode,
                    "digest_interval_minutes": prefs.digest_interval_minutes,
                    "suppression_windows":    json.dumps([w.model_dump() for w in prefs.suppression_windows]),
                    "alert_types":            json.dumps(prefs.alert_types),
                })
                return dict(cur.fetchone())

    def get_template(self, template_id: str) -> NotificationTemplateRead:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT * FROM notification_templates WHERE template_id = %(template_id)s", {"template_id": template_id})
                row = cur.fetchone()
        if not row:
            raise FDQException(status_code=404, code=ErrorCode.NOT_FOUND, message=f"Template '{template_id}' not found.")
        return NotificationTemplateRead(**dict(row))

    def update_template(self, template_id: str, body: NotificationTemplateUpdate, updated_by: uuid.UUID | None) -> NotificationTemplateRead | dict:
        if body.dry_run:
            data = body.sample_data or {}
            try:
                rendered_body    = Template(body.body_template).render(**data)
                rendered_subject = Template(body.subject_template).render(**data) if body.subject_template else None
            except TemplateError as exc:
                raise FDQException(status_code=422, code=ErrorCode.VALIDATION_ERROR, message=f"Template render preview failed: {exc}")
            return {"dry_run": True, "rendered_subject": rendered_subject, "rendered_body": rendered_body}
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    UPDATE notification_templates
                    SET subject_template = %(subject)s,
                        body_template    = %(body)s,
                        version          = version + 1,
                        updated_by       = %(updated_by)s,
                        updated_at       = NOW()
                    WHERE template_id = %(template_id)s
                    RETURNING *
                """, {
                    "subject": body.subject_template, "body": body.body_template,
                    "updated_by": str(updated_by) if updated_by else None,
                    "template_id": template_id,
                })
                row = cur.fetchone()
        if not row:
            raise FDQException(status_code=404, code=ErrorCode.NOT_FOUND, message=f"Template '{template_id}' not found.")
        log.info("notification_template_updated", template_id=template_id, version=row["version"])
        return NotificationTemplateRead(**dict(row))