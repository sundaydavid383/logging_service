"""
services/notification_service/views.py
------------------------------------------
Django views for the Notification Service.

Converted from legacy routes; now served by Django views.
Uses sync views with raw psycopg2 for database access (no ORM).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_http_methods

from fdq_commons.config import settings
from fdq_commons.middleware.django_jwt_auth import require_scope
from fdq_commons.models.errors import ErrorCode, FDQException
from fdq_commons.models.pagination import PaginatedResponse, PaginationParams
from fdq_commons.tasks.celery_app import celery_app

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

log = structlog.get_logger()


def _get_json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        return json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FDQException(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message='Malformed JSON in request body.',
        ) from exc


def _handle_fdq_exception(exc: FDQException) -> JsonResponse:
    from fdq_commons.models.errors import ErrorBody, ErrorEnvelope

    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=exc.fdq_code,
            message=exc.fdq_message,
            details=exc.fdq_details,
            trace_id=exc.fdq_trace_id or str(uuid.uuid4()),
        )
    )
    return JsonResponse(envelope.model_dump(mode='json'), status=exc.status_code)


@require_http_methods(['POST'])
@require_scope('notifications:send')
def dispatch_notification(request: HttpRequest) -> JsonResponse:
    try:
        body_data = _get_json_body(request)
        body = NotificationDispatch(**body_data)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_dispatch_parse_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f'Request validation failed: {str(exc)}',
            )
        )

    try:
        svc = NotificationService()
        existing = svc.check_idempotency(body.notification_id)
        if existing:
            return JsonResponse(
                NotificationDispatchResponse(
                    notification_id=body.notification_id,
                    status=existing['status'],
                    queued_at=existing['created_at'],
                ).model_dump(mode='json'),
                status=202,
            )

        svc.create_log_entry(body, status='QUEUED')
        celery_app.send_task(
            'services.notification_service.tasks.deliver_notification',
            kwargs={
                'notification_log_id': str(body.notification_id),
                'dispatch_payload': body.model_dump(mode='json'),
            },
            queue='fdq_notifications',
            priority={'HIGH': 9, 'NORMAL': 5, 'LOW': 1}.get(body.priority, 5),
        )

        log.info('notification_queued', notification_id=str(body.notification_id), channel=body.channel, recipient=body.recipient)
        result = NotificationDispatchResponse(
            notification_id=body.notification_id,
            status='QUEUED',
            queued_at=datetime.now(timezone.utc),
        )
        return JsonResponse(result.model_dump(mode='json'), status=202)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_dispatch_failed', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to dispatch notification.',
            )
        )


@require_http_methods(['POST'])
@require_scope('notifications:configure')
def send_direct_email(request: HttpRequest) -> JsonResponse:
    try:
        body_data = _get_json_body(request)
        body = DirectEmailRequest(**body_data)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_email_parse_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f'Request validation failed: {str(exc)}',
            )
        )

    from fdq_commons.notifications.email_sender import send_email, EmailSendError

    try:
        dispatch = NotificationDispatch(
            notification_id=body.notification_id,
            channel='EMAIL',
            recipient=','.join(body.to),
            subject=body.subject,
        )
        svc = NotificationService()
        svc.create_log_entry(dispatch, status='QUEUED', body_preview=body.html_body[:500])
        result = send_email(
            to=body.to,
            cc=body.cc,
            subject=body.subject,
            html_body=body.html_body,
            text_body=body.text_body,
        )
        svc.update_log_after_delivery(body.notification_id, 'DELIVERED', provider_message_id=result.get('provider_message_id'))

        response = NotificationDispatchResponse(
            notification_id=body.notification_id,
            status='QUEUED',
            queued_at=datetime.now(timezone.utc),
        )
        return JsonResponse(response.model_dump(mode='json'), status=202)
    except EmailSendError as exc:
        svc.update_log_after_delivery(body.notification_id, 'FAILED', error_message=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=502,
                code=ErrorCode.QUEUE_UNAVAILABLE,
                message=f'Email delivery failed: {str(exc)}',
            )
        )
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_email_send_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to send direct email.',
            )
        )


@require_http_methods(['POST'])
@require_scope('notifications:configure')
def send_direct_teams(request: HttpRequest) -> JsonResponse:
    try:
        body_data = _get_json_body(request)
        body = DirectTeamsRequest(**body_data)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_teams_parse_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f'Request validation failed: {str(exc)}',
            )
        )

    from fdq_commons.notifications.teams_sender import send_teams_message, TeamsSendError

    try:
        dispatch = NotificationDispatch(
            notification_id=body.notification_id,
            channel='TEAMS',
            recipient=body.channel_key,
            subject=body.title,
        )
        svc = NotificationService()
        svc.create_log_entry(dispatch, status='QUEUED')
        send_teams_message(
            channel_key=body.channel_key,
            title=body.title,
            summary=body.summary,
            facts=body.facts,
            severity=body.severity,
        )
        svc.update_log_after_delivery(body.notification_id, 'DELIVERED')
        response = NotificationDispatchResponse(
            notification_id=body.notification_id,
            status='QUEUED',
            queued_at=datetime.now(timezone.utc),
        )
        return JsonResponse(response.model_dump(mode='json'), status=202)
    except TeamsSendError as exc:
        svc.update_log_after_delivery(body.notification_id, 'FAILED', error_message=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=502,
                code=ErrorCode.QUEUE_UNAVAILABLE,
                message=f'Teams delivery failed: {str(exc)}',
            )
        )
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_teams_send_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to send direct Teams message.',
            )
        )


@require_http_methods(['GET'])
@require_scope('notifications:read')
def notification_history(request: HttpRequest) -> JsonResponse:
    try:
        channel = request.GET.get('channel')
        status = request.GET.get('status')
        recipient = request.GET.get('recipient')
        template_id = request.GET.get('template_id')
        start_date = _parse_iso_datetime(request.GET.get('start_date'))
        end_date = _parse_iso_datetime(request.GET.get('end_date'))
        page = int(request.GET.get('page', 1))
        page_size = int(request.GET.get('page_size', settings.pagination_default_page_size))
        page_size = min(page_size, settings.pagination_max_page_size_audit)
    except (ValueError, TypeError, FDQException) as exc:
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f'Invalid query parameter: {str(exc)}',
            )
        )

    try:
        svc = NotificationService()
        rows, total = svc.history(
            channel=channel,
            status=status,
            recipient=recipient,
            template_id=template_id,
            start_date=start_date,
            end_date=end_date,
            page=page,
            page_size=page_size,
        )
        data = [NotificationLogRead(**row).model_dump(mode='json') for row in rows]
        params = PaginationParams(page=page, page_size=page_size, max_page_size=settings.pagination_max_page_size_audit)
        response = PaginatedResponse.build(data=data, params=params, total=total)
        return JsonResponse(response.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_history_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve notification history.',
            )
        )


@require_http_methods(['GET'])
@require_scope('notifications:read')
def get_notification_status(request: HttpRequest, notification_id: uuid.UUID) -> JsonResponse:
    try:
        svc = NotificationService()
        result = svc.get_status(notification_id)
        return JsonResponse(result.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_status_error', error=str(exc), notification_id=str(notification_id))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve notification status.',
            )
        )


@require_http_methods(['PUT'])
@require_scope('notifications:configure')
def update_preferences(request: HttpRequest, user_id: uuid.UUID) -> JsonResponse:
    try:
        body_data = _get_json_body(request)
        body = NotificationPreferences(**body_data)
        svc = NotificationService()
        result = svc.upsert_preferences(user_id=user_id, prefs=body)
        return JsonResponse(result, status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('notification_preferences_error', error=str(exc), user_id=str(user_id))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to update notification preferences.',
            )
        )


@require_http_methods(['GET', 'PUT'])
@require_scope('notifications:configure')
def notification_template(request: HttpRequest, template_id: str) -> JsonResponse:
    try:
        svc = NotificationService()
        if request.method == 'GET':
            result = svc.get_template(template_id)
            return JsonResponse(result.model_dump(mode='json'), status=200)

        body_data = _get_json_body(request)
        body = NotificationTemplateUpdate(**body_data)
        from fdq_commons.middleware.jwt_auth import parse_caller
        caller = parse_caller(request.claims)
        updated_by = uuid.UUID(caller.user_id) if caller.user_id else None
        result = svc.update_template(template_id=template_id, body=body, updated_by=updated_by)
        return JsonResponse(result.model_dump(mode='json') if hasattr(result, 'model_dump') else result, status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        if request.method == 'GET':
            log.error('notification_template_error', error=str(exc), template_id=template_id)
            message = 'Failed to retrieve notification template.'
        else:
            log.error('notification_template_update_error', error=str(exc), template_id=template_id)
            message = 'Failed to update notification template.'
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message=message,
            )
        )


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise FDQException(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=f'Invalid ISO 8601 datetime: {value}',
        ) from exc
