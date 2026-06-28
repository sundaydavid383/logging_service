"""
fdq_commons/middleware/jwt_auth.py
------------------------------------
JWT authentication middleware and scope/permission checker for all FDQ services.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import jwt

from fdq_commons.config import settings
from fdq_commons.models.errors import ErrorCode, FDQException

# Django-based flows use fdq_commons.middleware.django_jwt_auth for view decorators.
_bearer_scheme = None


# ---------------------------------------------------------------------------
# Public key loader (Cached & Compiled for High Throughput)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_compiled_public_key() -> Any:
    """
    Load and parse the RS256 public key.
    Caching the compiled PyJWT key object avoids parsing the PEM string on every request.
    
    To execute key rotation without pod restarts (spec §10.3), call:
    _load_compiled_public_key.cache_clear()
    """
    key_path = settings.jwt_public_key_path
    if not key_path.exists():
        raise RuntimeError(
            f"JWT public key not found at '{key_path}'. "
            "Set JWT_PUBLIC_KEY_PATH in your environment."
        )
    raw_pem = key_path.read_text(encoding="utf-8")
    # Pre-compile the key object inside the cache wrapper
    return jwt.algorithms.RSAAlgorithm.from_jwk(raw_pem) if "{" in raw_pem else raw_pem


# ---------------------------------------------------------------------------
# Core token verification
# ---------------------------------------------------------------------------

def verify_token(raw_token: str) -> dict[str, Any]:
    """
    Verify a raw JWT string and return the decoded claims dict.
    """
    public_key = _load_compiled_public_key()

    try:
        # Enforce clock leeway tracking to prevent distributed cluster synchronization drops
        claims: dict[str, Any] = jwt.decode(
            raw_token,
            public_key,
            algorithms=[settings.jwt_algorithm],
            options={
                "require": ["exp", "iat", "sub"],
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
            },
            leeway=settings.jwt_clock_leeway_seconds, # Standardized buffer (e.g., 5 seconds)
        )
    except jwt.ExpiredSignatureError:
        raise FDQException(
            status_code=401,
            code=ErrorCode.TOKEN_EXPIRED,
            message="The Bearer token has expired. Request a new token from the IdP.",
        )
    except jwt.InvalidSignatureError:
        raise FDQException(
            status_code=401,
            code=ErrorCode.INVALID_TOKEN,
            message="Token signature verification failed.",
        )
    except (jwt.DecodeError, jwt.InvalidTokenError) as exc:
        raise FDQException(
            status_code=401,
            code=ErrorCode.INVALID_TOKEN,
            message=f"Token verification validation failed: {str(exc)}",
        )

    # Enforce maximum TTL constraint check (spec §2.1)
    iat = claims.get("iat", 0)
    exp = claims.get("exp", 0)
    token_ttl = exp - iat
    if token_ttl > settings.jwt_max_ttl_seconds:
        raise FDQException(
            status_code=401,
            code=ErrorCode.INVALID_TOKEN,
            message=f"Token TTL ({token_ttl}s) exceeds the platform maximum allowed configuration ({settings.jwt_max_ttl_seconds}s).",
        )

    return claims


def _extract_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None:
        raise FDQException(
            status_code=401,
            code=ErrorCode.UNAUTHORIZED,
            message="Authorization header is missing. Provide a Bearer token.",
        )
    if credentials.scheme.lower() != "bearer":
        raise FDQException(
            status_code=401,
            code=ErrorCode.UNAUTHORIZED,
            message=f"Unsupported auth scheme '{credentials.scheme}'. Use Bearer.",
        )
    return credentials.credentials


# ---------------------------------------------------------------------------
# Scope verification matching logic block
# ---------------------------------------------------------------------------

def _check_scopes(claims: dict[str, Any], required_scopes: tuple[str, ...], require_all: bool) -> None:
    raw_scope = claims.get("scope", claims.get("scopes", ""))
    token_scopes = set(raw_scope) if isinstance(raw_scope, list) else set(str(raw_scope).split())

    if not required_scopes:
        return

    if require_all:
        missing = set(required_scopes) - token_scopes
        if missing:
            raise FDQException(
                status_code=403,
                code=ErrorCode.INSUFFICIENT_SCOPE,
                message=f"Token is missing required scope(s): {', '.join(sorted(missing))}.",
            )
    else:
        if not (token_scopes & set(required_scopes)):
            raise FDQException(
                status_code=403,
                code=ErrorCode.INSUFFICIENT_SCOPE,
                message=f"Token must carry at least one of the following matching scopes: {', '.join(required_scopes)}.",
            )


# ---------------------------------------------------------------------------
# Unique Hash Classes for dependency deduplication
# ---------------------------------------------------------------------------

# FastAPI dependency helpers removed — use Django decorators in services.
def require_scope(*scopes: str):
    raise RuntimeError("Use fdq_commons.middleware.django_jwt_auth.require_scope for Django views")


def require_any_scope(*scopes: str):
    raise RuntimeError("Use fdq_commons.middleware.django_jwt_auth.require_any_scope for Django views")


# ---------------------------------------------------------------------------
# Caller Context Serialization Layer
# ---------------------------------------------------------------------------

class CallerContext:
    __slots__ = ("user_id", "role", "scopes", "client_id", "raw_claims")

    def __init__(self, claims: dict[str, Any]) -> None:
        self.user_id: str | None = claims.get("sub")
        self.role: str | None = claims.get("role") or claims.get("roles")
        raw_scope = claims.get("scope", claims.get("scopes", ""))
        self.scopes: set[str] = set(raw_scope) if isinstance(raw_scope, list) else set(str(raw_scope).split())
        self.client_id: str | None = claims.get("client_id") or claims.get("azp")
        self.raw_claims: dict[str, Any] = claims

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def is_system_client(self) -> bool:
        return self.user_id is None or self.client_id is not None

    def __repr__(self) -> str:
        return f"CallerContext(user_id={self.user_id!r}, role={self.role!r}, scopes={self.scopes!r})"


def parse_caller(claims: dict[str, Any]) -> CallerContext:
    return CallerContext(claims)