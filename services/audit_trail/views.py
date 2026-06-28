"""
services/audit_trail/views.py
-----------------------------------
Django views for the Audit Trail Service.

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
from fdq_commons.utils.sanitiser import apply_pii_mask
from fdq_commons.tasks.celery_app import celery_app

from .schemas import (
    AuditEventCreate,
    AuditEventRead,
    ChainVerifyRequest,
    ChainVerifyResponse,
    FullChainVerifyResponse,
    FullChainVerifyStatusResponse,
)
from .service import AuditTrailService

log = structlog.get_logger()


def _get_json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        return json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FDQException(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="Malformed JSON in request body.",
        ) from exc


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise FDQException(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=f"Invalid ISO 8601 datetime: {value}",
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


def audit_events_root(request: HttpRequest) -> JsonResponse:
    if request.method == 'POST':
        return append_audit_event(request)
    if request.method == 'GET':
        return list_audit_events(request)
    return JsonResponse(
        {'error': {'code': 'METHOD_NOT_ALLOWED', 'message': f'Method {request.method} not allowed'}},
        status=405,
    )


@require_http_methods(['POST'])
@require_scope('audit:append')
def append_audit_event(request: HttpRequest) -> JsonResponse:
    try:
        body_data = _get_json_body(request)
        body = AuditEventCreate(**body_data)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('audit_append_parse_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Request validation failed: {str(exc)}",
            )
        )

    try:
        svc = AuditTrailService()
        result = svc.append_event(body)
        return JsonResponse(result.model_dump(mode='json'), status=201)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('audit_append_failed', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to append audit event.',
            )
        )


@require_http_methods(['GET'])
@require_scope('audit:read')
def list_audit_events(request: HttpRequest) -> JsonResponse:
    try:
        aggregate_type = request.GET.get('aggregate_type')
        aggregate_id = request.GET.get('aggregate_id')
        event_type = request.GET.get('event_type')
        actor_user_id = request.GET.get('actor_user_id')
        if actor_user_id:
            actor_user_id = uuid.UUID(actor_user_id)

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
                message=f"Invalid query parameter: {str(exc)}",
            )
        )

    try:
        svc = AuditTrailService()
        records, total = svc.list(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            start_date=start_date,
            end_date=end_date,
            page=page,
            page_size=page_size,
        )

        data = [_to_read_model(record, request.claims) for record in records]
        params = PaginationParams(
            page=page,
            page_size=page_size,
            max_page_size=settings.pagination_max_page_size_audit,
        )
        response = PaginatedResponse.build(data=data, params=params, total=total)
        return JsonResponse(response.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('audit_list_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to list audit events.',
            )
        )


@require_http_methods(['GET'])
@require_scope('audit:read')
def get_audit_event(request: HttpRequest, event_id: uuid.UUID) -> JsonResponse:
    try:
        svc = AuditTrailService()
        record = svc.get_by_id(event_id)
        model = _to_read_model(record, request.claims)
        return JsonResponse(model.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('audit_get_error', error=str(exc), event_id=str(event_id))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve audit event.',
            )
        )


@require_http_methods(['POST'])
@require_scope('audit:verify')
def verify_chain(request: HttpRequest) -> JsonResponse:
    try:
        body_data = _get_json_body(request)
        body = ChainVerifyRequest(**body_data)
        svc = AuditTrailService()
        result = svc.verify_chain(body)
        return JsonResponse(result.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('audit_verify_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to verify audit chain.',
            )
        )


@require_http_methods(['POST'])
@require_scope('audit:verify')
def verify_all_chains(request: HttpRequest) -> JsonResponse:
    try:
        from fdq_commons.tasks.maintenance import verify_audit_chain_integrity
        task = verify_audit_chain_integrity.delay()
        response = FullChainVerifyResponse(
            task_id=task.id,
            status='QUEUED',
            message='Full database verification started. Poll /verify-all/{task_id} for the result.',
        )
        return JsonResponse(response.model_dump(mode='json'), status=202)
    except Exception as exc:
        log.error('audit_verify_all_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to queue full audit verification.',
            )
        )


@require_http_methods(['GET'])
@require_scope('audit:verify')
def get_verify_all_status(request: HttpRequest, task_id: str) -> JsonResponse:
    try:
        task = celery_app.AsyncResult(task_id)
        if task.state == 'PENDING':
            result = FullChainVerifyStatusResponse(task_id=task_id, status='PENDING')
        elif task.state == 'FAILURE':
            result = FullChainVerifyStatusResponse(task_id=task_id, status='FAILURE')
        elif task.state == 'SUCCESS':
            payload = task.result or {}
            result = FullChainVerifyStatusResponse(
                task_id=task_id,
                status='SUCCESS',
                aggregates_checked=payload.get('aggregates_checked'),
                broken_count=payload.get('broken_count'),
                broken_aggregates=payload.get('broken_aggregates'),
            )
        else:
            result = FullChainVerifyStatusResponse(task_id=task_id, status=task.state)
        return JsonResponse(result.model_dump(mode='json'), status=200)
    except Exception as exc:
        log.error('audit_verify_all_status_error', error=str(exc), task_id=task_id)
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve verification task status.',
            )
        )


@require_http_methods(['GET'])
@require_scope('audit:read')
def get_entity_history(request: HttpRequest, aggregate_type: str, aggregate_id: str) -> JsonResponse:
    try:
        from_sequence = request.GET.get('from_sequence')
        to_sequence = request.GET.get('to_sequence')
        if from_sequence is not None:
            from_sequence = int(from_sequence)
        if to_sequence is not None:
            to_sequence = int(to_sequence)
        event_type = request.GET.get('event_type')
        page = int(request.GET.get('page', 1))
        page_size = int(request.GET.get('page_size', settings.pagination_default_page_size))
        page_size = min(page_size, settings.pagination_max_page_size_audit)
    except (ValueError, TypeError) as exc:
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Invalid query parameter: {str(exc)}",
            )
        )

    try:
        svc = AuditTrailService()
        records, total = svc.get_entity_history(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            from_sequence=from_sequence,
            to_sequence=to_sequence,
            event_type=event_type,
            page=page,
            page_size=page_size,
        )
        data = [_to_read_model(record, request.claims) for record in records]
        params = PaginationParams(
            page=page,
            page_size=page_size,
            max_page_size=settings.pagination_max_page_size_audit,
        )
        response = PaginatedResponse.build(data=data, params=params, total=total)
        return JsonResponse(response.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('audit_entity_history_error', error=str(exc), aggregate_type=aggregate_type, aggregate_id=aggregate_id)
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve entity history.',
            )
        )


_UNMASKED_ROLES = {'system_admin', 'compliance_officer', 'dgs'}


def _to_read_model(record: Any, claims: dict[str, Any]) -> AuditEventRead:
    data = record.to_dict()
    role = (claims.get('role') or '').lower()
    if role not in _UNMASKED_ROLES and isinstance(data.get('payload'), dict):
        masked_payload = {}
        for key, value in data['payload'].items():
            masked_payload[key] = apply_pii_mask(value) if isinstance(value, dict) else value
        data['payload'] = masked_payload
    return AuditEventRead(**data)
