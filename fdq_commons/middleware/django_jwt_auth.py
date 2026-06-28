"""
fdq_commons/middleware/django_jwt_auth.py
-------------------------------------------
Django decorator-based JWT authentication and scope validation.

Replaces FastAPI's Depends(require_scope()) with a custom Django view decorator.
Extracts token from Authorization header, verifies signature, validates scopes, 
and attaches claims to request.claims for view access.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

import jwt

from fdq_commons.config import settings
from fdq_commons.models.errors import ErrorCode, FDQException
from fdq_commons.middleware.jwt_auth import (
    _load_compiled_public_key,
    verify_token,
    _extract_token,
    _check_scopes,
)


def require_scope(*required_scopes: str, require_all: bool = True) -> Callable:
    """
    Django view decorator for JWT authentication with scope validation.
    
    Usage:
        @require_scope("logs:write")
        def record_activity_log(request):
            ...
        
        @require_scope("logs:read", "logs:audit")  # require ANY
        @require_scope("logs:write", require_all=False)  # require ALL
        def my_view(request):
            claims = request.claims  # Verified claims dict
            ...
    
    Args:
        *required_scopes: One or more required scopes.
        require_all: If True (default), token must have ALL scopes.
                     If False, token must have at least ONE.
    
    Raises:
        FDQException: If token is missing, invalid, expired, or lacks required scopes.
    """
    def decorator(view_func: Callable) -> Callable:
        @functools.wraps(view_func)
        def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            # 1. Extract Bearer token from Authorization header
            auth_header = request.META.get('HTTP_AUTHORIZATION', '')
            if not auth_header.startswith('Bearer '):
                raise FDQException(
                    status_code=401,
                    code=ErrorCode.UNAUTHORIZED,
                    message="Authorization header is missing. Provide a Bearer token.",
                )
            
            raw_token = auth_header.split(' ', 1)[1]
            
            # 2. Verify token signature and claims
            claims = verify_token(raw_token)
            
            # 3. Check scopes
            if required_scopes:
                _check_scopes(claims, required_scopes, require_all=require_all)
            
            # 4. Attach claims to request for view access
            request.claims = claims
            
            # 5. Call the actual view
            return view_func(request, *args, **kwargs)
        
        return wrapper
    
    return decorator


def require_any_scope(*scopes: str) -> Callable:
    """
    Django view decorator for JWT authentication requiring ANY of the given scopes.
    
    Usage:
        @require_any_scope("logs:read", "logs:audit")
        def my_view(request):
            ...
    """
    return require_scope(*scopes, require_all=False)
