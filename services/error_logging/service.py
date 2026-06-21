"""
services/error_logging/service.py
-----------------------------------
Service layer for error_logs.

Key spec requirements implemented here:
  - Deduplication (spec §4.2): before inserting, check if identical
    error_code + service_name exists within ERROR_DEDUP_WINDOW_SECONDS.
    If yes, increment recurrence_count instead of inserting a new row.
  - Hash key: (error_code + service_name + first line of stack_trace)
  - Resolution is non-destructive — original error data is never changed
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
import hashlib

import structlog
from psycopg2.extras import DictCursor

from fdq_commons.config import settings
from fdq_commons.db.session import db_connection
from fdq_commons.models.errors import ErrorCode, FDQException

from .models import ErrorLogRecord
from .schemas import (
    ErrorLogCreate,
    ErrorLogCreateResponse,
    ErrorLogRead,
    ErrorLogStatsItem,
    ErrorLogStatusUpdate,
    ErrorLogStatusResponse,
)

log = structlog.get_logger()


class ErrorLoggingService:

    # ------------------------------------------------------------------
    # CREATE with deduplication  (spec §4.3.1 + §4.2)
    # ------------------------------------------------------------------

    def create(self, body: ErrorLogCreate) -> ErrorLogCreateResponse:
        """
        Insert an error log entry with deduplication.

        Deduplication check (spec §4.2):
        Hash (error_code + service_name + first line of stack trace).
        If an OPEN entry with the same hash exists within the dedup window,
        increment recurrence_count and update last_occurrence.
        Do NOT insert a new row and do NOT trigger duplicate alerts.
        """
        dedup_key = self._build_dedup_key(
            body.error_code,
            body.service_name,
            body.stack_trace,
        )

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:

                # Check for existing OPEN entry within the dedup window
                cur.execute("""
                    SELECT id, recurrence_count
                    FROM error_logs
                    WHERE error_code        = %(error_code)s
                      AND service_name      = %(service_name)s
                      AND resolution_status = 'OPEN'
                      AND last_occurrence   >= NOW() - (%(window)s || ' seconds')::INTERVAL
                    ORDER BY last_occurrence DESC
                    LIMIT 1
                """, {
                    "error_code":   body.error_code,
                    "service_name": body.service_name,
                    "window":       settings.error_dedup_window_seconds,
                })
                existing = cur.fetchone()

                if existing:
                    # Deduplicated — increment recurrence count
                    new_count = existing["recurrence_count"] + 1
                    cur.execute("""
                        UPDATE error_logs
                        SET recurrence_count = %(count)s,
                            last_occurrence  = NOW()
                        WHERE id = %(id)s
                        RETURNING id, recurrence_count
                    """, {"count": new_count, "id": existing["id"]})
                    row = cur.fetchone()

                    log.info(
                        "error_log_deduplicated",
                        error_log_id=str(row["id"]),
                        error_code=body.error_code,
                        recurrence_count=row["recurrence_count"],
                    )
                    return ErrorLogCreateResponse(
                        error_log_id=row["id"],
                        deduplicated=True,
                        recurrence_count=row["recurrence_count"],
                    )

                # Not deduplicated — insert new row
                request_ctx = (
                    body.request_context.model_dump(exclude_none=True)
                    if body.request_context else None
                )

                cur.execute("""
                    INSERT INTO error_logs (
                        correlation_id, service_name, error_code, error_message,
                        stack_trace, severity, request_context, actor_user_id,
                        resolution_status, recurrence_count, first_occurrence,
                        last_occurrence, metadata
                    ) VALUES (
                        %(correlation_id)s, %(service_name)s, %(error_code)s,
                        %(error_message)s, %(stack_trace)s, %(severity)s,
                        %(request_context)s, %(actor_user_id)s,
                        'OPEN', 1, NOW(), NOW(), %(metadata)s
                    )
                    RETURNING id, recurrence_count
                """, {
                    "correlation_id":  body.correlation_id,
                    "service_name":    body.service_name,
                    "error_code":      body.error_code,
                    "error_message":   body.error_message,
                    "stack_trace":     body.stack_trace,
                    "severity":        body.severity,
                    "request_context": json.dumps(request_ctx) if request_ctx else None,
                    "actor_user_id":   body.actor_user_id,
                    "metadata":        json.dumps(body.metadata) if body.metadata else None,
                })
                row = cur.fetchone()

        log.info(
            "error_log_created",
            error_log_id=str(row["id"]),
            error_code=body.error_code,
            severity=body.severity,
            service_name=body.service_name,
        )
        return ErrorLogCreateResponse(
            error_log_id=row["id"],
            deduplicated=False,
            recurrence_count=1,
        )

    # ------------------------------------------------------------------
    # LIST  (spec §4.3.2)
    # ------------------------------------------------------------------

    def list(
        self,
        service_name:      str | None,
        severity:          list[str] | None,
        resolution_status: str | None,
        start_date:        datetime | None,
        end_date:          datetime | None,
        page:              int,
        page_size:         int,
    ) -> tuple[list[ErrorLogRecord], int]:

        filters: list[str] = []
        values:  dict[str, Any] = {}

        if service_name:
            filters.append("service_name = %(service_name)s")
            values["service_name"] = service_name
        if severity:
            filters.append("severity = ANY(%(severity)s)")
            values["severity"] = [s.upper() for s in severity]
        if resolution_status:
            filters.append("resolution_status = %(resolution_status)s")
            values["resolution_status"] = resolution_status.upper()
        if start_date:
            filters.append("created_at >= %(start_date)s")
            values["start_date"] = start_date
        if end_date:
            filters.append("created_at <= %(end_date)s")
            values["end_date"] = end_date

        where  = ("WHERE " + " AND ".join(filters)) if filters else ""
        limit  = min(page_size, settings.pagination_max_page_size_audit)
        offset = (page - 1) * limit
        values["limit"]  = limit
        values["offset"] = offset

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(f"SELECT COUNT(*) AS total FROM error_logs {where}", values)
                total = cur.fetchone()["total"]

                cur.execute(f"""
                    SELECT * FROM error_logs
                    {where}
                    ORDER BY created_at DESC
                    LIMIT %(limit)s OFFSET %(offset)s
                """, values)
                rows = cur.fetchall()

        records = ErrorLogRecord.from_rows([dict(r) for r in rows])
        return records, total

    # ------------------------------------------------------------------
    # PATCH status  (spec §4.3.3)
    # ------------------------------------------------------------------

    def update_status(
        self,
        error_id: uuid.UUID,
        body:     ErrorLogStatusUpdate,
        resolver_id: uuid.UUID | None,
    ) -> ErrorLogStatusResponse:
        """
        Non-destructive status update — original error data is preserved.
        Only resolution_status, resolved_by, resolved_at, resolution_notes change.
        """
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    UPDATE error_logs
                    SET resolution_status = %(status)s,
                        resolved_by       = %(resolved_by)s,
                        resolved_at       = CASE
                                                WHEN %(status)s = 'RESOLVED'
                                                THEN NOW()
                                                ELSE resolved_at
                                            END,
                        resolution_notes  = %(notes)s
                    WHERE id = %(id)s
                    RETURNING id, resolution_status, resolved_at
                """, {
                    "status":      body.resolution_status,
                    "resolved_by": resolver_id,
                    "notes":       body.resolution_notes,
                    "id":          error_id,
                })
                row = cur.fetchone()

        if not row:
            raise FDQException(
                status_code=404,
                code=ErrorCode.NOT_FOUND,
                message=f"Error log '{error_id}' not found.",
            )

        log.info(
            "error_log_status_updated",
            error_log_id=str(row["id"]),
            resolution_status=row["resolution_status"],
        )
        return ErrorLogStatusResponse(
            error_log_id=row["id"],
            resolution_status=row["resolution_status"],
            resolved_at=row["resolved_at"] or datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # STATS  (spec §4.3.4)
    # ------------------------------------------------------------------

    def stats(
        self,
        start_date: datetime | None,
        end_date:   datetime | None,
        group_by:   str,
    ) -> list[ErrorLogStatsItem]:

        allowed = {"service_name", "severity", "error_code"}
        if group_by not in allowed:
            raise FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"group_by must be one of: {', '.join(sorted(allowed))}",
            )

        filters = []
        values: dict[str, Any] = {}

        if start_date:
            filters.append("created_at >= %(start_date)s")
            values["start_date"] = start_date
        if end_date:
            filters.append("created_at <= %(end_date)s")
            values["end_date"] = end_date

        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        # group_by is validated against the allowed set above — safe to interpolate
        sql = f"""
            SELECT
                {group_by}                                              AS group_key,
                COUNT(*)                                                AS total_errors,
                COUNT(*) FILTER (WHERE resolution_status = 'OPEN')     AS open_count,
                COUNT(*) FILTER (WHERE severity = 'CRITICAL')          AS critical_count,
                COUNT(*) FILTER (WHERE resolution_status = 'RESOLVED') AS resolved_count,
                AVG(recurrence_count)                                   AS avg_recurrence
            FROM error_logs
            {where}
            GROUP BY {group_by}
            ORDER BY total_errors DESC
        """

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(sql, values)
                rows = cur.fetchall()

        return [
            ErrorLogStatsItem(
                group_key=r["group_key"],
                total_errors=r["total_errors"],
                open_count=r["open_count"],
                critical_count=r["critical_count"],
                resolved_count=r["resolved_count"],
                avg_recurrence=float(r["avg_recurrence"] or 0),
            )
            for r in rows
        ]
    
    # ------------------------------------------------------------------
    # GET BY ID (Fixes the AttributeError)
    # ------------------------------------------------------------------
    def get_by_id(self, error_id: uuid.UUID) -> ErrorLogRecord:
        """
        Fetch a single error log entry by its UUID.
        Raises 404 FDQException if the record is missing.
        """
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT * FROM error_logs 
                    WHERE id = %(id)s
                """, {"id": error_id})
                row = cur.fetchone()

        if not row:
            raise FDQException(
                status_code=404,
                code=ErrorCode.NOT_FOUND,
                message=f"Error log record '{error_id}' not found.",
            )

        # Parse the raw row into your domain model record layer
        return ErrorLogRecord.from_rows([dict(row)])[0]
    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_dedup_key(
        error_code:  str,
        service_name: str,
        stack_trace:  str | None,
    ) -> str:
        """
        Build deduplication hash from error_code + service_name +
        first line of stack trace (spec §4.2).
        """
        first_line = ""
        if stack_trace:
            first_line = stack_trace.strip().splitlines()[0]
        raw = f"{error_code}:{service_name}:{first_line}"
        return hashlib.sha256(raw.encode()).hexdigest()