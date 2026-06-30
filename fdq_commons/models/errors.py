"""
fdq_commons/models/errors.py
-----------------------------
Standard error response envelope for all FDQ services (spec §2.3).

Every service returns errors in this exact shape. Never return a raw framework HTTPException without wrapping it through these models — that would break the contract consumers depend on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from django.http import HttpRequest, JsonResponse
from pydantic import BaseModel, Field

from fdq_commons.utils.sanitiser import apply_pii_mask


# ---------------------------------------------------------------------------
# Helper to extract context trace IDs reliably
# ---------------------------------------------------------------------------
def _get_active_trace_id(request: HttpRequest | None) -> str:
    """
    Extract the active trace/correlation ID from request state or headers,
    falling back to a clean UUID if none exists.
    """
    if not request:
        return str(uuid4())

    # Check for fdq_context set by Django middleware
    try:
        fdq_ctx = getattr(request, "fdq_context", None)
        if isinstance(fdq_ctx, dict) and fdq_ctx.get("correlation_id"):
            return str(fdq_ctx.get("correlation_id"))
    except Exception:
        pass

    # Check HTTP headers via request.headers if available
    headers = getattr(request, "headers", None)
    if headers and hasattr(headers, "get"):
        for header in ("x-correlation-id", "x-trace-id", "x-request-id"):
            header_val = headers.get(header)
            if header_val:
                return header_val

    # Fallback to Django META header keys
    if hasattr(request, "META"):
        for header in ("HTTP_X_CORRELATION_ID", "HTTP_X_TRACE_ID", "HTTP_X_REQUEST_ID"):
            header_val = request.META.get(header)
            if header_val:
                return header_val

    return str(uuid4())


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    """
    Field-level detail for validation errors.
    Mirrors the shape the spec shows in the 400/422 examples.
    """
    field: str = Field(..., description="The name of the field that caused the error.")
    issue: str = Field(..., description="Machine-readable issue code, e.g. 'required', 'invalid_format'.")
    value: Any | None = Field(None, description="The rejected value (automatically masked for PII safety).")

    model_config = {"extra": "ignore"}


class ErrorBody(BaseModel):
    """The inner 'error' object."""
    code: str = Field(
        ...,
        description="Machine-readable error code, e.g. 'VALIDATION_ERROR', 'UNAUTHORIZED'.",
    )
    message: str = Field(
        ...,
        description="Human-readable explanation safe to show to API consumers.",
    )
    details: list[ErrorDetail] = Field(
        default_factory=list,
        description="Field-level breakdown for validation errors. Empty for non-validation errors.",
    )
    trace_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Correlates to the audit log entry for this request.",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        description="ISO-8601 UTC timestamp when the error was generated.",
    )


class ErrorEnvelope(BaseModel):
    """
    The outer wrapper. Every error response from every FDQ service is
    exactly this shape — no exceptions.
    """
    error: ErrorBody


# ---------------------------------------------------------------------------
# FDQException — raise this instead of framework HTTPException
# ---------------------------------------------------------------------------

class FDQException(Exception):
    """
    Standard platform exception carrying an airtight ErrorEnvelope payload.
    Provides a framework-agnostic exception wrapper for consistent error envelopes.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[ErrorDetail] | None = None,
        trace_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        # Maintain Starlette-like attribute names for compatibility with existing code
        self.status_code = status_code
        self.fdq_code = code
        self.fdq_message = message
        self.fdq_details = details or []
        self.fdq_trace_id = trace_id
        self.headers = headers

    def to_envelope(self, fallback_request: HttpRequest | None = None) -> ErrorEnvelope:
        effective_trace = self.fdq_trace_id or _get_active_trace_id(fallback_request)
        return ErrorEnvelope(
            error=ErrorBody(
                code=self.fdq_code,
                message=self.fdq_message,
                details=self.fdq_details,
                trace_id=effective_trace,
            )
        )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def make_django_error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    trace_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JsonResponse:
    """Build a Django JsonResponse with the FDQ error envelope."""
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or [],
            trace_id=trace_id or str(uuid4()),
        )
    )
    resp = JsonResponse(envelope.model_dump(mode="json"), status=status_code)
    if headers:
        for k, v in headers.items():
            resp[k] = v
    return resp


# ---------------------------------------------------------------------------
# Standard error codes
# ---------------------------------------------------------------------------

class ErrorCode:
    # Auth
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    INVALID_TOKEN = "INVALID_TOKEN"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"

    # Validation
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_IP_ADDRESS = "INVALID_IP_ADDRESS"
    INVALID_UUID = "INVALID_UUID"
    INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    FIELD_TOO_LONG = "FIELD_TOO_LONG"
    INVALID_ENUM_VALUE = "INVALID_ENUM_VALUE"
    INVALID_JSON = "INVALID_JSON"

    # Resource
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # Rate limiting
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"

    # Audit
    AUDIT_MUTATION_DENIED = "AUDIT_MUTATION_DENIED"
    AUDIT_HASH_CHAIN_BROKEN = "AUDIT_HASH_CHAIN_BROKEN"

    # Server
    INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    DATABASE_ERROR = "DATABASE_ERROR"
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
    LOG_WRITE_FAILED = "LOG_WRITE_FAILED"


# ---------------------------------------------------------------------------
# Django-compatible exception helpers
# ---------------------------------------------------------------------------

def fdq_exception_handler_django(request: HttpRequest | None, exc: FDQException) -> JsonResponse:
    """Django-compatible handler for FDQException — returns JsonResponse."""
    return make_django_error_response(
        status_code=getattr(exc, "status_code", 500),
        code=exc.fdq_code,
        message=exc.fdq_message,
        details=exc.fdq_details,
        trace_id=exc.fdq_trace_id,
        headers=exc.headers,
    )


def validation_exception_handler_django(request: HttpRequest | None, exc: Any) -> JsonResponse:
    """
    Convert validation errors into FDQ error envelope for Django.
    """
    details: list[ErrorDetail] = []
    trace_id = _get_active_trace_id(request)

    if hasattr(exc, "errors") and callable(exc.errors):
        for error in exc.errors():
            field_path = ".".join(str(loc) for loc in error.get("loc", []))
            raw_input = error.get("input")
            scrubbed_value = None
            if raw_input is not None:
                if isinstance(raw_input, dict):
                    scrubbed_value = apply_pii_mask(raw_input)
                elif isinstance(raw_input, (str, int, float, bool)):
                    field_lower = field_path.lower()
                    fake_payload = {field_lower: str(raw_input)}
                    masked_payload = apply_pii_mask(fake_payload)
                    scrubbed_value = masked_payload[field_lower] if masked_payload else "***"
                else:
                    scrubbed_value = "***"
            details.append(ErrorDetail(field=field_path, issue=error.get("type", "invalid"), value=scrubbed_value))

    return make_django_error_response(
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR,
        message="Request validation failed. Check the 'details' array for field-level errors.",
        details=details,
        trace_id=trace_id,
    )


def generic_exception_handler_django(request: HttpRequest | None, exc: Exception) -> JsonResponse:
    """Catch-all for unhandled exceptions in Django views/middleware."""
    import structlog
    trace_id = _get_active_trace_id(request)
    log = structlog.get_logger()
    path = getattr(request, 'path', getattr(request, 'url', None))
    log.error(
        "unhandled_exception",
        trace_id=trace_id,
        exc_type=type(exc).__name__,
        exc_message=str(exc),
        path=path,
    )
    return make_django_error_response(
        status_code=500,
        code=ErrorCode.INTERNAL_SERVER_ERROR,
        message=f"{type(exc).__name__}: {str(exc)}",
        details=[ErrorDetail(field="_debug", issue=type(exc).__name__, value=str(exc))],
        trace_id=trace_id,
    )


def register_exception_handlers(app: Any) -> None:
    """No-op compatibility shim. Django apps register exceptions via middleware/views."""
    return