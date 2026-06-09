"""
services/activity_logging/models.py
-------------------------------------
psycopg2 row model for the activity_logs table.
Inherits BaseRecord for from_row() helper.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fdq_commons.db.base_model import BaseRecord


@dataclass
class ActivityLogRecord(BaseRecord):
    # -------------------------------------------------------------------------
    # Schema Fields matching Spec §3.2
    # All fields must provide a default value to prevent Python inheritance
    # TypeError exceptions regardless of BaseRecord's layout.
    # -------------------------------------------------------------------------
    id: uuid.UUID | None = None
    correlation_id:   uuid.UUID | None = None
    session_id:       uuid.UUID | None = None
    service_name:     str              = ""
    event_type:       str              = ""
    actor_user_id:    uuid.UUID | None = None
    actor_role:       str | None       = None
    actor_ip_address: str              = ""
    actor_device_info: dict | None     = None  # Parsed natively from JSONB by psycopg2
    target_entity_type: str | None     = None
    target_entity_id:   str | None     = None
    action:             str              = ""
    status:             str              = "SUCCESS"
    failure_reason:     str | None       = None
    metadata:           dict | None      = None  # Parsed natively from JSONB by psycopg2
    environment:        str              = "production"
    created_at:         datetime | None  = None

    def __post_init__(self) -> None:
        """
        Executes immediately after initialization. Ensures that newly instantiated 
        records get fresh IDs/Timestamps, while keeping database reads fully intact.
        """
        if self.id is None:
            self.id = uuid.uuid4()
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    def to_db_params(self) -> dict[str, Any]:
        """
        Transforms the dataclass instance fields into a primitive dictionary
        perfectly safe for raw psycopg2 cursor.execute() parameterized mappings.
        Normalizes falsy dictionaries to true database NULLs.
        """
        return {
            "id": self.id,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "service_name": self.service_name,
            "event_type": self.event_type,
            "actor_user_id": self.actor_user_id,
            "actor_role": self.actor_role,
            "actor_ip_address": self.actor_ip_address,
            # If the dict is empty or None, treat it as true database NULL
            "actor_device_info": json.dumps(self.actor_device_info) if self.actor_device_info else None,
            "target_entity_type": self.target_entity_type,
            "target_entity_id": self.target_entity_id,
            "action": self.action,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
            "environment": self.environment,
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        """
        Standard serializable format for matching FastAPI response schema 
        instantiation blocks cleanly.
        """
        return {
            "log_id": self.id,  # Maps directly to the 'log_id' expected by ActivityLogRead/Response
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "service_name": self.service_name,
            "event_type": self.event_type,
            "actor_user_id": self.actor_user_id,
            "actor_role": self.actor_role,
            "actor_ip_address": self.actor_ip_address,
            "actor_device_info": self.actor_device_info,
            "target_entity_type": self.target_entity_type,
            "target_entity_id": self.target_entity_id,
            "action": self.action,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "metadata": self.metadata,
            "environment": self.environment,
            "created_at": self.created_at,
        }