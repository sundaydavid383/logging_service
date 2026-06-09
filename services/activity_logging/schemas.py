"""
services/activity_logging/schemas.py
--------------------------------------
Pydantic v2 request/response schemas for the Activity Logging Service.

All field constraints come from the spec §3.2 and §3.3.
No hardcoded limits — lengths and enums reference constants from settings
or are taken directly from the spec column definitions.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from fdq_commons.config import settings
from fdq_commons.utils.ip_validator import IPvAnyAddressStr
from fdq_commons.utils.sanitiser import (
    SanitisedFreeText,
    SanitisedStructuredField,
    apply_pii_mask,
)


# ---------------------------------------------------------------------------
# Shared sub-schemas
# ---------------------------------------------------------------------------

class DeviceInfo(BaseModel):
    user_agent:  str | None = None
    device_type: str | None = None
    os:          str | None = None
    model_config = {"extra": "ignore"}


# ---------------------------------------------------------------------------
# POST /api/v1/activity-logs  — request body  (spec §3.3.1)
# ---------------------------------------------------------------------------

class ActivityLogCreate(BaseModel):
    correlation_id:   uuid.UUID | None = Field(None)
    session_id:       uuid.UUID | None = Field(None)
    service_name:     str              = Field(..., max_length=100)
    event_type:       str              = Field(..., max_length=100)
    actor_user_id:    uuid.UUID | None = Field(None)
    actor_role:       str | None       = Field(None, max_length=80)
    actor_ip_address: IPvAnyAddressStr = Field(...)
    actor_device_info: DeviceInfo | None = Field(None)
    target_entity_type: str | None       = Field(None, max_length=100)
    target_entity_id:   str | None       = Field(None, max_length=255)
    action:             str              = Field(..., max_length=150)
    status:             str              = Field(...)
    failure_reason:     SanitisedFreeText = Field(None)
    metadata:           dict[str, Any] | None = Field(None)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        allowed = {"SUCCESS", "FAILURE", "PARTIAL"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, v: str) -> str:
        # Uppercase and strip — event types are always uppercase per registry
        return v.strip().upper()

    @model_validator(mode="after")
    def _require_failure_reason_when_failed(self) -> "ActivityLogCreate":
        if self.status == "FAILURE" and not self.failure_reason:
            raise ValueError("failure_reason is required when status is FAILURE")
        return self


# ---------------------------------------------------------------------------
# POST response  (spec §3.3.1)
# ---------------------------------------------------------------------------

class ActivityLogResponse(BaseModel):
    log_id:     uuid.UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# GET single log response  (spec §3.3.3)
# ---------------------------------------------------------------------------

class ActivityLogRead(BaseModel):
    log_id:             uuid.UUID  # Fixed: Changed from 'id' to match models.py to_dict() mapping
    correlation_id:     uuid.UUID | None
    session_id:         uuid.UUID | None
    service_name:       str
    event_type:         str
    actor_user_id:      uuid.UUID | None
    actor_role:         str | None
    actor_ip_address:   str
    actor_device_info:  dict | None
    target_entity_type: str | None
    target_entity_id:   str | None
    action:             str
    status:             str
    failure_reason:     str | None
    metadata:           dict | None
    environment:        str
    created_at:         datetime

    # Explicitly removed 'from_attributes = True' to keep verification safe 
    # and deterministic when processing direct model dictionaries.

    @field_validator("metadata", mode="before")
    @classmethod
    def _mask_metadata_pii(cls, v: Any) -> Any:
        # Apply PII masking at serialization — spec §11.1
        if isinstance(v, dict):
            return apply_pii_mask(v)
        return v


# ---------------------------------------------------------------------------
# GET /api/v1/activity-logs  — query params  (spec §3.3.2)
# ---------------------------------------------------------------------------

class ActivityLogQueryParams(BaseModel):
    """Parsed and validated query parameters for the list endpoint."""
    actor_user_id:    uuid.UUID | None = None
    event_type:       str | None       = None
    target_entity_id: str | None       = None
    status:           str | None       = None
    start_date:       datetime | None  = None
    end_date:         datetime | None  = None
    service_name:     str | None       = None
    page:             int              = Field(1, ge=1)
    page_size:        int              = Field(
                                            settings.pagination_default_page_size,
                                            ge=1,
                                            le=settings.pagination_max_page_size_logs,
                                        )
    after_id:         uuid.UUID | None = None  # cursor-based pagination

    @model_validator(mode="after")
    def _validate_date_range(self) -> "ActivityLogQueryParams":
        if self.start_date and self.end_date:
            if self.start_date > self.end_date:
                raise ValueError("start_date must be before end_date")
        return self


# ---------------------------------------------------------------------------
# GET /api/v1/activity-logs/summary  — response item  (spec §3.3.4)
# ---------------------------------------------------------------------------

class ActivityLogSummaryItem(BaseModel):
    group_key:       str
    group_by:        str
    count:           int
    failure_count:   int
    last_occurrence: datetime