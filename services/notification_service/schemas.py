"""
services/notification_service/schemas.py
------------------------------------------
Pydantic schemas for the Notification Service.
All fields match spec §6.2 (database schema) and §6.3 (API endpoints).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class NotificationDispatch(BaseModel):
    notification_id:         UUID                   = Field(...)
    channel:                 str                    = Field(...)
    recipient:               str                    = Field(..., max_length=500)
    template_id:             str | None             = Field(None, max_length=100)
    template_data:           dict[str, Any] | None  = Field(None)
    subject:                 str | None             = Field(None, max_length=500)
    priority:                str                    = Field("NORMAL")
    triggered_by_event_id:   UUID | None            = Field(None)
    triggered_by_user_id:    UUID | None            = Field(None)
    suppress_within_seconds: int                    = Field(0, ge=0)
    metadata:                dict[str, Any] | None  = Field(None)

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, v: str) -> str:
        allowed = {"EMAIL", "TEAMS", "SMS", "DASHBOARD"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"channel must be one of {allowed}")
        return v

    @field_validator("priority")
    @classmethod
    def _validate_priority(cls, v: str) -> str:
        allowed = {"HIGH", "NORMAL", "LOW"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"priority must be one of {allowed}")
        return v


class NotificationDispatchResponse(BaseModel):
    notification_id: UUID
    status:          str
    queued_at:       datetime


class DirectEmailRequest(BaseModel):
    notification_id: UUID
    to:              list[str]
    cc:              list[str]   = Field(default_factory=list)
    subject:         str         = Field(..., max_length=500)
    html_body:       str
    text_body:       str | None  = None


class DirectTeamsRequest(BaseModel):
    notification_id: UUID
    channel_key:     str
    title:           str
    summary:         str
    facts:           list[dict[str, str]] = Field(default_factory=list)
    severity:        str                  = Field("INFO")

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: str) -> str:
        allowed = {"CRITICAL", "WARNING", "INFO"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"severity must be one of {allowed}")
        return v


class NotificationStatusResponse(BaseModel):
    notification_id:     UUID
    channel:             str
    status:              str
    delivery_attempts:   int
    delivered_at:        datetime | None
    provider_message_id: str | None


class NotificationLogRead(BaseModel):
    id:                    UUID
    notification_id:       UUID
    channel:               str
    recipient:             str
    template_id:           str | None
    subject:               str | None
    body_preview:          str | None
    status:                str
    provider_message_id:   str | None
    delivery_attempts:     int
    last_attempt_at:       datetime | None
    delivered_at:          datetime | None
    error_message:         str | None
    triggered_by_event_id: UUID | None
    triggered_by_user_id:  UUID | None
    metadata:              dict | None
    created_at:            datetime
    model_config = {"from_attributes": True}


class SuppressionWindow(BaseModel):
    day:   str
    start: str
    end:   str


class NotificationPreferences(BaseModel):
    email_enabled:           bool                    = True
    teams_enabled:           bool                    = False
    digest_mode:             bool                    = False
    digest_interval_minutes: int                     = Field(60, ge=1)
    suppression_windows:     list[SuppressionWindow] = Field(default_factory=list)
    alert_types:             list[str]               = Field(default_factory=list)


class NotificationTemplateRead(BaseModel):
    template_id:      str
    channel:          str
    subject_template: str | None
    body_template:    str
    version:          int
    is_active:        bool
    updated_at:       datetime


class NotificationTemplateUpdate(BaseModel):
    subject_template: str | None        = None
    body_template:    str
    dry_run:          bool              = False
    sample_data:      dict[str, Any] | None = None