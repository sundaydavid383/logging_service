"""
fdq_commons/middleware/django_request_context.py
---------------------------------------------------
Django WSGI middleware for request context (correlation IDs, request IDs).

This replaces the previous ASGI RequestContextMiddleware.
Establishes immutable per-request tracking context and injects headers into responses.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

import structlog

from fdq_commons.logging_setup import bind_request_context, clear_request_context

log = structlog.get_logger()

HEADER_CORRELATION_ID = "X-Correlation-ID"
HEADER_REQUEST_ID = "X-Request-ID"


class DjangoRequestContextMiddleware:
    """
    Django WSGI middleware establishing immutable per-request tracking context.
    
    Attaches correlation_id and request_id to request.fdq_context for view layer access.
    Injects tracing headers into outbound HTTP responses.
    """

    def __init__(self, get_response: Callable) -> None:
        """
        Django middleware initialization.
        
        Args:
            get_response: The next middleware or view in the chain.
        """
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        # 1. Parse or initialize unique identification vectors
        correlation_id = request.META.get(
            f"HTTP_{HEADER_CORRELATION_ID.upper().replace('-', '_')}",
            None,
        ) or str(uuid.uuid4())
        request_id = str(uuid.uuid4())

        # 2. Store variables on the request object safely
        if not hasattr(request, 'fdq_context'):
            request.fdq_context = {}
        request.fdq_context['correlation_id'] = correlation_id
        request.fdq_context['request_id'] = request_id

        # 3. Bind to thread-isolated logger context
        bind_request_context(
            correlation_id=correlation_id,
            request_id=request_id,
        )

        start_time = time.perf_counter()

        # 4. Call the next middleware/view in the chain
        response = self.get_response(request)

        # 5. Inject tracing headers into response
        response[HEADER_CORRELATION_ID] = correlation_id
        response[HEADER_REQUEST_ID] = request_id

        # 6. Calculate and log request duration metrics
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        
        try:
            # Execute explicit structured access log statement
            log.info(
                "http_request",
                method=request.method,
                path=request.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                request_id=request_id,
            )
        except Exception as log_err:
            # Fail-safe standard library backup logging
            import logging
            std_logger = logging.getLogger("fdq_commons.middleware")
            std_logger.info(
                f"[LOG_FALLBACK] http_request - method={request.method} path={request.path} "
                f"status_code={response.status_code} duration_ms={duration_ms} error={log_err}"
            )

        # 7. Guaranteed cleanup
        clear_request_context()

        return response
