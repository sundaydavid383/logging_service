"""
services/error_logging/models.py
----------------------------------
psycopg2 row model for the error_logs table.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fdq_commons.db.base_model import BaseRecord


@dataclass
class ErrorLogRecord(BaseRecord):
    correlation_id:    uuid.UUID | None = None
    service_name:      str              = ""
    error_code:        str              = ""
    error_message:     str              = ""
    stack_trace:       str | None       = None
    severity:          str              = "ERROR"
    request_context:   dict | None      = None
    actor_user_id:     uuid.UUID | None = None
    resolution_status: str              = "OPEN"
    resolved_by:       uuid.UUID | None = None
    resolved_at:       datetime | None  = None
    resolution_notes:  str | None       = None
    recurrence_count:  int              = 1
    first_occurrence:  datetime         = field(
                                            default_factory=lambda: datetime.now(timezone.utc)
                                        )
    last_occurrence:   datetime         = field(
                                            default_factory=lambda: datetime.now(timezone.utc)
                                        )
    metadata:          dict | None      = None