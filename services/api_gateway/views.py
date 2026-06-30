"""
services/api_gateway/views.py
-------------------------------
Django views for the API Gateway:
- POST /api/v1/auth/signup   (token-on-signup)
- POST /api/v1/auth/token    (login / token exchange)
- Proxy view for downstream microservice routes with JWT + Redis balance enforcement
"""

from __future__ import annotations

import json
from typing import Any
import structlog

from django.http import HttpRequest, HttpResponse
from rest_framework.decorators import api_view

from drf_spectacular.utils import extend_schema, OpenApiParameter

from fdq_commons.models.errors import (
    ErrorCode,
    FDQException,
    fdq_exception_handler_django,
    make_django_error_response,
)

from .proxies import enforce_redis_balance, extract_bearer, forward_request, verify_jwt
from .schemas import LoginRequest, SignupRequest
from .service import AuthService

log = structlog.get_logger()


def _parse_json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        return json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FDQException(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="Malformed JSON in request body.",
        ) from exc


# ---------------------------------------------------------------------------
# 1. SIGNUP VIEW WITH SWAGGER METADATA
# ---------------------------------------------------------------------------
@extend_schema(
    summary="User Account Registration",
    description="Registers a new platform profile using raw psycopg2. Instantly issues and returns a valid RS256 token upon successful account creation.",
    request={"application/json": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "example": "David Sunday"},
            "email": {"type": "string", "example": "david@example.com"},
            "password": {"type": "string", "example": "SecurePassword123"},
            "role": {"type": "string", "example": "developer"}
        },
        "required": ["name", "email", "password"]
    }},
    responses={201: dict}
)
@api_view(['POST'])
def signup_view(request: HttpRequest) -> HttpResponse:
    try:
        # Resolve DRF request wrapper to access raw WSGI request body cleanly
        django_request = request._request if hasattr(request, '_request') else request
        body = SignupRequest(**_parse_json_body(django_request))
    except FDQException as exc:
        return fdq_exception_handler_django(request, exc)
    except Exception as exc:
        log.error("signup_validation_failed", error=str(exc))
        return fdq_exception_handler_django(
            request,
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Request validation failed: {str(exc)}",
            ),
        )

    try:
        svc = AuthService()
        result = svc.signup(
            name=body.name,
            email=body.email,
            password=body.password,
            role=body.role,
        )
    except FDQException as exc:
        return fdq_exception_handler_django(request, exc)
    except Exception as exc:
        log.error("signup_unexpected", error=str(exc))
        return fdq_exception_handler_django(
            request,
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message="Signup failed due to an unexpected error.",
            ),
        )

    return HttpResponse(
        json.dumps(result),
        content_type="application/json",
        status=201,
    )


# ---------------------------------------------------------------------------
# 2. TOKEN VIEW WITH SWAGGER METADATA
# ---------------------------------------------------------------------------
@extend_schema(
    summary="Acquire Access Token (Login)",
    description="Validates identity credentials against the database and returns a fresh 3,600-second RS256 token linked to Redis balance monitoring.",
    request={"application/json": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "example": "david@example.com"},
            "password": {"type": "string", "example": "SecurePassword123"}
        },
        "required": ["email", "password"]
    }},
    responses={200: dict}
)
@api_view(['POST'])
def token_view(request: HttpRequest) -> HttpResponse:
    try:
        # Resolve DRF request wrapper to access raw WSGI request body cleanly
        django_request = request._request if hasattr(request, '_request') else request
        body = LoginRequest(**_parse_json_body(django_request))
    except FDQException as exc:
        return fdq_exception_handler_django(request, exc)
    except Exception as exc:
        log.error("login_validation_failed", error=str(exc))
        return fdq_exception_handler_django(
            request,
            FDQException(
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Request validation failed: {str(exc)}",
            ),
        )

    try:
        svc = AuthService()
        result = svc.login(email=body.email, password=body.password)
    except FDQException as exc:
        return fdq_exception_handler_django(request, exc)
    except Exception as exc:
        log.error("login_unexpected", error=str(exc))
        return fdq_exception_handler_django(
            request,
            FDQException(
                status_code=500,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message="Login failed due to an unexpected error.",
            ),
        )

    return HttpResponse(
        json.dumps(result),
        content_type="application/json",
        status=200,
    )


# ---------------------------------------------------------------------------
# 3. PROXY VIEW WITH SWAGGER METADATA
# ---------------------------------------------------------------------------
@extend_schema(
    summary="Gateway Reverse-Proxy Routing Path",
    description="Validates the RS256 token signature, executes an atomic decrement on its Redis limit index, and proxies the payload over to the targeted core microservice.",
    parameters=[
        OpenApiParameter(
            name="Authorization",
            type=str,
            location=OpenApiParameter.HEADER,
            description="Bearer <JWT_TOKEN_STRING>",
            required=True
        )
    ],
    responses={200: dict, 401: dict, 403: dict, 502: dict}
)
@api_view(['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def proxy_view(request: Any, service: str, subpath: str = "") -> HttpResponse:
    # Safely unpack the underlying HttpRequest from DRF wrapper for downstream requests compatibility
    django_request = request._request if hasattr(request, '_request') else request
    
    try:
        token = extract_bearer(django_request)
        verify_jwt(django_request, token)
        enforce_redis_balance(django_request, token)
    except FDQException as exc:
        return fdq_exception_handler_django(django_request, exc)

    try:
        return forward_request(service, django_request, subpath)
    except FDQException as exc:
        return fdq_exception_handler_django(django_request, exc)
    except Exception as exc:
        log.error("proxy_fatal", error=str(exc))
        return make_django_error_response(
            502,
            ErrorCode.SERVICE_UNAVAILABLE,
            "Proxy encountered an unexpected error.",
        )