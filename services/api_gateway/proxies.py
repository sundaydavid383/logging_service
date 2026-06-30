"""
services/api_gateway/proxies.py
---------------------------------
HTTP reverse-proxy utilities for routing validated requests
to downstream microservice endpoints.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests
from django.http import HttpRequest, HttpResponse

from fdq_commons.config import settings
from fdq_commons.db.redis_client import get_redis
from fdq_commons.middleware.jwt_auth import verify_token
from fdq_commons.models.errors import (
    ErrorCode,
    FDQException,
    make_django_error_response,
)

logger = logging.getLogger(__name__)

_SERVICE_URLS: dict[str, str] = {
    "activity": settings.service_activity_logging_url,
    "error": settings.service_error_logging_url,
    "audit": settings.service_audit_trail_url,
    "notification": settings.service_notification_service_url,
}


def extract_bearer(request: HttpRequest) -> str:
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Bearer "):
        raise FDQException(
            status_code=401,
            code=ErrorCode.UNAUTHORIZED,
            message="Authorization header is missing. Provide a Bearer token.",
        )
    return auth.split(" ", 1)[1]


def verify_jwt(request: HttpRequest, token: str) -> dict[str, Any]:
    try:
        return verify_token(token)
    except FDQException as exc:
        raise FDQException(
            status_code=exc.status_code,
            code=exc.fdq_code,
            message=exc.fdq_message,
            headers=exc.headers,
        )


def enforce_redis_balance(request: HttpRequest, token: str) -> int:
    r = get_redis()
    key = f"token_balance:{token}"
    lua = """
    local current = redis.call('GET', KEYS[1])
    if current == false then
        return -1
    end
    local value = tonumber(current)
    if value <= 0 then
        return 0
    end
    redis.call('DECR', KEYS[1])
    return value
    """
    try:
        result = int(r.eval(lua, 1, key))
    except Exception as exc:
        logger.error("redis_balance_check_failed - Error: %s", str(exc))
        raise FDQException(
            status_code=500,
            code=ErrorCode.INTERNAL_SERVER_ERROR,
            message="Token balance verification unavailable.",
        ) from exc

    if result == -1:
        raise FDQException(
            status_code=401,
            code=ErrorCode.INVALID_TOKEN,
            message="Token has been revoked or not found.",
        )
    if result == 0:
        raise FDQException(
            status_code=401,
            code=ErrorCode.INVALID_TOKEN,
            message="Token balance exhausted. Request a new token.",
        )
    return result


def forward_request(service: str, request: HttpRequest, subpath: str = "") -> Any:
    target_base = _SERVICE_URLS.get(service)
    if not target_base:
        raise FDQException(
            status_code=500,
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=f"Unknown downstream service: {service}",
        )

    original_path = request.get_full_path()
    target_url = f"{target_base}{original_path}"
    method = request.method.upper()

    forward_headers = {}
    for key, value in request.headers.items():
        if key in (
            "Authorization",
            "Content-Type",
            "Accept",
            "X-Correlation-ID",
            "X-Trace-ID",
            "X-Request-Id",
        ):
            forward_headers[key] = value

    body = request.body if method in ("POST", "PUT", "PATCH") else None

    try:
        resp = requests.request(
            method=method,
            url=target_url,
            headers=forward_headers,
            data=body,
            timeout=30,
            allow_redirects=False,
        )
    except requests.ConnectionError as exc:
        # Fixed: Standard logger requires standard string parameters
        logger.error("proxy_connection_error targeting URL %s - Details: %s", target_url, str(exc))
        raise FDQException(
            status_code=503,
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=f"Downstream service at {target_base} is offline or unreachable.",
        ) from exc
    except requests.Timeout as exc:
        logger.error("proxy_timeout targeting URL %s - Details: %s", target_url, str(exc))
        raise FDQException(
            status_code=504,
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message="Downstream service timed out.",
        ) from exc
    except Exception as exc:
        logger.error("proxy_unexpected error - Details: %s", str(exc))
        raise FDQException(
            status_code=502,
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message="Proxy encountered an unexpected error.",
        ) from exc

    response = HttpResponse(
        content=resp.content,
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", "application/json"),
    )
    for key, value in resp.headers.items():
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
            response[key] = value
    return response