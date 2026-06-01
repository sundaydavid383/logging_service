"""
fdq_commons/middleware/rate_limit_headers.py
---------------------------------------------
Rate limit response header stamper for all FDQ services.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from fdq_commons.config import settings


# ---------------------------------------------------------------------------
# Endpoint group → limit/burst mapping
# ---------------------------------------------------------------------------

def _get_limits(endpoint_group: str) -> tuple[int, int]:
    """
    Return (limit_rpm, burst_rpm) for the given endpoint group.
    All values come from settings so they stay in sync with spec §2.4.
    """
    _map: dict[str, tuple[int, int]] = {
        "activity_write": (
            settings.rate_limit_activity_write_rpm,
            settings.rate_limit_activity_write_burst,
        ),
        "activity_read": (
            settings.rate_limit_activity_read_rpm,
            settings.rate_limit_activity_read_burst,
        ),
        "error_write": (
            settings.rate_limit_error_write_rpm,
            settings.rate_limit_error_write_burst,
        ),
        "audit_append": (
            settings.rate_limit_audit_append_rpm,
            settings.rate_limit_audit_append_burst,
        ),
        "audit_read": (
            settings.rate_limit_audit_read_rpm,
            settings.rate_limit_audit_read_burst,
        ),
        "notification_send": (
            settings.rate_limit_notification_send_rpm,
            settings.rate_limit_notification_send_burst,
        ),
    }
    return _map.get(endpoint_group, (1_000, 2_000))  # Safe default


# ---------------------------------------------------------------------------
# High-Performance Pure ASGI Middleware (Bypasses BaseHTTPMiddleware bugs)
# ---------------------------------------------------------------------------

class RateLimitHeaderMiddleware:
    """
    Pure ASGI middleware that stamps informational X-RateLimit-* headers 
    onto outbound HTTP responses by intercepting the ASGI 'http.response.start' message.
    """

    def __init__(self, app: ASGIApp, endpoint_group: str = "activity_write") -> None:
        self.app = app
        self.endpoint_group = endpoint_group
        self._limit, self._burst = _get_limits(endpoint_group)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # We only care about HTTP requests; pass web-sockets or lifespan events through untouched
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract inbound headers directly from the ASGI scope byte-matrix list
        headers_dict = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}

        # -- Parse incoming gateway headers defensively --------------------
        try:
            limit = int(headers_dict.get("x-kong-ratelimit-limit-minute", self._limit))
        except ValueError:
            limit = self._limit

        try:
            remaining_raw = headers_dict.get("x-kong-ratelimit-remaining-minute")
            remaining = int(remaining_raw) if remaining_raw is not None else limit
        except ValueError:
            remaining = limit

        try:
            reset_raw = headers_dict.get("x-kong-ratelimit-reset")
            reset_ts = int(reset_raw) if reset_raw is not None else self._calculate_fallback_reset()
        except ValueError:
            reset_ts = self._calculate_fallback_reset()

        # Define the interception wrapper for sending the response back out
        async def send_with_rate_limit_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                # Extract status code to look for 429 errors
                status_code = message.get("status", 200)
                
                # Reconstruct/append to headers safely as byte arrays per ASGI spec
                new_headers = list(message.get("headers", []))
                
                # Add our standard informational stamp headers
                new_headers.append((b"x-ratelimit-limit", str(limit).encode("latin-1")))
                new_headers.append((b"x-ratelimit-remaining", str(max(0, remaining)).encode("latin-1")))
                new_headers.append((b"x-ratelimit-reset", str(reset_ts).encode("latin-1")))

                # Inject Retry-After if we encounter a 429 payload limit break
                if status_code == 429:
                    retry_after = max(0, reset_ts - int(time.time()))
                    new_headers.append((b"retry-after", str(retry_after).encode("latin-1")))

                message["headers"] = new_headers

            await send(message)

        # Execute application stack pipeline with our custom send hook wrapper
        await self.app(scope, receive, send_with_rate_limit_headers)

    def _calculate_fallback_reset(self) -> int:
        now = int(time.time())
        return now + (60 - (now % 60))


# ---------------------------------------------------------------------------
# Standalone header builder (for use outside middleware, e.g. in tests)
# ---------------------------------------------------------------------------

def build_rate_limit_headers(
    endpoint_group: str,
    remaining: int | None = None,
    reset_ts: int | None = None,
) -> dict[str, str]:
    limit, _ = _get_limits(endpoint_group)
    _remaining = remaining if remaining is not None else limit

    now = int(time.time())
    _reset = reset_ts if reset_ts is not None else (now + (60 - (now % 60)))

    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(0, _remaining)),
        "X-RateLimit-Reset": str(_reset),
    }