"""
services/error_logging/schemas.py
-----------------------------------
Pydantic schemas for the Error Logging Service.
All field names match the spec §4.2 database schema exactly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from fdq_commons.config import settings
from fdq_commons.utils.sanitiser import SanitisedFreeText


# ---------------------------------------------------------------------------
# POST /api/v1/error-logs — request body  (spec §4.3.1)
# ---------------------------------------------------------------------------

class RequestContext(BaseModel):
    endpoint:   str | None = None
    method:     str | None = None
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    model_config = {"extra": "ignore"}


class ErrorLogCreate(BaseModel):
    correlation_id:  UUID | None        = Field(None)
    service_name:    str                = Field(..., max_length=100)
    error_code:      str                = Field(..., max_length=100,
                                            description="e.g. ETL_PROPAGATION_FAILED")
    error_message:   str                = Field(...,
                                            description="Human-readable error message")
    stack_trace:     SanitisedFreeText  = Field(None,
                                            description="Full traceback — no truncation")
    severity:        str                = Field(...,
                                            description="DEBUG|INFO|WARNING|ERROR|CRITICAL")
    request_context: RequestContext | None = Field(None)
    actor_user_id:   UUID | None        = Field(None)
    metadata:        dict[str, Any] | None = Field(None)

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"severity must be one of {allowed}")
        return v

    @field_validator("error_code")
    @classmethod
    def _upper_error_code(cls, v: str) -> str:
        return v.strip().upper()


# ---------------------------------------------------------------------------
# POST response  (spec §4.3.1)
# ---------------------------------------------------------------------------

class ErrorLogCreateResponse(BaseModel):
    error_log_id:     UUID
    deduplicated:     bool
    recurrence_count: int


# ---------------------------------------------------------------------------
# GET response object  (spec §4.3.2)
# ---------------------------------------------------------------------------

class ErrorLogRead(BaseModel):
    id:                UUID
    correlation_id:    UUID | None
    service_name:      str
    error_code:        str
    error_message:     str
    stack_trace:       str | None
    severity:          str
    request_context:   dict | None
    actor_user_id:     UUID | None
    resolution_status: str
    resolved_by:       UUID | None
    resolved_at:       datetime | None
    resolution_notes:  str | None
    recurrence_count:  int
    first_occurrence:  datetime
    last_occurrence:   datetime
    metadata:          dict | None
    created_at:        datetime
    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# PATCH /api/v1/error-logs/{error_id}/status  (spec §4.3.3)
# ---------------------------------------------------------------------------

class ErrorLogStatusUpdate(BaseModel):
    resolution_status: str = Field(...,
                                description="ACKNOWLEDGED|RESOLVED|SUPPRESSED")
    resolution_notes:  SanitisedFreeText = Field(None)

    @field_validator("resolution_status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        allowed = {"ACKNOWLEDGED", "RESOLVED", "SUPPRESSED"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"resolution_status must be one of {allowed}")
        return v


class ErrorLogStatusResponse(BaseModel):
    error_log_id:      UUID
    resolution_status: str
    resolved_at:       datetime


# ---------------------------------------------------------------------------
# GET /api/v1/error-logs/stats  (spec §4.3.4)
# ---------------------------------------------------------------------------

class ErrorLogStatsItem(BaseModel):
    group_key:      str
    total_errors:   int
    open_count:     int
    critical_count: int
    resolved_count: int
    avg_recurrence: float