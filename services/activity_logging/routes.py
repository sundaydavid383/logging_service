"""
services/activity_logging/routes.py
-------------------------------------
FastAPI router for all Activity Logging endpoints.

Endpoints (spec §3.3):
  POST /api/v1/activity-logs           — record a log entry (async via Celery)
  GET  /api/v1/activity-logs           — query logs with filters + pagination
  GET  /api/v1/activity-logs/summary   — aggregate statistics from mat view
  GET  /api/v1/activity-logs/{log_id}  — retrieve single entry

Auth scopes per spec §2.2:
  logs:write — POST
  logs:read  — all GET endpoints
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.concurrency import run_in_threadpool

from fdq_commons.config import settings
from fdq_commons.middleware.jwt_auth import require_scope
from fdq_commons.middleware.rate_limit_headers import build_rate_limit_headers
from fdq_commons.middleware.request_context import get_correlation_id
from fdq_commons.models.errors import ErrorCode, FDQException
from fdq_commons.models.pagination import PaginatedResponse, PaginationMeta
from fdq_commons.db.redis_client import activity_idempotency_cache

from .schemas import (
    ActivityLogCreate,
    ActivityLogQueryParams,
    ActivityLogRead,
    ActivityLogResponse,
    ActivityLogSummaryItem,
)
from .service import ActivityLoggingService
from .tasks import dispatch_activity_log

log = structlog.get_logger()

router = APIRouter(
    prefix=f"{settings.api_v1_prefix}/activity-logs",
    tags=["Activity Logging"],
)

# Dependency Injection provider for clean service layer creation
def get_activity_service() -> ActivityLoggingService:
    return ActivityLoggingService()


# ---------------------------------------------------------------------------
# POST /api/v1/activity-logs  (spec §3.3.1)
# ---------------------------------------------------------------------------

@router.post(
    "/",
    status_code=201,
    response_model=ActivityLogResponse,
    summary="Record an activity log entry",
    description="Satisfies FR-SEC-05, FR-VER-10. Dispatches write asynchronously via Celery.",
    responses={
        201: {"description": "Entry recorded successfully."},
        400: {"description": "Malformed JSON or missing required fields."},
        401: {"description": "Missing or invalid Bearer token."},
        403: {"description": "Insufficient scope — logs:write required."},
        422: {"description": "Payload fails validation (invalid IP, unknown status)."},
        429: {"description": "Rate limit exceeded."},
        500: {"description": "Internal error — emitted to ELK, never dropped silently."},
    },
)
async def record_activity_log(
    request:          Request,
    body:              ActivityLogCreate,
    idempotency_key:  Optional[str] = Header(None, alias="Idempotency-Key"),
    claims:           dict          = Depends(require_scope("logs:write")),
    svc:              ActivityLoggingService = Depends(get_activity_service), # Added dependency here
) -> ActivityLogResponse:
    """
    Validates the payload against the DB registry synchronously, checks idempotency, 
    dispatches a Celery task, and returns immediately.
    """
    correlation_id = get_correlation_id(request)

    # 1. NEW: Validate event_type synchronously against the DB registry before doing anything else
    # This prevents un-registered events from getting into Celery
    try:
        event_type = body.event_type.upper()
        svc._validate_event_type(event_type, raise_as_fatal=False)
    except FDQException as exc:
        # Re-raise the exact 422 exception to the user
        raise exc

    # 2. Idempotency check — 60s window per spec §3.3.1
    if idempotency_key:
        cached = activity_idempotency_cache.get(idempotency_key)
        if cached:
            log.info("activity_log_idempotency_hit", idempotency_key=idempotency_key)
            return ActivityLogResponse(**cached)

    # 3. Bind a single deterministic ID to synchronize API layer and DB worker
    assigned_id = uuid.uuid4()
    
    payload = body.model_dump(mode="json")
    payload["log_id"] = str(assigned_id) 

    # 4. Dispatch async Celery task — never block on DB write
    try:
        dispatch_activity_log(payload)
    except Exception as exc:
        log.error(
            "activity_log_dispatch_failed",
            error=str(exc),
            correlation_id=correlation_id,
            event_type=body.event_type,
        )
        raise FDQException(
            status_code=500,
            code=ErrorCode.LOG_WRITE_FAILED,
            message="Log write failed. The incident has been recorded.",
            trace_id=correlation_id,
        )
    
    result = ActivityLogResponse(
        log_id=assigned_id,
        created_at=datetime.now(timezone.utc),
    )

    # 5. Cache idempotency result — 60s TTL per spec
    if idempotency_key:
        activity_idempotency_cache.set(
            idempotency_key,
            result.model_dump(mode="json"),
        )

    log.info(
        "activity_log_dispatched",
        event_type=body.event_type,
        correlation_id=correlation_id,
    )
    return result
# ---------------------------------------------------------------------------
# GET /api/v1/activity-logs/summary  (spec §3.3.4)
# ---------------------------------------------------------------------------

@router.get(
    "/summary",
    response_model=list[ActivityLogSummaryItem],
    summary="Aggregate activity log statistics",
)
async def get_activity_summary(
    start_date: Optional[datetime] = Query(None),
    end_date:   Optional[datetime] = Query(None),
    group_by:   str                = Query("event_type"),
    svc:        ActivityLoggingService = Depends(get_activity_service),
    claims:     dict               = Depends(require_scope("logs:read")),
) -> list[ActivityLogSummaryItem]:
    # Safely execute synchronous blocking DB view lookup in background threadpool
    return await run_in_threadpool(
        svc.summary, start_date=start_date, end_date=end_date, group_by=group_by
    )


# ---------------------------------------------------------------------------
# GET /api/v1/activity-logs  (spec §3.3.2)
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=PaginatedResponse[ActivityLogRead],
    summary="Query activity logs",
)
async def list_activity_logs(
    actor_user_id:    Optional[uuid.UUID] = Query(None),
    event_type:       Optional[str]       = Query(None),
    target_entity_id: Optional[str]       = Query(None),
    status:           Optional[str]       = Query(None),
    start_date:       Optional[datetime]  = Query(None),
    end_date:         Optional[datetime]  = Query(None),
    service_name:     Optional[str]       = Query(None),
    page:             int                 = Query(1, ge=1),
    page_size:        int                 = Query(
                                               settings.pagination_default_page_size,
                                               ge=1,
                                               le=settings.pagination_max_page_size_logs,
                                           ),
    after_id:         Optional[uuid.UUID] = Query(None),
    svc:              ActivityLoggingService = Depends(get_activity_service),
    claims:           dict                = Depends(require_scope("logs:read")),
) -> PaginatedResponse[ActivityLogRead]:
    params = ActivityLogQueryParams(
        actor_user_id=actor_user_id,
        event_type=event_type,
        target_entity_id=target_entity_id,
        status=status,
        start_date=start_date,
        end_date=end_date,
        service_name=service_name,
        page=page,
        page_size=min(page_size, settings.pagination_max_page_size_logs),
        after_id=after_id,
    )

    # Prevent event-loop freeze during high-volume cursor scanning
    records, total = await run_in_threadpool(svc.list, params)

    data = [ActivityLogRead(**r.to_dict()) for r in records]

    from fdq_commons.models.pagination import PaginationParams
    pseudo_params = PaginationParams(
        page=page,
        page_size=params.page_size
    )
    # Using explicit keyword args ensures compatibility with the common pagination builder
    return PaginatedResponse.build(data=data, params=pseudo_params, total=total)


# ---------------------------------------------------------------------------
# GET /api/v1/activity-logs/{log_id}  (spec §3.3.3)
# ---------------------------------------------------------------------------

@router.get(
    "/{log_id}",
    response_model=ActivityLogRead,
    summary="Retrieve a single activity log entry",
)
async def get_activity_log(
    log_id:   uuid.UUID,
    svc:      ActivityLoggingService = Depends(get_activity_service),
    claims:   dict = Depends(require_scope("logs:read")),
) -> ActivityLogRead:
    # Keep the thread loop responsive while looking up individual event entries
    record = await run_in_threadpool(svc.get_by_id, log_id)
    return ActivityLogRead(**record.to_dict())