"""
services/activity_logging/views.py
-----------------------------------
Django views for Activity Logging Service endpoints.

Converted from legacy routes; now served by Django views.
Uses sync views with raw psycopg2 for database access (no ORM).

Endpoints:
  POST /api/v1/activity-logs           — record a log entry (async via Celery)
  GET  /api/v1/activity-logs           — query logs with filters + pagination
  GET  /api/v1/activity-logs/summary   — aggregate statistics from mat view
  GET  /api/v1/activity-logs/{log_id}  — retrieve single entry

Auth scopes per spec §2.2:
  logs:write — POST
  logs:read  — all GET endpoints
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
from fdq_commons.db.redis_client import activity_idempotency_cache

from .schemas import (
    ActivityLogCreate,
    ActivityLogQueryParams,
    ActivityLogRead,
    ActivityLogResponse,
)
from .service import ActivityLoggingService
from .tasks import dispatch_activity_log

log = structlog.get_logger()


# ============================================================================
# Helper: Extract JSON body from Django request
# ============================================================================

def _get_json_body(request: HttpRequest) -> dict[str, Any]:
    """Parse JSON request body with error handling."""
    try:
        return json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FDQException(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="Malformed JSON in request body.",
        ) from exc


def _get_correlation_id_from_request(request: HttpRequest) -> str:
    """Extract correlation_id from request context."""
    return getattr(request, 'fdq_context', {}).get('correlation_id', str(uuid.uuid4()))


# ============================================================================
# Root dispatcher: Handle both GET and POST on /api/v1/activity-logs
# ============================================================================

# ============================================================================
# Helper: Convert FDQException to Django response
# ============================================================================

def _handle_fdq_exception(exc: FDQException) -> JsonResponse:
    """Convert FDQException to Django JSON error response."""
    from fdq_commons.models.errors import ErrorEnvelope, ErrorBody
    
    error_body = ErrorBody(
        code=exc.code,
        message=exc.message,
        details=[],
        trace_id=exc.trace_id,
    )
    envelope = ErrorEnvelope(error=error_body)
    
    return JsonResponse(
        envelope.model_dump(mode='json'),
        status=exc.status_code,
    )


@require_http_methods(['POST'])
@require_scope('logs:write')
def _handle_post_activity_log(request: HttpRequest) -> JsonResponse:
    """POST /api/v1/activity-logs — record a new activity log."""
    try:
        body_data = _get_json_body(request)
        body = ActivityLogCreate(**body_data)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('activity_log_parse_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f'Request validation failed: {str(exc)}',
            )
        )

    correlation_id = _get_correlation_id_from_request(request)
    svc = ActivityLoggingService()

    try:
        event_type = body.event_type.upper()
        svc._validate_event_type(event_type, raise_as_fatal=False)
    except FDQException as exc:
        return _handle_fdq_exception(exc)

    idempotency_key = request.META.get('HTTP_IDEMPOTENCY_KEY')
    if idempotency_key:
        cached = activity_idempotency_cache.get(idempotency_key)
        if cached:
            log.info('activity_log_idempotency_hit', idempotency_key=idempotency_key)
            result_data = cached if isinstance(cached, dict) else json.loads(cached)
            return JsonResponse(result_data, status=201)

    assigned_id = str(uuid.uuid4())
    payload = body.model_dump(mode='json')
    payload['log_id'] = assigned_id

    try:
        dispatch_activity_log(payload)
    except Exception as exc:
        log.error(
            'activity_log_dispatch_failed',
            error=str(exc),
            correlation_id=correlation_id,
            event_type=body.event_type,
        )
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.LOG_WRITE_FAILED,
                message='Log write failed. The incident has been recorded.',
                trace_id=correlation_id,
            )
        )

    result = ActivityLogResponse(
        log_id=uuid.UUID(assigned_id),
        created_at=datetime.now(timezone.utc),
    )

    if idempotency_key:
        activity_idempotency_cache.set(
            idempotency_key,
            result.model_dump(mode='json'),
            ttl=settings.idempotency_ttl_activity,
        )

    log.info(
        'activity_log_dispatched',
        event_type=body.event_type,
        correlation_id=correlation_id,
    )

    return JsonResponse(result.model_dump(mode='json'), status=201)


@require_http_methods(['GET'])
@require_scope('logs:read')
def _handle_get_activity_logs_list(request: HttpRequest) -> JsonResponse:
    """GET /api/v1/activity-logs — list activity logs."""
    try:
        actor_user_id = request.GET.get('actor_user_id')
        if actor_user_id:
            actor_user_id = uuid.UUID(actor_user_id)

        event_type = request.GET.get('event_type')
        target_entity_id = request.GET.get('target_entity_id')
        status = request.GET.get('status')

        start_date = None
        end_date = None
        start_date_str = request.GET.get('start_date')
        if start_date_str:
            start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
        end_date_str = request.GET.get('end_date')
        if end_date_str:
            end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))

        service_name = request.GET.get('service_name')
        page = int(request.GET.get('page', 1))
        page_size = int(request.GET.get('page_size', settings.pagination_default_page_size))
        page_size = min(page_size, settings.pagination_max_page_size_logs)
        after_id = request.GET.get('after_id')
        if after_id:
            after_id = uuid.UUID(after_id)

        params = ActivityLogQueryParams(
            actor_user_id=actor_user_id,
            event_type=event_type,
            target_entity_id=target_entity_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            service_name=service_name,
            page=page,
            page_size=page_size,
            after_id=after_id,
        )
    except (ValueError, TypeError, uuid.InvalidOperation) as exc:
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f'Invalid query parameter: {str(exc)}',
            )
        )

    try:
        svc = ActivityLoggingService()
        records, total = svc.list(params)
        data = [ActivityLogRead(**r.to_dict()).model_dump(mode='json') for r in records]

        pseudo_params = PaginationParams(page=page, page_size=params.page_size)
        paginated_response = PaginatedResponse.build(data=data, params=pseudo_params, total=total)
        return JsonResponse(paginated_response.model_dump(mode='json'), status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error('activity_list_error', error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message='Failed to retrieve activity logs.',
            )
        )


# ============================================================================
# Root endpoint: POST and GET on /api/v1/activity-logs
# ============================================================================

def activity_logs_root(request: HttpRequest) -> JsonResponse:
    """
    Dispatcher view for /api/v1/activity-logs (POST and GET).

    - POST: Record a new activity log (requires logs:write scope)
    - GET: List activity logs (requires logs:read scope)
    """
    if request.method == 'POST':
        return _handle_post_activity_log(request)
    if request.method == 'GET':
        return _handle_get_activity_logs_list(request)
    return JsonResponse(
        {"error": {"code": "METHOD_NOT_ALLOWED", "message": f"Method {request.method} not allowed"}},
        status=405,
    )


# ============================================================================
# GET /api/v1/activity-logs/summary  (spec §3.3.4)
# ============================================================================

@require_http_methods(["GET"])
@require_scope("logs:read")
def get_activity_summary(request: HttpRequest) -> JsonResponse:
    """
    Retrieve aggregate activity log statistics from materialized view.
    """
    # Parse query parameters
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    group_by = request.GET.get('group_by', 'event_type')
    
    start_date = None
    end_date = None
    
    try:
        if start_date_str:
            start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
        if end_date_str:
            end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
    except ValueError as exc:
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Invalid date format: {str(exc)}",
            )
        )
    
    try:
        svc = ActivityLoggingService()
        results = svc.summary(start_date=start_date, end_date=end_date, group_by=group_by)
        
        # Convert dataclass results to dicts for JSON serialization
        response_data = [
            result.model_dump(mode='json') if hasattr(result, 'model_dump') else result.__dict__
            for result in results
        ]
        
        return JsonResponse(response_data, safe=False, status=200)
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error("activity_summary_error", error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="Failed to retrieve summary.",
            )
        )


# ============================================================================
# GET /api/v1/activity-logs  (spec §3.3.2)
# ============================================================================

@require_http_methods(["GET"])
@require_scope("logs:read")
def list_activity_logs(request: HttpRequest) -> JsonResponse:
    """
    Query activity logs with filters and pagination.
    """
    # Parse query parameters
    try:
        actor_user_id = request.GET.get('actor_user_id')
        if actor_user_id:
            actor_user_id = uuid.UUID(actor_user_id)
        
        event_type = request.GET.get('event_type')
        target_entity_id = request.GET.get('target_entity_id')
        status = request.GET.get('status')
        
        start_date_str = request.GET.get('start_date')
        start_date = None
        if start_date_str:
            start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
        
        end_date_str = request.GET.get('end_date')
        end_date = None
        if end_date_str:
            end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
        
        service_name = request.GET.get('service_name')
        
        page = int(request.GET.get('page', 1))
        page_size = int(request.GET.get('page_size', settings.pagination_default_page_size))
        page_size = min(page_size, settings.pagination_max_page_size_logs)
        
        after_id = request.GET.get('after_id')
        if after_id:
            after_id = uuid.UUID(after_id)
        
        params = ActivityLogQueryParams(
            actor_user_id=actor_user_id,
            event_type=event_type,
            target_entity_id=target_entity_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            service_name=service_name,
            page=page,
            page_size=page_size,
            after_id=after_id,
        )
    except (ValueError, uuid.InvalidOperation) as exc:
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Invalid query parameter: {str(exc)}",
            )
        )
    
    try:
        svc = ActivityLoggingService()
        records, total = svc.list(params)
        
        data = [ActivityLogRead(**r.to_dict()) for r in records]
        
        pseudo_params = PaginationParams(
            page=page,
            page_size=params.page_size
        )
        
        paginated_response = PaginatedResponse.build(
            data=data,
            params=pseudo_params,
            total=total,
        )
        
        return JsonResponse(
            paginated_response.model_dump(mode='json'),
            status=200,
        )
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error("activity_list_error", error=str(exc))
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="Failed to retrieve activity logs.",
            )
        )


# ============================================================================
# GET /api/v1/activity-logs/{log_id}  (spec §3.3.3)
# ============================================================================

@require_http_methods(["GET"])
@require_scope("logs:read")
def get_activity_log(request: HttpRequest, log_id: str) -> JsonResponse:
    """
    Retrieve a single activity log entry by ID.
    """
    try:
        log_uuid = uuid.UUID(log_id)
    except (ValueError, uuid.InvalidOperation):
        return _handle_fdq_exception(
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Invalid UUID: {log_id}",
            )
        )
    
    try:
        svc = ActivityLoggingService()
        record = svc.get_by_id(log_uuid)
        
        if not record:
            return _handle_fdq_exception(
                FDQException(
                    status_code=404,
                    code=ErrorCode.NOT_FOUND,
                    message=f"Activity log entry '{log_id}' not found.",
                )
            )
        
        return JsonResponse(
            ActivityLogRead(**record.to_dict()).model_dump(mode='json'),
            status=200,
        )
    except FDQException as exc:
        return _handle_fdq_exception(exc)
    except Exception as exc:
        log.error("activity_get_error", error=str(exc), log_id=log_id)
        return _handle_fdq_exception(
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="Failed to retrieve activity log.",
            )
        )
