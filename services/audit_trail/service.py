"""
services/audit_trail/service.py
-----------------------------------
Service layer for audit_events.

This is the most sensitive service in the platform (spec §5.1).
Three rules enforced here, exactly as specified:

  1. Append-only — no UPDATE/DELETE methods exist in this class at all.
     The database trigger (prevent_audit_mutation) is the final guard,
     but this service layer does not even attempt mutation operations.

  2. Hash chaining — server computes the hash, never trusts a caller-
     provided hash. Uses pg_advisory_xact_lock keyed on
     (aggregate_type, aggregate_id) to serialise concurrent appends to
     the same aggregate (spec §5.3.1, step-by-step server-side hash
     computation).

  3. Idempotency — caller-provided idempotency_key is UNIQUE in the DB.
     Duplicate keys within 24h return the original 201 response without
     re-inserting (spec §5.3.1).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from psycopg2.extras import DictCursor
from psycopg2 import errors as pg_errors

from fdq_commons.config import settings
from fdq_commons.db.session import db_connection
from fdq_commons.models.errors import ErrorCode, FDQException

from .models import AuditEventRecord
from .schemas import (
    AuditEventCreate,
    AuditEventCreateResponse,
    AuditEventRead,
    ChainVerifyRequest,
    ChainVerifyResponse,
)

log = structlog.get_logger()


class AuditTrailService:

    # ------------------------------------------------------------------
    # APPEND  (spec §5.3.1) — the only write operation that exists
    # ------------------------------------------------------------------

    def append_event(self, body: AuditEventCreate) -> AuditEventCreateResponse:
        """
        Append an audit event with server-computed hash chaining.

        Steps (exactly as spec §5.3.1 prescribes):
          1. Check idempotency_key uniqueness — return existing row if duplicate
          2. Acquire pg_advisory_xact_lock on (aggregate_type, aggregate_id)
             to prevent concurrent appends from racing on previous_hash
          3. Query latest event_hash for this aggregate -> previous_event_hash
          4. Build canonical JSON: json.dumps(fields, sort_keys=True, ensure_ascii=True)
          5. Compute event_hash = SHA-256(sequence || idempotency_key || aggregate_id
             || occurred_at || canonical_payload)
          6. Insert atomically inside the transaction
        """
        device_info = (
            body.actor_device_info.model_dump(exclude_none=True)
            if body.actor_device_info else None
        )
        payload_dict = body.payload.model_dump()

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:

                # ---- Step 1: idempotency check (spec: 24h dedup window) ----
                cur.execute("""
                    SELECT id, sequence_number, event_hash, recorded_at
                    FROM audit_events
                    WHERE idempotency_key = %(key)s
                """, {"key": str(body.idempotency_key)})
                existing = cur.fetchone()

                if existing:
                    log.info(
                        "audit_event_idempotency_hit",
                        idempotency_key=str(body.idempotency_key),
                        event_id=str(existing["id"]),
                    )
                    return AuditEventCreateResponse(
                        event_id=existing["id"],
                        sequence_number=existing["sequence_number"],
                        event_hash=existing["event_hash"],
                        recorded_at=existing["recorded_at"],
                    )

                # ---- Step 2: advisory lock on this aggregate ----
                # Serialises concurrent appends to the same aggregate so
                # previous_hash cannot be read by two transactions at once.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%(key)s))",
                    {"key": f"{body.aggregate_type}:{body.aggregate_id}"},
                )

                # ---- Step 3: fetch previous hash for this aggregate ----
                cur.execute("""
                    SELECT event_hash
                    FROM audit_events
                    WHERE aggregate_type = %(agg_type)s
                      AND aggregate_id   = %(agg_id)s
                    ORDER BY sequence_number DESC
                    LIMIT 1
                """, {
                    "agg_type": body.aggregate_type,
                    "agg_id":   body.aggregate_id,
                })
                prev_row = cur.fetchone()
                previous_hash = prev_row["event_hash"] if prev_row else None

                # ---- Step 4 & 5: canonical JSON + SHA-256 hash ----
                # Reserve the next sequence number via the sequence itself
                # so the hash can include it deterministically.
                cur.execute("SELECT nextval('audit_events_sequence_number_seq') AS seq")
                sequence_number = cur.fetchone()["seq"]

                occurred_at_iso = body.occurred_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

                canonical = json.dumps({
                    "sequence_number": sequence_number,
                    "idempotency_key": str(body.idempotency_key),
                    "aggregate_id":    body.aggregate_id,
                    "event_type":      body.event_type,
                    "occurred_at":     occurred_at_iso,
                    "payload":         payload_dict,
                    "previous_hash":   previous_hash or "",
                }, sort_keys=True, ensure_ascii=True)

                event_hash = hashlib.new(
                    settings.audit_hash_algorithm,
                    canonical.encode(),
                ).hexdigest()

                # ---- Step 6: atomic insert ----
                try:
                    cur.execute("""
                        INSERT INTO audit_events (
                            sequence_number, idempotency_key, event_type,
                            aggregate_type, aggregate_id, actor_user_id,
                            actor_role, actor_ip_address, actor_device_info,
                            payload, schema_version, previous_event_hash,
                            event_hash, occurred_at, metadata
                        ) VALUES (
                            %(sequence_number)s, %(idempotency_key)s, %(event_type)s,
                            %(aggregate_type)s, %(aggregate_id)s, %(actor_user_id)s,
                            %(actor_role)s, %(actor_ip_address)s, %(actor_device_info)s,
                            %(payload)s, %(schema_version)s, %(previous_event_hash)s,
                            %(event_hash)s, %(occurred_at)s, %(metadata)s
                        )
                        RETURNING id, sequence_number, event_hash, recorded_at
                    """, {
                        "sequence_number":     sequence_number,
                        "idempotency_key":     str(body.idempotency_key),
                        "event_type":          body.event_type,
                        "aggregate_type":      body.aggregate_type,
                        "aggregate_id":        body.aggregate_id,
                        "actor_user_id":       body.actor_user_id,
                        "actor_role":          body.actor_role,
                        "actor_ip_address":    body.actor_ip_address,
                        "actor_device_info":   json.dumps(device_info) if device_info else None,
                        "payload":             json.dumps(payload_dict),
                        "schema_version":      body.schema_version,
                        "previous_event_hash": previous_hash,
                        "event_hash":          event_hash,
                        "occurred_at":         body.occurred_at,
                        "metadata":            json.dumps(body.metadata) if body.metadata else None,
                    })
                    row = cur.fetchone()

                except pg_errors.UniqueViolation:
                    # Race: another request inserted same idempotency_key
                    # between Step 1 and Step 6. Return that row instead.
                    conn.rollback()
                    cur.execute("""
                        SELECT id, sequence_number, event_hash, recorded_at
                        FROM audit_events WHERE idempotency_key = %(key)s
                    """, {"key": str(body.idempotency_key)})
                    row = cur.fetchone()
                    log.info(
                        "audit_event_idempotency_race_resolved",
                        idempotency_key=str(body.idempotency_key),
                    )

        log.info(
            "audit_event_appended",
            event_id=str(row["id"]),
            sequence_number=row["sequence_number"],
            aggregate_type=body.aggregate_type,
            aggregate_id=body.aggregate_id,
            event_type=body.event_type,
            event_hash=row["event_hash"][:8],
        )

        return AuditEventCreateResponse(
            event_id=row["id"],
            sequence_number=row["sequence_number"],
            event_hash=row["event_hash"],
            recorded_at=row["recorded_at"],
        )

    # ------------------------------------------------------------------
    # GET LIST  (spec §5.3.2)
    # ------------------------------------------------------------------

    def list(
        self,
        aggregate_type: str | None,
        aggregate_id:   str | None,
        event_type:     str | None,
        actor_user_id:  uuid.UUID | None,
        start_date:     datetime | None,
        end_date:       datetime | None,
        page:           int,
        page_size:      int,
    ) -> tuple[list[AuditEventRecord], int]:

        filters: list[str] = []
        values:  dict[str, Any] = {}

        if aggregate_type:
            filters.append("aggregate_type = %(aggregate_type)s")
            values["aggregate_type"] = aggregate_type.upper()
        if aggregate_id:
            filters.append("aggregate_id = %(aggregate_id)s")
            values["aggregate_id"] = aggregate_id
        if event_type:
            filters.append("event_type = %(event_type)s")
            values["event_type"] = event_type.upper()
        if actor_user_id:
            filters.append("actor_user_id = %(actor_user_id)s")
            values["actor_user_id"] = actor_user_id
        if start_date:
            filters.append("occurred_at >= %(start_date)s")
            values["start_date"] = start_date
        if end_date:
            filters.append("occurred_at <= %(end_date)s")
            values["end_date"] = end_date

        where  = ("WHERE " + " AND ".join(filters)) if filters else ""
        limit  = min(page_size, settings.pagination_max_page_size_audit)
        offset = (page - 1) * limit
        values["limit"]  = limit
        values["offset"] = offset

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(f"SELECT COUNT(*) AS total FROM audit_events {where}", values)
                total = cur.fetchone()["total"]

                cur.execute(f"""
                    SELECT * FROM audit_events
                    {where}
                    ORDER BY sequence_number DESC
                    LIMIT %(limit)s OFFSET %(offset)s
                """, values)
                rows = cur.fetchall()

        records = AuditEventRecord.from_rows([dict(r) for r in rows])
        return records, total

    # ------------------------------------------------------------------
    # GET SINGLE  (spec §5.3.3)
    # ------------------------------------------------------------------

    def get_by_id(self, event_id: uuid.UUID) -> AuditEventRecord:
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT * FROM audit_events WHERE id = %(id)s", {"id": event_id})
                row = cur.fetchone()

        if not row:
            raise FDQException(
                status_code=404,
                code=ErrorCode.NOT_FOUND,
                message=f"Audit event '{event_id}' not found.",
            )
        return AuditEventRecord.from_row(dict(row))

    # ------------------------------------------------------------------
    # GET ENTITY HISTORY  (spec §5.3.4)
    # ------------------------------------------------------------------

    def get_entity_history(
        self,
        aggregate_type: str,
        aggregate_id:   str,
        from_sequence:  int | None,
        to_sequence:    int | None,
        event_type:     str | None,
        page:           int,
        page_size:      int,
    ) -> tuple[list[AuditEventRecord], int]:

        filters = ["aggregate_type = %(aggregate_type)s", "aggregate_id = %(aggregate_id)s"]
        values: dict[str, Any] = {
            "aggregate_type": aggregate_type.upper(),
            "aggregate_id":   aggregate_id,
        }

        if from_sequence is not None:
            filters.append("sequence_number >= %(from_seq)s")
            values["from_seq"] = from_sequence
        if to_sequence is not None:
            filters.append("sequence_number <= %(to_seq)s")
            values["to_seq"] = to_sequence
        if event_type:
            filters.append("event_type = %(event_type)s")
            values["event_type"] = event_type.upper()

        where  = "WHERE " + " AND ".join(filters)
        limit  = min(page_size, settings.pagination_max_page_size_audit)
        offset = (page - 1) * limit
        values["limit"]  = limit
        values["offset"] = offset

        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(f"SELECT COUNT(*) AS total FROM audit_events {where}", values)
                total = cur.fetchone()["total"]

                # Chronological order — full entity history (spec §5.3.4)
                cur.execute(f"""
                    SELECT * FROM audit_events
                    {where}
                    ORDER BY sequence_number ASC
                    LIMIT %(limit)s OFFSET %(offset)s
                """, values)
                rows = cur.fetchall()

        records = AuditEventRecord.from_rows([dict(r) for r in rows])
        return records, total

    # ------------------------------------------------------------------
    # VERIFY CHAIN  (spec §5.3.5)
    # ------------------------------------------------------------------

    def verify_chain(self, body: ChainVerifyRequest) -> ChainVerifyResponse:
        """
        Re-compute and validate the hash chain for a sequence range.
        Independently re-derives each hash using the same canonical
        serialisation as append_event and compares against stored values.
        """
        with db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("""
                    SELECT
                        sequence_number, idempotency_key, aggregate_id,
                        event_type, occurred_at, payload,
                        previous_event_hash, event_hash
                    FROM audit_events
                    WHERE aggregate_type = %(agg_type)s
                      AND aggregate_id   = %(agg_id)s
                      AND sequence_number BETWEEN %(from_seq)s AND %(to_seq)s
                    ORDER BY sequence_number ASC
                """, {
                    "agg_type": body.aggregate_type,
                    "agg_id":   body.aggregate_id,
                    "from_seq": body.from_sequence,
                    "to_seq":   body.to_sequence,
                })
                events = cur.fetchall()

        verification_timestamp = datetime.now(timezone.utc)

        if not events:
            return ChainVerifyResponse(
                valid=True,
                events_verified=0,
                first_sequence=None,
                last_sequence=None,
                broken_at_sequence=None,
                verification_timestamp=verification_timestamp,
            )

        previous_hash: str | None = events[0]["previous_event_hash"]
        broken_at: int | None = None

        for event in events:
            occurred_at_iso = event["occurred_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

            canonical = json.dumps({
                "sequence_number": event["sequence_number"],
                "idempotency_key": str(event["idempotency_key"]),
                "aggregate_id":    event["aggregate_id"],
                "event_type":      event["event_type"],
                "occurred_at":     occurred_at_iso,
                "payload":         event["payload"],
                "previous_hash":   previous_hash or "",
            }, sort_keys=True, ensure_ascii=True)

            expected_hash = hashlib.new(
                settings.audit_hash_algorithm,
                canonical.encode(),
            ).hexdigest()

            if expected_hash != event["event_hash"]:
                broken_at = event["sequence_number"]
                break

            previous_hash = event["event_hash"]

        # Log result as an audit event itself — AUDIT_CHAIN_VERIFIED (Appendix A)
        log.info(
            "audit_chain_verified",
            aggregate_type=body.aggregate_type,
            aggregate_id=body.aggregate_id,
            valid=(broken_at is None),
            events_verified=len(events),
            broken_at_sequence=broken_at,
        )

        return ChainVerifyResponse(
            valid=(broken_at is None),
            events_verified=len(events),
            first_sequence=events[0]["sequence_number"],
            last_sequence=events[-1]["sequence_number"],
            broken_at_sequence=broken_at,
            verification_timestamp=verification_timestamp,
        )