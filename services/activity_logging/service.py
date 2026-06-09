"""
services/activity_logging/service.py
--------------------------------------
Service layer for activity_logs.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import psycopg2.extras
from psycopg2.extras import DictCursor
import structlog

from fdq_commons.config import settings
from fdq_commons.db.session import db_connection
from fdq_commons.models.errors import ErrorCode, FDQException

from .models import ActivityLogRecord
from .schemas import (
    ActivityLogCreate,
    ActivityLogQueryParams,
    ActivityLogResponse,
    ActivityLogSummaryItem,
)

log = structlog.get_logger()

# Register UUID adapter once at module import
psycopg2.extras.register_uuid()


class ActivityLoggingService:

    # ------------------------------------------------------------------
    # CREATE & TASK INTEGRATION (spec §3.3.1)
    # ------------------------------------------------------------------

    def create(self, body: ActivityLogCreate) -> ActivityLogResponse:
        """
        Insert one activity log row from a validated Pydantic model structure.
        """
        record_data = body.model_dump()
        return self.create_from_raw_dict(record_data, is_async_worker=False)

    def create_from_raw_dict(self, record_data: dict[str, Any], is_async_worker: bool = True) -> ActivityLogResponse:
        """
        Core database insertion pipeline. Handles both direct execution 
        and asynchronous worker task payloads.
        """
        event_type = record_data.get("event_type", "").upper()
        
        # Pass context flag down so validation errors don't cause infinite task retries
        self._validate_event_type(event_type, raise_as_fatal=is_async_worker)

        log_id = record_data.get("id") or record_data.get("log_id") or uuid.uuid4()
        
        device_info = record_data.get("actor_device_info")
        if hasattr(device_info, "model_dump"):
            device_info = device_info.model_dump(exclude_none=True)

        sql = """
            INSERT INTO activity_logs (
                id, correlation_id, session_id, service_name, event_type,
                actor_user_id, actor_role, actor_ip_address, actor_device_info,
                target_entity_type, target_entity_id, action, status,
                failure_reason, metadata, environment
            ) VALUES (
                %(id)s, %(correlation_id)s, %(session_id)s, %(service_name)s, %(event_type)s,
                %(actor_user_id)s, %(actor_role)s, %(actor_ip_address)s, %(actor_device_info)s,
                %(target_entity_type)s, %(target_entity_id)s, %(action)s, %(status)s,
                %(failure_reason)s, %(metadata)s, %(environment)s
            )
            RETURNING id, created_at
        """

        params = {
            "id":                 log_id,
            "correlation_id":     record_data.get("correlation_id"),
            "session_id":         record_data.get("session_id"),
            "service_name":       record_data.get("service_name"),
            "event_type":         event_type,
            "actor_user_id":      record_data.get("actor_user_id"),
            "actor_role":         record_data.get("actor_role"),
            "actor_ip_address":   record_data.get("actor_ip_address"),
            "actor_device_info":  json.dumps(device_info) if device_info else json.dumps({}),
            "target_entity_type": record_data.get("target_entity_type"),
            "target_entity_id":   record_data.get("target_entity_id"),
            "action":             record_data.get("action"),
            "status":             record_data.get("status", "").upper(),
            "failure_reason":     record_data.get("failure_reason"),
            "metadata":           json.dumps(record_data.get("metadata")) if record_data.get("metadata") else json.dumps({}),
            "environment":        settings.environment,
        }

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()

        log.info(
            "activity_log_created",
            log_id=str(row["id"]),
            event_type=event_type,
            service_name=params["service_name"],
            actor_user_id=str(params["actor_user_id"]) if params["actor_user_id"] else None,
        )

        return ActivityLogResponse(log_id=row["id"], created_at=row["created_at"])

    # ------------------------------------------------------------------
    # LIST  (spec §3.3.2)
    # ------------------------------------------------------------------

    def list(self, params: ActivityLogQueryParams) -> tuple[list[ActivityLogRecord], int]:
        """
        Query activity logs with filters. Returns (records, total_count).
        """
        filters: list[str] = []
        values:  dict[str, Any] = {}

        if params.actor_user_id:
            filters.append("actor_user_id = %(actor_user_id)s")
            values["actor_user_id"] = params.actor_user_id
        if params.event_type:
            filters.append("event_type = %(event_type)s")
            values["event_type"] = params.event_type.upper()
        if params.target_entity_id:
            filters.append("target_entity_id = %(target_entity_id)s")
            values["target_entity_id"] = params.target_entity_id
        if params.status:
            filters.append("status = %(status)s")
            values["status"] = params.status.upper()
        if params.service_name:
            filters.append("service_name = %(service_name)s")
            values["service_name"] = params.service_name
        if params.start_date:
            filters.append("created_at >= %(start_date)s")
            values["start_date"] = params.start_date
        if params.end_date:
            filters.append("created_at <= %(end_date)s")
            values["end_date"] = params.end_date
        if params.after_id:
            filters.append("""
                (created_at, id) < (
                    SELECT created_at, id FROM activity_logs WHERE id = %(after_id)s
                )
            """)
            values["after_id"] = params.after_id

        # Fixed: Explicitly inject pagination limits directly into initial allocation map
        limit  = max(1, params.page_size)
        offset = max(0, (params.page - 1) * limit) if not params.after_id else 0
        
        values["limit"]  = limit
        values["offset"] = offset

        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        count_sql = f"SELECT COUNT(*) AS total FROM activity_logs {where}"
        data_sql  = f"""
            SELECT * FROM activity_logs
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(count_sql, values)
                total = cur.fetchone()["total"]

                cur.execute(data_sql, values)
                rows = cur.fetchall()

        records = ActivityLogRecord.from_rows([dict(r) for r in rows])
        return records, total

    # ------------------------------------------------------------------
    # GET SINGLE  (spec §3.3.3)
    # ------------------------------------------------------------------

    def get_by_id(self, log_id: uuid.UUID) -> ActivityLogRecord:
        sql = "SELECT * FROM activity_logs WHERE id = %(id)s"
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(sql, {"id": log_id})
                row = cur.fetchone()

        if not row:
            raise FDQException(
                status_code=404,
                code=ErrorCode.NOT_FOUND,
                message=f"Activity log '{log_id}' not found.",
            )
        return ActivityLogRecord.from_row(dict(row))

    # ------------------------------------------------------------------
    # SUMMARY  (spec §3.3.4)
    # ------------------------------------------------------------------

    def summary(
        self,
        start_date: datetime | None,
        end_date:   datetime | None,
        group_by:   str,
    ) -> list[ActivityLogSummaryItem]:
        """
        Return aggregated statistics from activity_logs_summary mat view.
        """
        allowed_groups = {"event_type", "actor_role", "service_name", "status"}
        if group_by not in allowed_groups:
            raise FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"group_by must be one of: {', '.join(sorted(allowed_groups))}",
            )

        filters = ["group_by = %(group_by)s"]
        values: dict[str, Any] = {"group_by": group_by}

        if start_date:
            filters.append("time_bucket >= %(start_date)s")
            values["start_date"] = start_date
        if end_date:
            filters.append("time_bucket <= %(end_date)s")
            values["end_date"] = end_date

        where = "WHERE " + " AND ".join(filters)

        sql = f"""
            SELECT
                group_key,
                group_by,
                SUM(count)         AS count,
                SUM(failure_count) AS failure_count,
                MAX(last_occurrence) AS last_occurrence
            FROM activity_logs_summary
            {where}
            GROUP BY group_key, group_by
            ORDER BY count DESC
        """

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(sql, values)
                rows = cur.fetchall()

        return [
            ActivityLogSummaryItem(
                group_key=r["group_key"],
                group_by=r["group_by"],
                count=r["count"],
                failure_count=r["failure_count"],
                last_occurrence=r["last_occurrence"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_event_type(self, event_type: str, raise_as_fatal: bool = False) -> None:
        """
        Check event_type exists in the active registry.
        """
        sql = """
            SELECT 1 FROM event_type_registry
            WHERE event_type = %(event_type)s AND is_active = TRUE
        """
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(sql, {"event_type": event_type})
                exists = cur.fetchone()

        if not exists:
            message = (
                f"event_type '{event_type}' is not registered. "
                "Add it to the event_type_registry table before use."
            )
            if raise_as_fatal:
                raise ValueError(message)
                
            raise FDQException(
                status_code=422,
                code=ErrorCode.INVALID_ENUM_VALUE,
                message=message,
            )