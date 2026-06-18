"""
services/notification_service/routes.py
-----------------------------------------
FastAPI router for all Notification Service endpoints (spec §6.3).

Endpoints:
  POST /api/v1/notifications/dispatch              — universal dispatch
  POST /api/v1/notifications/email                 — direct email (admin only)
  POST /api/v1/notifications/teams                 — direct Teams (admin only)
  GET  /api/v1/notifications/history               — notification history
  GET  /api/v1/notifications/{id}/status           — delivery status
  PUT  /api/v1/notifications/preferences/{user_id} — user preferences
  GET  /api/v1/notifications/templates/{id}        — get template
  PUT  /api/v1/notifications/templates/{id}        — update template
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Query

from fdq_commons.config import settings
from fdq_commons.middleware.jwt_auth import require_scope, parse_caller
from fdq_commons.models.pagination import PaginatedResponse, PaginationParams

from .schemas import (
    DirectEmailRequest,
    DirectTeamsRequest,
    NotificationDispatch,
    NotificationDispatchResponse,
    NotificationLogRead,
    NotificationPreferences,
    NotificationStatusResponse,
    NotificationTemplateRead,
    NotificationTemplateUpdate,
)
from .service import NotificationService
from .tasks import deliver_notification

log = structlog.get_logger()

router = APIRouter(
    prefix=f"{settings.api_v1_prefix}/notifications",
    tags=["Notification Service"],
)

# ---------------------------------------------------------------------------
# POST /dispatch  (spec §6.3.1) — 202 Accepted, async
# ---------------------------------------------------------------------------

@router.post("/dispatch", status_code=202, response_model=NotificationDispatchResponse,
             summary="Universal notification dispatch")
def dispatch_notification(
    body:   NotificationDispatch,
    claims: dict = Depends(require_scope("notifications:send")),
) -> NotificationDispatchResponse:
    svc = NotificationService()

    existing = svc.check_idempotency(body.notification_id)
    if existing:
        log.info("notification_idempotency_hit", notification_id=str(body.notification_id))
        return NotificationDispatchResponse(
            notification_id=body.notification_id,
            status=existing["status"],
            queued_at=existing["created_at"],
        )

    svc.create_log_entry(body, status="QUEUED")

    # --- THE RIGHT FIX IS HERE ---
    # Instead of deliver_notification.apply_async, we use the explicit celery_app connection:
    from fdq_commons.tasks.celery_app import celery_app
    
    celery_app.send_task(
        "services.notification_service.tasks.deliver_notification",
        kwargs={
            "notification_log_id": str(body.notification_id),
            "dispatch_payload":    body.model_dump(mode="json"),
        },
        queue="fdq_notifications",
        priority={"HIGH": 9, "NORMAL": 5, "LOW": 1}.get(body.priority, 5),
    )
    # ------------------------------

    log.info("notification_queued", notification_id=str(body.notification_id),
             channel=body.channel, recipient=body.recipient)

    return NotificationDispatchResponse(
        notification_id=body.notification_id,
        status="QUEUED",
        queued_at=datetime.now(timezone.utc),
    )
# ---------------------------------------------------------------------------
# POST /email  (spec §6.3.2) — System Admin only
# ---------------------------------------------------------------------------

@router.post("/email", status_code=202, response_model=NotificationDispatchResponse,
             summary="Direct email send — System Admin only")
def send_direct_email(
    body:   DirectEmailRequest,
    claims: dict = Depends(require_scope("notifications:configure")),
) -> NotificationDispatchResponse:
    from fdq_commons.notifications.email_sender import send_email, EmailSendError

    dispatch = NotificationDispatch(
        notification_id=body.notification_id,
        channel="EMAIL",
        recipient=", ".join(body.to),
        subject=body.subject,
    )
    svc = NotificationService()
    svc.create_log_entry(dispatch, status="QUEUED", body_preview=body.html_body[:500])

    try:
        result = send_email(to=body.to, cc=body.cc, subject=body.subject,
                            html_body=body.html_body, text_body=body.text_body)
        svc.update_log_after_delivery(body.notification_id, "DELIVERED",
                                      provider_message_id=result.get("provider_message_id"))
    except EmailSendError as exc:
        svc.update_log_after_delivery(body.notification_id, "FAILED", error_message=str(exc))

    return NotificationDispatchResponse(
        notification_id=body.notification_id,
        status="QUEUED",
        queued_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# POST /teams  (spec §6.3.3) — System Admin only
# ---------------------------------------------------------------------------

@router.post("/teams", status_code=202, response_model=NotificationDispatchResponse,
             summary="Direct Teams message — System Admin only")
def send_direct_teams(
    body:   DirectTeamsRequest,
    claims: dict = Depends(require_scope("notifications:configure")),
) -> NotificationDispatchResponse:
    from fdq_commons.notifications.teams_sender import send_teams_message, TeamsSendError

    dispatch = NotificationDispatch(
        notification_id=body.notification_id,
        channel="TEAMS",
        recipient=body.channel_key,
        subject=body.title,
    )
    svc = NotificationService()
    svc.create_log_entry(dispatch, status="QUEUED")

    try:
        send_teams_message(channel_key=body.channel_key, title=body.title,
                           summary=body.summary, facts=body.facts, severity=body.severity)
        svc.update_log_after_delivery(body.notification_id, "DELIVERED")
    except TeamsSendError as exc:
        svc.update_log_after_delivery(body.notification_id, "FAILED", error_message=str(exc))

    return NotificationDispatchResponse(
        notification_id=body.notification_id,
        status="QUEUED",
        queued_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# GET /history  (spec §6.3.5) — defined BEFORE /{id}/status
# ---------------------------------------------------------------------------

@router.get("/history", response_model=PaginatedResponse[NotificationLogRead],
            summary="Notification history")
def notification_history(
    channel:     Optional[str]      = Query(None),
    status:      Optional[str]      = Query(None),
    recipient:   Optional[str]      = Query(None),
    template_id: Optional[str]      = Query(None),
    start_date:  Optional[datetime] = Query(None),
    end_date:    Optional[datetime] = Query(None),
    page:        int                = Query(1, ge=1),
    page_size:   int                = Query(settings.pagination_default_page_size, ge=1,
                                           le=settings.pagination_max_page_size_audit),
    claims: dict = Depends(require_scope("notifications:read")),
) -> PaginatedResponse[NotificationLogRead]:
    svc = NotificationService()
    rows, total = svc.history(channel=channel, status=status, recipient=recipient,
                               template_id=template_id, start_date=start_date,
                               end_date=end_date, page=page, page_size=page_size)
    data   = [NotificationLogRead(**r) for r in rows]
    params = PaginationParams(page=page, page_size=page_size,
                              max_page_size=settings.pagination_max_page_size_audit)
    return PaginatedResponse.build(data=data, params=params, total=total)


# ---------------------------------------------------------------------------
# GET /{notification_id}/status  (spec §6.3.4)
# ---------------------------------------------------------------------------

@router.get("/{notification_id}/status", response_model=NotificationStatusResponse,
            summary="Delivery status")
def get_notification_status(
    notification_id: uuid.UUID,
    claims: dict = Depends(require_scope("notifications:read")),
) -> NotificationStatusResponse:
    return NotificationService().get_status(notification_id)


# ---------------------------------------------------------------------------
# PUT /preferences/{user_id}  (spec §6.3.6)
# ---------------------------------------------------------------------------

@router.put("/preferences/{user_id}", summary="Update user notification preferences")
def update_preferences(
    user_id: uuid.UUID,
    body:    NotificationPreferences,
    claims:  dict = Depends(require_scope("notifications:configure")),
) -> dict:
    return NotificationService().upsert_preferences(user_id=user_id, prefs=body)


# ---------------------------------------------------------------------------
# GET /templates/{template_id}  (spec §6.3.7)
# ---------------------------------------------------------------------------

@router.get("/templates/{template_id}", response_model=NotificationTemplateRead,
            summary="Get notification template")
def get_template(
    template_id: str,
    claims: dict = Depends(require_scope("notifications:configure")),
) -> NotificationTemplateRead:
    return NotificationService().get_template(template_id)


# ---------------------------------------------------------------------------
# PUT /templates/{template_id}  (spec §6.3.7)
# ---------------------------------------------------------------------------

@router.put("/templates/{template_id}",
            summary="Update notification template — dry_run=true for preview")
def update_template(
    template_id: str,
    body:        NotificationTemplateUpdate,
    claims:      dict = Depends(require_scope("notifications:configure")),
):
    caller     = parse_caller(claims)
    updated_by = uuid.UUID(caller.user_id) if caller.user_id else None
    return NotificationService().update_template(
        template_id=template_id, body=body, updated_by=updated_by
    )