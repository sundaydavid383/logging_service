"""
services/audit_trail/models.py
---------------------------------
psycopg2 row model for the audit_events table.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from fdq_commons.db.base_model import BaseRecord


@dataclass
class AuditEventRecord(BaseRecord):
    sequence_number:     int              = 0
    idempotency_key:     uuid.UUID | None = None
    event_type:          str              = ""
    aggregate_type:      str              = ""
    aggregate_id:        str              = ""
    actor_user_id:       uuid.UUID | None = None
    actor_role:          str | None       = None
    actor_ip_address:    str              = ""
    actor_device_info:   dict | None      = None
    payload:             dict | None      = None
    schema_version:      int              = 1
    previous_event_hash: str | None       = None
    event_hash:          str              = ""
    occurred_at:         datetime | None  = None
    recorded_at:         datetime | None  = None
    metadata:            dict | None      = None