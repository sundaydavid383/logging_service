"""
fdq_commons/middleware/request_context.py
------------------------------------------
Request context middleware — runs on every incoming request.
"""

from __future__ import annotations

import time
import uuid
from typing import Any



import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from fdq_commons.logging_setup import bind_request_context, clear_request_context

log = structlog.get_logger()

HEADER_CORRELATION_ID = "X-Correlation-ID"
HEADER_REQUEST_ID = "X-Request-ID"


class RequestContextMiddleware:
    """
    Pure ASGI middleware establishing immutable per-request tracking context.
    Bypasses BaseHTTPMiddleware task isolation bugs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Parse or initialize unique identification vectors
        headers_dict = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        
        correlation_id = headers_dict.get(HEADER_CORRELATION_ID.lower()) or str(uuid.uuid4())
        request_id = str(uuid.uuid4())

        # 2. Store variables on the ASGI scope state context dictionary safely
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["correlation_id"] = correlation_id
        scope["state"]["request_id"] = request_id

        # 3. Bind to thread-isolated logger context
        bind_request_context(
            correlation_id=correlation_id,
            request_id=request_id,
        )

        start_time = time.perf_counter()

        # Intercept outbound messaging to guarantee telemetry stamps are delivered
        async def send_with_tracing_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
                new_headers = list(message.get("headers", []))

                # Inject transaction tracing headers into downstream sockets
                new_headers.append((HEADER_CORRELATION_ID.encode("latin-1"), correlation_id.encode("latin-1")))
                new_headers.append((HEADER_REQUEST_ID.encode("latin-1"), request_id.encode("latin-1")))
                message["headers"] = new_headers

                # Calculate final request execution duration metrics
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

                # FIX: Wrap in try-except to prevent logging crashes from breaking the API response pipeline
                try:
                    # Execute explicit structured access log statement
                    log.info(
                        "http_request",
                        method=scope["method"],
                        path=scope["path"],
                        status_code=status_code,
                        duration_ms=duration_ms,
                        correlation_id=correlation_id,
                        request_id=request_id,
                    )
                except Exception as log_err:
                    # Fail-safe standard library backup logging
                    import logging
                    std_logger = logging.getLogger("fdq_commons.middleware")
                    std_logger.info(
                        f"[LOG_FALLBACK] http_request - method={scope['method']} path={scope['path']} "
                        f"status_code={status_code} duration_ms={duration_ms} error={log_err}"
                    )
            await send(message)

        try:
            # Propagate application flow down the pipeline
            await self.app(scope, receive, send_with_tracing_headers)
        finally:
            # Guaranteed cleanup safely executed at the end of the execution thread
            clear_request_context()


# ---------------------------------------------------------------------------
# Framework Context Access Helpers
# ---------------------------------------------------------------------------

def get_correlation_id(request: Any) -> str:
    """
    Retrieve correlation_id from the request context state layer.
    """
    state = getattr(request, "state", {})
    if isinstance(state, dict):
        return state.get("correlation_id") or str(uuid.uuid4())
    return getattr(state, "correlation_id", str(uuid.uuid4()))


def get_request_id(request: Any) -> str:
    """
    Retrieve request_id from the request context state layer.
    """
    state = getattr(request, "state", {})
    if isinstance(state, dict):
        return state.get("request_id") or str(uuid.uuid4())
    return getattr(state, "request_id", str(uuid.uuid4()))

