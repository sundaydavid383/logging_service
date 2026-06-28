"""
fdq_commons/middleware/django_rate_limit_headers.py
-----------------------------------------------------
Django WSGI middleware for rate limit response headers.

Replaces the previous ASGI RateLimitHeaderMiddleware.
Stamps informational X-RateLimit-* headers onto outbound HTTP responses.
"""

from __future__ import annotations

import time
from typing import Any, Callable

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
# Rate Limit Middleware
# ---------------------------------------------------------------------------

class DjangoRateLimitHeaderMiddleware:
    """
    Django WSGI middleware that stamps informational X-RateLimit-* headers 
    onto outbound HTTP responses.
    
    Note: In Django, this middleware is applied uniformly to all requests.
    For per-endpoint rate limiting, use view decorators or per-view configuration.
    """

    def __init__(self, get_response: Callable) -> None:
        """
        Django middleware initialization.
        
        Args:
            get_response: The next middleware or view in the chain.
        """
        self.get_response = get_response
        # For now, use a default endpoint group; Django views can override via request.fdq_rate_limit_group
        self.endpoint_group = "activity_write"
        self._limit, self._burst = _get_limits(self.endpoint_group)

    def __call__(self, request: Any) -> Any:
        # Allow views to override the endpoint group via request attribute
        endpoint_group = getattr(request, 'fdq_rate_limit_group', self.endpoint_group)
        limit, burst = _get_limits(endpoint_group)

        # Parse incoming gateway headers defensively
        try:
            limit = int(request.META.get('HTTP_X_KONG_RATELIMIT_LIMIT_MINUTE', limit))
        except (ValueError, TypeError):
            pass

        try:
            remaining_raw = request.META.get('HTTP_X_KONG_RATELIMIT_REMAINING_MINUTE')
            remaining = int(remaining_raw) if remaining_raw is not None else limit
        except (ValueError, TypeError):
            remaining = limit

        try:
            reset_raw = request.META.get('HTTP_X_KONG_RATELIMIT_RESET')
            reset_ts = int(reset_raw) if reset_raw is not None else self._calculate_fallback_reset()
        except (ValueError, TypeError):
            reset_ts = self._calculate_fallback_reset()

        # Call the next middleware/view in the chain
        response = self.get_response(request)

        # Inject rate limit headers into response
        response['X-RateLimit-Limit'] = str(limit)
        response['X-RateLimit-Remaining'] = str(max(0, remaining - 1))
        response['X-RateLimit-Reset'] = str(reset_ts)

        # Add Retry-After for 429 responses
        if response.status_code == 429:
            response['Retry-After'] = str(reset_ts)

        return response

    @staticmethod
    def _calculate_fallback_reset() -> int:
        """
        Calculate a reasonable fallback reset timestamp (60 seconds from now).
        """
        return int(time.time()) + 60
