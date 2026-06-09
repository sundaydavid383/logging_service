"""
fdq_commons/db/base_model.py
-----------------------------
Base database model helpers for the FDQ platform.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Type, TypeVar

T = TypeVar("T", bound="BaseRecord")

# ---------------------------------------------------------------------------
# Shared column SQL fragments
# ---------------------------------------------------------------------------
COL_ID = "id UUID PRIMARY KEY DEFAULT gen_random_uuid()"
COL_CREATED_AT = "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
COL_CORRELATION_ID = "correlation_id UUID"


def _serialize_value(val: Any) -> Any:
    """Fast, memory-efficient type converter for dictionary primitives."""
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, dict):
        return {k: _serialize_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_serialize_value(v) for v in val]
    return val


@dataclass
class BaseRecord:
    """
    Base class for all psycopg2 row model objects.
    
    Uses keyword-only fields (kw_only=True) to seamlessly bypass the 
    Python Dataclass Inheritance Order restriction.
    """
    id: uuid.UUID = field(default_factory=uuid.uuid4, kw_only=True)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc), kw_only=True
    )

    @classmethod
    def from_row(cls: Type[T], row: dict[str, Any] | None) -> T | None:
        """
        Construct a model instance from a psycopg2 RealDictRow.
        Respects existing field defaults if a database query is a partial selection.
        """
        if row is None:
            return None

        fields_meta = dataclasses.fields(cls)
        instance_kwargs: dict[str, Any] = {}

        for f in fields_meta:
            if f.name in row:
                instance_kwargs[f.name] = row[f.name]
            else:
                # If field is missing from a partial SQL select:
                # 1. If it has an active factory, execute it
                if f.default_factory != dataclasses.MISSING:  # type: ignore[comparison-overlap]
                    instance_kwargs[f.name] = f.default_factory()
                # 2. If it has a standard static default, assign it
                elif f.default != dataclasses.MISSING:
                    instance_kwargs[f.name] = f.default
                # 3. If there is absolutely no fallback, safely bind None
                else:
                    instance_kwargs[f.name] = None

        return cls(**instance_kwargs)

    @classmethod
    def from_rows(cls: Type[T], rows: list[dict[str, Any]]) -> list[T]:
        """Bulk convert a list of psycopg2 rows into model instances."""
        return [cls.from_row(row) for row in rows if row is not None]

    def to_dict(self) -> dict[str, Any]:
        """
        Deep serialize the record into a clean dictionary.
        Uses a fast in-memory object traversal wrapper instead of an expensive JSON string cycle.
        """
        # dataclasses.asdict copies everything deeply into standard dict structures
        raw_dict = dataclasses.asdict(self)
        return {k: _serialize_value(v) for k, v in raw_dict.items()}