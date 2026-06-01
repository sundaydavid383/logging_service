"""
fdq_commons/models/errors.py
-----------------------------
Standard error response envelope for all FDQ services (spec §2.3).

Every service returns errors in this exact shape. Never return a raw
FastAPI HTTPException without wrapping it through these models — that
would break the contract consumers depend on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from fdq_commons.utils.sanitiser import apply_pii_mask


# ---------------------------------------------------------------------------
# Helper to extract context trace IDs reliably
# ---------------------------------------------------------------------------
def _get_active_trace_id(request: Request | None) -> str:
    """
    Extract the active trace/correlation ID from request state or headers,
    falling back to a clean UUID if none exists.
    """
    if not request:
        return str(uuid4())
        
    # Check if a middleware has already assigned a tracking ID to the request state
    state_trace = getattr(request.state, "correlation_id", None) or getattr(request.state, "trace_id", None)
    if state_trace:
        return str(state_trace)
        
    # Check incoming request headers
    for header in ("x-correlation-id", "x-trace-id", "x-request-id"):
        header_val = request.headers.get(header)
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
# FDQException — raise this instead of Starlette/FastAPI HTTPException
# ---------------------------------------------------------------------------

class FDQException(StarletteHTTPException):
    """
    Standard platform exception carrying an airtight ErrorEnvelope payload.
    Inherits from StarletteHTTPException for clean middleware interoperability.
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
        super().__init__(status_code=status_code, detail=message)
        self.fdq_code = code
        self.fdq_message = message
        self.fdq_details = details or []
        self.fdq_trace_id = trace_id
        self.headers = headers

    def to_envelope(self, fallback_request: Request | None = None) -> ErrorEnvelope:
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

def make_error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    trace_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """
    Build a FastAPI JSONResponse with a serialized error envelope structure.
    Uses model_dump(mode="json") to enforce Pydantic customization factories.
    """
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or [],
            trace_id=trace_id or str(uuid4()),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


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
# FastAPI exception handlers — registered globally
# ---------------------------------------------------------------------------

async def fdq_exception_handler(request: Request, exc: FDQException) -> JSONResponse:
    """Handler for FDQException — returns the structured envelope securely."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_envelope(fallback_request=request).model_dump(mode="json"),
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: Any) -> JSONResponse:
    """
    Handler for Pydantic v2 RequestValidationError.
    Converts field errors into an ErrorDetail array with recursive PII scrubbing.
    """
    details = []
    trace_id = _get_active_trace_id(request)
    
    if hasattr(exc, "errors") and callable(exc.errors):
        for error in exc.errors():
            field_path = ".".join(str(loc) for loc in error.get("loc", []))
            
            # Extract raw error context input value
            raw_input = error.get("input")
            scrubbed_value = None
            
            if raw_input is not None:
                if isinstance(raw_input, dict):
                    scrubbed_value = apply_pii_mask(raw_input)
                elif isinstance(raw_input, (str, int, float, bool)):
                    # Guard simple value fields using field-name hints
                    field_lower = field_path.lower()
                    fake_payload = {field_lower: str(raw_input)}
                    masked_payload = apply_pii_mask(fake_payload)
                    scrubbed_value = masked_payload[field_lower] if masked_payload else "***"
                else:
                    scrubbed_value = "***"  # Safe default fallback for complex objects
            
            details.append(
                ErrorDetail(
                    field=field_path,
                    issue=error.get("type", "invalid"),
                    value=scrubbed_value,
                )
            )
            
    return make_error_response(
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR,
        message="Request validation failed. Check the 'details' array for field-level errors.",
        details=details,
        trace_id=trace_id,
    )


async def starlette_http_exception_handler(request: Request, exc: Any) -> JSONResponse:
    """
    Intercepts low-level Starlette exceptions (like malformed unparseable JSON inputs)
    to enforce response structural standard consistency.
    """
    trace_id = _get_active_trace_id(request)
    status_code = getattr(exc, "status_code", 400)
    detail_msg = getattr(exc, "detail", "Malformed request content payload.")
    headers = getattr(exc, "headers", None)
    
    code_mapping = {
        400: ErrorCode.INVALID_JSON,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
    }
    error_code = code_mapping.get(status_code, ErrorCode.VALIDATION_ERROR)

    return make_error_response(
        status_code=status_code,
        code=error_code,
        message=detail_msg,
        trace_id=trace_id,
        headers=headers,
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all for unhandled exceptions.
    Logs internally; returns a safe 500 without leaking stack traces.
    """
    import structlog
    trace_id = _get_active_trace_id(request)
    
    log = structlog.get_logger()
    log.error(
        "unhandled_exception",
        trace_id=trace_id,
        exc_type=type(exc).__name__,
        exc_message=str(exc),
        path=request.url.path,
    )
    return make_error_response(
        status_code=500,
        code=ErrorCode.INTERNAL_SERVER_ERROR,
        message="An unexpected error occurred. The incident has been logged.",
        trace_id=trace_id,
    )


def register_exception_handlers(app: Any) -> None:
    """Register all FDQ exception handlers on a FastAPI application."""
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(FDQException, fdq_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, starlette_http_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)