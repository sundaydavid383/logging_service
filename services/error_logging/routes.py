"""
services/error_logging/routes.py
----------------------------------
FastAPI router for all Error Logging endpoints.

Endpoints (spec §4.3):
  POST  /api/v1/error-logs                        — record an error (sync)
  GET   /api/v1/error-logs                        — query error logs
  GET   /api/v1/error-logs/stats                  — error statistics
  PATCH /api/v1/error-logs/{error_id}/status      — update resolution status

Auth scopes per spec §2.2:
  logs:write — POST
  logs:read  — GET endpoints and PATCH
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, List

import structlog
from fastapi import APIRouter, Depends, Query, Request

from fdq_commons.config import settings
from fdq_commons.middleware.jwt_auth import require_scope, parse_caller
from fdq_commons.middleware.request_context import get_correlation_id
from fdq_commons.models.pagination import PaginatedResponse, PaginationMeta, PaginationParams
from fdq_commons.models.errors import FDQException, ErrorCode

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

router = APIRouter(
    prefix=f"{settings.api_v1_prefix}/error-logs",
    tags=["Error Logging"],
)


# ---------------------------------------------------------------------------
# POST /api/v1/error-logs  (spec §4.3.1)
# Synchronous write — error logs must be recorded immediately
# ---------------------------------------------------------------------------

@router.post(
    "/",
    status_code=201,
    response_model=ErrorLogCreateResponse,
    summary="Record an error log entry",
    description="Satisfies FR-ETL-06. Synchronous write with deduplication.",
    responses={
        201: {"description": "Error recorded. deduplicated=true means recurrence_count was incremented."},
        401: {"description": "Missing or invalid Bearer token."},
        403: {"description": "Insufficient scope — logs:write required."},
        422: {"description": "Payload validation failed."},
    },
)
def record_error_log(
    request: Request,
    body:    ErrorLogCreate,
    claims:  dict = Depends(require_scope("logs:write")),
) -> ErrorLogCreateResponse:
    svc = ErrorLoggingService()
    return svc.create(body)


# ---------------------------------------------------------------------------
# GET /api/v1/error-logs/stats  (spec §4.3.4)
# Must be defined BEFORE /{error_id} so FastAPI does not treat "stats" as UUID
# ---------------------------------------------------------------------------

@router.get(
    "/stats",
    response_model=list[ErrorLogStatsItem],
    summary="Error log statistics",
    description="Group by service_name, severity, or error_code.",
)
def get_error_stats(
    start_date: Optional[datetime] = Query(None),
    end_date:   Optional[datetime] = Query(None),
    group_by:   str                = Query("service_name"),
    claims:     dict               = Depends(require_scope("logs:read")),
) -> list[ErrorLogStatsItem]:
    svc = ErrorLoggingService()
    return svc.stats(start_date=start_date, end_date=end_date, group_by=group_by)


# ---------------------------------------------------------------------------
# GET /api/v1/error-logs  (spec §4.3.2)
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=PaginatedResponse[ErrorLogRead],
    summary="Query error logs",
)
def list_error_logs(
    service_name:      Optional[str]      = Query(None),
    severity:          Optional[List[str]] = Query(None),
    resolution_status: Optional[str]      = Query(None),
    start_date:        Optional[datetime] = Query(None),
    end_date:          Optional[datetime] = Query(None),
    page:              int                = Query(1, ge=1),
    page_size:         int                = Query(
                                               settings.pagination_default_page_size,
                                               ge=1,
                                               le=settings.pagination_max_page_size_audit,
                                           ),
    claims: dict = Depends(require_scope("logs:read")),
) -> PaginatedResponse[ErrorLogRead]:
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
    data = [ErrorLogRead(**r.to_dict()) for r in records]
    params = PaginationParams(page=page, page_size=page_size,
                              max_page_size=settings.pagination_max_page_size_audit)
    return PaginatedResponse.build(data=data, params=params, total=total)


# ---------------------------------------------------------------------------
# PATCH /api/v1/error-logs/{error_id}/status  (spec §4.3.3)
# ---------------------------------------------------------------------------

@router.patch(
    "/{error_id}/status",
    response_model=ErrorLogStatusResponse,
    summary="Update error resolution status",
    description=(
        "Non-destructive — original error data is preserved. "
        "Allowed transitions: OPEN → ACKNOWLEDGED → RESOLVED | SUPPRESSED."
    ),
    responses={
        200: {"description": "Status updated."},
        404: {"description": "Error log not found."},
    },
)
# ---------------------------------------------------------------------------
# GET /api/v1/error-logs/{error_id}  (Individual Log Entry Details)
# ---------------------------------------------------------------------------

@router.get(
    "/{error_id}",
    response_model=ErrorLogRead,
    summary="Retrieve a single error log entry by ID",
    description="Fetches full debugging metadata and stack trace context for an investigation.",
    responses={
        200: {"description": "Detailed log record returned successfully."},
        404: {"description": "Error log record matching this UUID does not exist."},
    },
)
def get_error_log_by_id(
    error_id: uuid.UUID,
    claims: dict = Depends(require_scope("logs:read")),
) -> ErrorLogRead:
    svc = ErrorLoggingService()
    record = svc.get_by_id(error_id)
    return ErrorLogRead(**record.to_dict())

def update_error_status(
    error_id: uuid.UUID,
    body:     ErrorLogStatusUpdate,
    claims:   dict = Depends(require_scope("logs:read")),
) -> ErrorLogStatusResponse:
    caller = parse_caller(claims)
    resolver_id = uuid.UUID(caller.user_id) if caller.user_id else None
    svc = ErrorLoggingService()
    return svc.update_status(error_id=error_id, body=body, resolver_id=resolver_id)