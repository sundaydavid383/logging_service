"""
services/error_logging/views.py
-----------------------------------
Django views for the Error Logging Service.

Converted from legacy routes; now served by Django views.
Uses sync views with raw psycopg2 for database access (no ORM).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_http_methods

from fdq_commons.config import settings
from fdq_commons.middleware.django_jwt_auth import require_scope
from fdq_commons.models.errors import ErrorCode, FDQException
from fdq_commons.models.pagination import PaginatedResponse, PaginationParams

from .schemas import (
    ErrorLogCreate,
    ErrorLogCreateResponse,
    ErrorLogRead,
    ErrorLogStatsItem,
    ErrorLogStatusUpdate,
    ErrorLogStatusResponse,
)
from .service import ErrorLoggingService

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


# ============================================================================
# Root dispatcher: Handle both GET and POST on /api/v1/error-logs
# ============================================================================

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


def error_logs_root(request: HttpRequest) -> JsonResponse:
    """
    Dispatcher for /api/v1/error-logs (POST and GET).

    - POST: record a new error log (requires logs:write scope)
    - GET:  list error logs with filters (requires logs:read scope)
    """
    if request.method == 'POST':
        return record_error_log(request)
    if request.method == 'GET':
        return list_error_logs(request)
    return JsonResponse(
        {"error": {"code": "METHOD_NOT_ALLOWED", "message": f"Method {request.method} not allowed"}},
        status=405,
    )


@require_http_methods(['POST'])
@require_scope('logs:write')
def record_error_log(request: HttpRequest) -> JsonResponse:
    try:
        body_data = _get_json_body(request)
        body = ErrorLogCreate(**body_data)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('error_log_parse_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f'Request validation failed: {str(exc)}',
            )
        )

    try:
        svc = ErrorLoggingService()
        result = svc.create(body)
        return JsonResponse(result.model_dump(mode='json'), status=201)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('error_log_create_failed', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to create error log.',
            )
        )


@require_http_methods(['GET'])
@require_scope('logs:read')
def get_error_stats(request: HttpRequest) -> JsonResponse:
    try:
        start_date = _parse_iso_datetime(request.GET.get('start_date'))
        end_date = _parse_iso_datetime(request.GET.get('end_date'))
        group_by = request.GET.get('group_by', 'service_name')

        svc = ErrorLoggingService()
        items = svc.stats(start_date=start_date, end_date=end_date, group_by=group_by)
        data = [item.model_dump(mode='json') for item in items]
        return JsonResponse(data, safe=False, status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('error_stats_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve error statistics.',
            )
        )


@require_http_methods(['GET'])
@require_scope('logs:read')
def list_error_logs(request: HttpRequest) -> JsonResponse:
    try:
        service_name = request.GET.get('service_name')
        severity = request.GET.getlist('severity') or None
        resolution_status = request.GET.get('resolution_status')
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
        svc = ErrorLoggingService()
        records, total = svc.list(
            service_name=service_name,
            severity=severity,
            resolution_status=resolution_status,
            start_date=start_date,
            end_date=end_date,
            page=page,
            page_size=page_size,
        )
        data = [ErrorLogRead(**record.to_dict()).model_dump(mode='json') for record in records]
        params = PaginationParams(page=page, page_size=page_size, max_page_size=settings.pagination_max_page_size_audit)
        response = PaginatedResponse.build(data=data, params=params, total=total)
        return JsonResponse(response.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('error_log_list_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve error logs.',
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


@require_http_methods(['GET'])
@require_scope('logs:read')
def get_error_log_by_id(request: HttpRequest, error_id: uuid.UUID) -> JsonResponse:
    try:
        svc = ErrorLoggingService()
        record = svc.get_by_id(error_id)
        return JsonResponse(ErrorLogRead(**record.to_dict()).model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('error_log_retrieve_error', error=str(exc), error_id=str(error_id))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve error log.',
            )
        )


@require_http_methods(['PATCH'])
@require_scope('logs:read')
def update_error_status(request: HttpRequest, error_id: uuid.UUID) -> JsonResponse:
    try:
        body_data = _get_json_body(request)
        body = ErrorLogStatusUpdate(**body_data)
        from fdq_commons.middleware.jwt_auth import parse_caller
        caller = parse_caller(request.claims)
        resolver_id = uuid.UUID(caller.user_id) if caller.user_id else None
        svc = ErrorLoggingService()
        result = svc.update_status(error_id=error_id, body=body, resolver_id=resolver_id)
        return JsonResponse(result.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('error_log_status_update_error', error=str(exc), error_id=str(error_id))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to update error status.',
            )
        )
