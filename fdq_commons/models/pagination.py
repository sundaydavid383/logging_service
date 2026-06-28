"""
fdq_commons/models/pagination.py
----------------------------------
Reusable pagination models and query helpers for all FDQ services.

PaginationParams accepts max_page_size so the SAME class can enforce
different caps per service:
  - activity_logs / error_logs -> settings.pagination_max_page_size_logs (200)
  - audit_events / notifications -> settings.pagination_max_page_size_audit (100)

This keeps one class, reusable everywhere — no duplicated logic.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar
from uuid import UUID

from typing import Optional
from pydantic import BaseModel, Field

from fdq_commons.config import settings

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Helper for robust cursor data property extraction
# ---------------------------------------------------------------------------
def _extract_record_id(record: Any) -> str | None:
    """
    Safely extracts an 'id' value from any record format — dict, Pydantic
    model, or plain object/dataclass.
    """
    if record is None:
        return None

    if isinstance(record, dict):
        val = record.get("id")
        return str(val) if val is not None else None

    if isinstance(record, BaseModel):
        if hasattr(record, "id"):
            val = getattr(record, "id")
            return str(val) if val is not None else None
        model_dict = record.model_dump()
        val = model_dict.get("id")
        return str(val) if val is not None else None

    if hasattr(record, "id"):
        val = getattr(record, "id")
        return str(val) if val is not None else None

    return None


# ---------------------------------------------------------------------------
# Query parameter dependency
# ---------------------------------------------------------------------------

class PaginationParams:
    """
    Helper that parses and validates pagination query parameters.

    max_page_size lets each route enforce its own cap:
        PaginationParams(page=page, page_size=page_size,
                         max_page_size=settings.pagination_max_page_size_audit)
    """

    def __init__(
        self,
        page: int = 1,
        page_size: int = settings.pagination_default_page_size,
        after_id: UUID | None = None,
        max_page_size: int = settings.pagination_max_page_size_logs,
    ) -> None:
        # Clamp page_size to the configured maximum — never trust the caller
        self.page_size = min(page_size, max_page_size)
        self.page = page
        self.after_id = after_id

    @property
    def offset(self) -> int:
        """SQL OFFSET value for offset-based pagination."""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """SQL LIMIT value."""
        return self.page_size

    @property
    def use_cursor(self) -> bool:
        """True when the caller supplied an after_id cursor."""
        return self.after_id is not None


class AuditPaginationParams(PaginationParams):
    """
    Pagination params with the tighter audit/notification page-size cap
    (100 instead of 200 — per spec §4.3.2, §5.3.2), plus sequence range
    filters used by the audit trail entity history and verify endpoints.
    """

    def __init__(
        self,
        page: int = 1,
        page_size: int = settings.pagination_default_page_size,
        after_id: UUID | None = None,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
    ) -> None:
        super().__init__(
            page=page,
            page_size=page_size,
            after_id=after_id,
            max_page_size=settings.pagination_max_page_size_audit,
        )

        if from_sequence is not None and to_sequence is not None:
            if from_sequence > to_sequence:
                raise ValueError("'from_sequence' cannot be greater than 'to_sequence'.")

        self.from_sequence = from_sequence
        self.to_sequence = to_sequence


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PaginationMeta(BaseModel):
    """The 'pagination' block returned in every list response."""
    page: int = Field(..., ge=1, description="Current page number.")
    page_size: int = Field(..., ge=1, description="Records in this page.")
    total: int = Field(..., ge=0, description="Total matching records across all pages.")
    total_pages: int = Field(..., ge=0, description="Total number of pages.")
    has_next: bool = Field(..., description="True if there is a next page.")
    has_previous: bool = Field(..., description="True if there is a previous page.")
    next_cursor: str | None = Field(
        None,
        description="UUID of the last record in this page. Use as after_id to advance.",
    )

    @classmethod
    def build(
        cls,
        page: int,
        page_size: int,
        total: int,
        last_id: str | None = None,
    ) -> "PaginationMeta":
        total_pages = max(1, -(-total // page_size)) if total > 0 else 1
        return cls(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
            next_cursor=last_id,
        )


class PaginatedResponse(BaseModel, Generic[T]):
    """
    Generic paginated list response.

    Build it with:
        PaginatedResponse.build(data=rows, params=params, total=total)
    """
    data: list[T] = Field(..., description="Array of result objects for this page.")
    pagination: PaginationMeta = Field(..., description="Pagination metadata.")

    @classmethod
    def build(
        cls,
        data: list[T],
        params: PaginationParams,
        total: int,
    ) -> "PaginatedResponse[T]":
        last_id = _extract_record_id(data[-1]) if data else None

        meta = PaginationMeta.build(
            page=params.page,
            page_size=params.page_size,
            total=total,
            last_id=last_id,
        )
        return cls(data=data, pagination=meta)


# ---------------------------------------------------------------------------
# Cursor-based pagination helper (for large exports)
# ---------------------------------------------------------------------------

class CursorPage(BaseModel, Generic[T]):
    """Cursor-based pagination response for large exports (> 10,000 records)."""
    data: list[T]
    next_cursor: str | None = Field(
        None,
        description="Pass this as after_id to fetch the next batch. Null means no more data.",
    )
    has_more: bool

    @classmethod
    def build(cls, data: list[T], limit: int) -> "CursorPage[T]":
        has_more = len(data) == limit
        next_cursor = _extract_record_id(data[-1]) if has_more and data else None
        return cls(data=data, next_cursor=next_cursor, has_more=has_more)