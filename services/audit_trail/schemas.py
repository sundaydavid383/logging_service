"""
services/audit_trail/schemas.py
----------------------------------
Pydantic schemas for the Audit Trail / Event Sourcing Service.
All fields match spec §5.2 (database schema) and §5.3.1 (API request body).

Critical constraint (spec §5.1):
  No endpoint in this service may support PUT, PATCH, or DELETE on event
  records. Audit events are write-once. This is regulatory, not a preference.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from fdq_commons.utils.ip_validator import IPvAnyAddressStr


# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------

class DeviceInfo(BaseModel):
    user_agent:  str | None = None
    device_type: str | None = None
    model_config = {"extra": "ignore"}


class EventPayload(BaseModel):
    """
    Suggested shape for state-change events: { before: {}, after: {}, diff: {} }
    (spec §5.3.1). All three are optional — not every audit event is a
    before/after state change (e.g. USER_RECORD_EXPORT, RULE_ACTIVATED).

    extra="allow" means any additional caller-supplied keys (exported_fields,
    reason, etc.) are preserved as-is instead of being silently dropped.
    The hash chain covers the payload exactly as received either way.
    """
    before: dict[str, Any] | None = None
    after:  dict[str, Any] | None = None
    diff:   dict[str, Any] | None = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# POST /api/v1/audit-events — request body  (spec §5.3.1)
# ---------------------------------------------------------------------------

class AuditEventCreate(BaseModel):
    idempotency_key:   UUID                = Field(...,
                                                description="Required — caller-provided, prevents duplicates within 24h")
    event_type:        str                 = Field(..., max_length=150,
                                                description="Use FDQ event type registry")
    aggregate_type:    str                 = Field(..., max_length=100,
                                                description="CUSTOMER_RECORD | SCAN_JOB | RULE | USER | ETL_JOB")
    aggregate_id:      str                 = Field(..., max_length=255)
    actor_user_id:     UUID | None         = Field(None)
    actor_role:        str | None          = Field(None, max_length=80)
    actor_ip_address:  IPvAnyAddressStr    = Field(...)
    actor_device_info: DeviceInfo | None   = Field(None)
    payload:           EventPayload        = Field(...)
    schema_version:    int                 = Field(1, ge=1)
    occurred_at:       datetime            = Field(...,
                                                description="The business timestamp — required")
    metadata:          dict[str, Any] | None = Field(None)

    @field_validator("event_type")
    @classmethod
    def _upper_event_type(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("aggregate_type")
    @classmethod
    def _validate_aggregate_type(cls, v: str) -> str:
        allowed = {"CUSTOMER_RECORD", "SCAN_JOB", "RULE", "USER", "ETL_JOB",
                   "NOTIFICATION_TEMPLATE"}
        v = v.strip().upper()
        if v not in allowed:
            raise ValueError(f"aggregate_type must be one of {allowed}")
        return v


# ---------------------------------------------------------------------------
# POST response  (spec §5.3.1)
# ---------------------------------------------------------------------------

class AuditEventCreateResponse(BaseModel):
    event_id:        UUID
    sequence_number: int
    event_hash:      str
    recorded_at:     datetime


# ---------------------------------------------------------------------------
# GET response object  (spec §5.3.2, §5.3.3)
# ---------------------------------------------------------------------------

class AuditEventRead(BaseModel):
    id:                  UUID
    sequence_number:     int
    idempotency_key:     UUID
    event_type:          str
    aggregate_type:      str
    aggregate_id:        str
    actor_user_id:       UUID | None
    actor_role:          str | None
    actor_ip_address:    str
    actor_device_info:   dict | None
    payload:             dict
    schema_version:      int
    previous_event_hash: str | None
    event_hash:          str
    occurred_at:         datetime
    recorded_at:         datetime
    metadata:            dict | None
    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# POST /api/v1/audit-events/verify — request body  (spec §5.3.5)
# ---------------------------------------------------------------------------

class ChainVerifyRequest(BaseModel):
    aggregate_type: str = Field(...)
    aggregate_id:   str = Field(...)
    from_sequence:  int = Field(..., ge=1)
    to_sequence:    int = Field(..., ge=1)

    @field_validator("aggregate_type")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class ChainVerifyResponse(BaseModel):
    valid:                bool
    events_verified:      int
    first_sequence:       int | None
    last_sequence:        int | None
    broken_at_sequence:   int | None
    verification_timestamp: datetime


# ---------------------------------------------------------------------------
# POST /api/v1/audit-events/verify-all — full database verification (spec §11.2)
# Async — triggers the scheduled weekly job on demand, returns a task_id
# ---------------------------------------------------------------------------

class FullChainVerifyResponse(BaseModel):
    task_id:    str
    status:     str
    message:    str


class FullChainVerifyStatusResponse(BaseModel):
    task_id:            str
    status:              str   # PENDING | STARTED | SUCCESS | FAILURE
    aggregates_checked:  int | None = None
    broken_count:        int | None = None
    broken_aggregates:   list[dict] | None = None