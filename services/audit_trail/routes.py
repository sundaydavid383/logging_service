"""
services/audit_trail/routes.py
----------------------------------
FastAPI router for the Audit Trail / Event Sourcing Service.

Endpoints (spec §5.3):
  POST /api/v1/audit-events                                       — append (audit:append)
  GET  /api/v1/audit-events                                       — query (audit:read)
  GET  /api/v1/audit-events/{event_id}                            — single (audit:read)
  GET  /api/v1/audit-events/entity/{aggregate_type}/{aggregate_id} — full history (audit:read)
  POST /api/v1/audit-events/verify                                — chain verify (audit:verify)

Critical constraint (spec §5.1):
  No PUT, PATCH, or DELETE endpoints exist in this router. This is
  intentional and must never be added.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request

from fdq_commons.config import settings
from fdq_commons.middleware.jwt_auth import require_scope, require_any_scope
from fdq_commons.models.pagination import PaginatedResponse, PaginationParams
from fdq_commons.utils.sanitiser import apply_pii_mask

from .schemas import (
    AuditEventCreate,
    AuditEventCreateResponse,
    AuditEventRead,
    ChainVerifyRequest,
    ChainVerifyResponse,
    FullChainVerifyResponse,
    FullChainVerifyStatusResponse,
)
from .service import AuditTrailService

log = structlog.get_logger()

router = APIRouter(
    prefix=f"{settings.api_v1_prefix}/audit-events",
    tags=["Audit Trail"],
)


# ---------------------------------------------------------------------------
# POST /api/v1/audit-events  (spec §5.3.1)
# ---------------------------------------------------------------------------

@router.post(
    "/",
    status_code=201,
    response_model=AuditEventCreateResponse,
    summary="Append an audit event",
    description=(
        "Satisfies FR-SEC-05, FR-STORE-02/03. Append-only, hash-chained. "
        "Idempotency required via idempotency_key — duplicates within 24h "
        "return the original 201 without re-inserting."
    ),
    responses={
        201: {"description": "Event appended (or idempotent replay returned)."},
        401: {"description": "Missing or invalid Bearer token."},
        403: {"description": "Insufficient scope — audit:append required."},
        422: {"description": "Payload validation failed."},
    },
)
def append_audit_event(
    request: Request,
    body:    AuditEventCreate,
    claims:  dict = Depends(require_scope("audit:append")),
) -> AuditEventCreateResponse:
    svc = AuditTrailService()
    return svc.append_event(body)


# ---------------------------------------------------------------------------
# GET /api/v1/audit-events  (spec §5.3.2)
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=PaginatedResponse[AuditEventRead],
    summary="Query audit events",
)
def list_audit_events(
    aggregate_type: Optional[str]      = Query(None),
    aggregate_id:   Optional[str]      = Query(None),
    event_type:     Optional[str]      = Query(None),
    actor_user_id:  Optional[uuid.UUID] = Query(None),
    start_date:     Optional[datetime] = Query(None),
    end_date:       Optional[datetime] = Query(None),
    page:           int                = Query(1, ge=1),
    page_size:      int                = Query(
                                            settings.pagination_default_page_size,
                                            ge=1,
                                            le=settings.pagination_max_page_size_audit,
                                        ),
    claims: dict = Depends(require_scope("audit:read")),
) -> PaginatedResponse[AuditEventRead]:
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
    data = [_to_read_model(r, claims) for r in records]
    params = PaginationParams(page=page, page_size=page_size,
                              max_page_size=settings.pagination_max_page_size_audit)
    return PaginatedResponse.build(data=data, params=params, total=total)


# ---------------------------------------------------------------------------
# POST /api/v1/audit-events/verify-all  (spec §11.2)
# Full-database hash chain verification — async, for government/regulatory
# on-demand audits. Triggers the same task Celery beat runs weekly, but
# immediately instead of waiting for Sunday.
# ---------------------------------------------------------------------------

@router.post(
    "/verify-all",
    status_code=202,
    response_model=FullChainVerifyResponse,
    summary="Verify hash chain integrity across the ENTIRE database",
    description=(
        "Triggers a full audit chain integrity scan across all aggregates — "
        "for regulatory/government-requested on-demand verification (spec §11.2). "
        "Runs asynchronously via Celery since this can take minutes on a large "
        "dataset. Returns a task_id — poll GET /verify-all/{task_id} for the result. "
        "If any chain is found broken, a CRITICAL log and Teams security alert "
        "are fired automatically."
    ),
)
def verify_all_chains(
    claims: dict = Depends(require_scope("audit:verify")),
) -> FullChainVerifyResponse:
    from fdq_commons.tasks.maintenance import verify_audit_chain_integrity
    print("=== DEBUG CELERY CONF ===")
    print("Broker URL:", verify_audit_chain_integrity.app.conf.broker_url)
    print("All registered tasks:", verify_audit_chain_integrity.app.tasks.keys())
    print("=========================")
    result = verify_audit_chain_integrity.delay()

    log.info("full_chain_verification_triggered", task_id=result.id,
             triggered_by=claims.get("sub"))

    return FullChainVerifyResponse(
        task_id=result.id,
        status="QUEUED",
        message="Full database verification started. Poll /verify-all/{task_id} for the result.",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/audit-events/verify-all/{task_id}  (spec §11.2)
# ---------------------------------------------------------------------------

@router.get(
    "/verify-all/{task_id}",
    response_model=FullChainVerifyStatusResponse,
    summary="Check status of a full-database verification run",
)
def get_verify_all_status(
    task_id: str,
    claims:  dict = Depends(require_scope("audit:verify")),
) -> FullChainVerifyStatusResponse:
    from fdq_commons.tasks.celery_app import celery_app

    task = celery_app.AsyncResult(task_id)

    if task.state == "PENDING":
        return FullChainVerifyStatusResponse(task_id=task_id, status="PENDING")

    if task.state == "FAILURE":
        return FullChainVerifyStatusResponse(task_id=task_id, status="FAILURE")

    if task.state == "SUCCESS":
        result = task.result or {}
        return FullChainVerifyStatusResponse(
            task_id=task_id,
            status="SUCCESS",
            aggregates_checked=result.get("aggregates_checked"),
            broken_count=result.get("broken_count"),
            broken_aggregates=result.get("broken_aggregates"),
        )

    return FullChainVerifyStatusResponse(task_id=task_id, status=task.state)


# ---------------------------------------------------------------------------
# POST /api/v1/audit-events/verify  (spec §5.3.5)
# Single-aggregate, synchronous, on-demand verification
# ---------------------------------------------------------------------------

@router.post(
    "/verify",
    response_model=ChainVerifyResponse,
    summary="Verify hash chain integrity",
    description=(
        "Re-computes and validates the hash chain for a sequence range. "
        "Used for regulatory proof-of-integrity and post-incident forensics. "
        "Run no more than once per hour per aggregate — computationally expensive."
    ),
)
def verify_chain(
    body:   ChainVerifyRequest,
    claims: dict = Depends(require_scope("audit:verify")),
) -> ChainVerifyResponse:
    svc = AuditTrailService()
    return svc.verify_chain(body)


# ---------------------------------------------------------------------------
# GET /api/v1/audit-events/entity/{aggregate_type}/{aggregate_id}  (spec §5.3.4)
# Defined BEFORE /{event_id} for route specificity
# ---------------------------------------------------------------------------

@router.get(
    "/entity/{aggregate_type}/{aggregate_id}",
    response_model=PaginatedResponse[AuditEventRead],
    summary="Full entity history",
    description=(
        "Returns the complete chronologically-ordered history of all events "
        "for a specific entity. Primary endpoint for the Data Lineage Viewer "
        "(FR-RPT-07) and DGS case review (FR-VER-02)."
    ),
)
def get_entity_history(
    aggregate_type: str,
    aggregate_id:   str,
    from_sequence:  Optional[int] = Query(None, ge=1),
    to_sequence:    Optional[int] = Query(None, ge=1),
    event_type:     Optional[str] = Query(None),
    page:           int           = Query(1, ge=1),
    page_size:      int           = Query(
                                        settings.pagination_default_page_size,
                                        ge=1,
                                        le=settings.pagination_max_page_size_audit,
                                    ),
    claims: dict = Depends(require_scope("audit:read")),
) -> PaginatedResponse[AuditEventRead]:
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
    data = [_to_read_model(r, claims) for r in records]
    params = PaginationParams(page=page, page_size=page_size,
                              max_page_size=settings.pagination_max_page_size_audit)
    return PaginatedResponse.build(data=data, params=params, total=total)


# ---------------------------------------------------------------------------
# GET /api/v1/audit-events/{event_id}  (spec §5.3.3)
# ---------------------------------------------------------------------------

@router.get(
    "/{event_id}",
    response_model=AuditEventRead,
    summary="Retrieve a single audit event",
    description=(
        "Returns the full audit event including hashes. PII fields in "
        "payload are masked for non-admin roles per spec §11.1."
    ),
    responses={
        200: {"description": "Full audit event object."},
        404: {"description": "Event not found."},
    },
)
def get_audit_event(
    event_id: uuid.UUID,
    claims:   dict = Depends(require_scope("audit:read")),
) -> AuditEventRead:
    svc    = AuditTrailService()
    record = svc.get_by_id(event_id)
    return _to_read_model(record, claims)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

# Roles permitted to see unmasked PII in audit payloads (spec §5.3.3, §11.1)
_UNMASKED_ROLES = {"system_admin", "compliance_officer", "dgs"}


def _to_read_model(record, claims: dict) -> AuditEventRead:
    """
    Apply field-level PII masking to payload for non-admin roles.
    Spec §5.3.3: "Never expose raw stack traces or internal system data
    in payload fields visible to external callers — apply field-level
    masking for non-admin roles."
    """
    data = record.to_dict()
    role = (claims.get("role") or "").lower()

    if role not in _UNMASKED_ROLES and isinstance(data.get("payload"), dict):
        masked_payload = {}
        for key, value in data["payload"].items():
            if isinstance(value, dict):
                masked_payload[key] = apply_pii_mask(value)
            else:
                masked_payload[key] = value
        data["payload"] = masked_payload

    return AuditEventRead(**data)