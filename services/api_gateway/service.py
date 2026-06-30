"""
services/api_gateway/service.py
---------------------------------
Business logic for the API Gateway: user account management,
password hashing, RS256 token issuance, and Redis capacity caching.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
import uuid
from typing import Any

import jwt
import structlog
from psycopg2.extras import RealDictCursor

from fdq_commons.config import settings
from fdq_commons.db.redis_client import get_redis
from fdq_commons.db.session import db_connection
from fdq_commons.models.errors import ErrorCode, FDQException

logger = structlog.get_logger()

_GATEWAY_SCOPE = (
    "logs:write logs:read audit:append audit:read audit:verify "
    "notifications:configure notifications:read notifications:send"
)

_PBKDF2_ITERATIONS = 200_000


def _get_redis():
    return get_redis()


def _now() -> int:
    return int(time.time())


def _load_private_key() -> str:
    key = settings.jwt_private_key
    if not key:
        raise RuntimeError(
            "JWT private key not configured. Set JWT_PRIVATE_KEY_PATH or JWT_PRIVATE_KEY_CONTENT."
        )
    return key


def hash_password(plain: str) -> str:
    salt = os.urandom(32)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=64)
    salt_b64 = base64.b64encode(salt).decode("utf-8")
    hash_b64 = base64.b64encode(dk).decode("utf-8")
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt_b64}${hash_b64}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        algo, iterations_str, salt_b64, hash_b64 = stored.split("$", 3)
        iterations = int(iterations_str)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError) as exc:
        logger.warning("malformed_password_hash", error=str(exc))
        return False
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations, dklen=len(expected))
    return hmac.compare_digest(dk, expected)


def generate_rs256_token(user_id: str, role: str) -> str:
    private_key = _load_private_key()
    now = _now()
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + 3600,
        "scope": _GATEWAY_SCOPE,
        "role": role,
    }
    token = jwt.encode(payload, private_key, algorithm=settings.jwt_algorithm)
    return token


def cache_token_balance(token: str, limit: int | None = None) -> None:
    balance_limit = limit if limit is not None else settings.gateway_token_balance_limit
    r = _get_redis()
    r.set(f"token_balance:{token}", str(balance_limit))


def get_token_balance(token: str) -> int | None:
    r = _get_redis()
    raw = r.get(f"token_balance:{token}")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def decrement_token_balance(token: str) -> int:
    r = _get_redis()
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
    result = r.eval(lua, 1, f"token_balance:{token}")
    return int(result)


class UserService:
    """Manage user accounts via raw psycopg2."""

    # One-time table bootstrap guard — avoids re-running CREATE TABLE IF NOT EXISTS
    # (and its pool.getconn() cycle) on every single UserService instantiation.
    _table_ensured: bool = False
    _table_ensured_lock = threading.Lock()

    def __init__(self) -> None:
        # Automatically ensure the database table schema exists on first instantiation
        self._ensure_table_exists()

    def _ensure_table_exists(self) -> None:
        """Create the users table if it does not exist yet.

        One-time operation per process: guarded by a class-level flag so we
        only hit the database once, no matter how many UserService instances
        the gateway creates across requests.
        """
        if UserService._table_ensured:
            return

        with UserService._table_ensured_lock:
            if UserService._table_ensured:
                return
            try:
                with db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS users (
                                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                                name VARCHAR(255) NOT NULL,
                                email VARCHAR(255) UNIQUE NOT NULL,
                                password_hash VARCHAR(255) NOT NULL,
                                role VARCHAR(64) NOT NULL DEFAULT 'user',
                                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                            );
                            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                            """)
            except Exception as exc:
                logger.error("failed_to_bootstrap_users_table", error=str(exc))
                raise
            else:
                UserService._table_ensured = True

    def create(self, name: str, email: str, password: str, role: str = "user") -> dict[str, Any]:
        user_id = str(uuid.uuid4())
        password_hash = hash_password(password)
        try:
            with db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        INSERT INTO users (id, name, email, password_hash, role)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id, name, email, role, created_at, updated_at
                        """,
                        (user_id, name, email, password_hash, role),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise FDQException(
                            status_code=500,
                            code=ErrorCode.DATABASE_ERROR,
                            message="Failed to create user account.",
                        )
                    return dict(row)
        except FDQException:
            raise
        except Exception as exc:
            print(f"\n[!!!] RAW POSTGRES ENGINE CRASH DETAIL: {str(exc)}\n")
            if "unique" in str(exc).lower():
                raise FDQException(
                    status_code=409,
                    code=ErrorCode.CONFLICT,
                    message="An account with this email already exists.",
                )
            logger.error("user_create_failed", error=str(exc))
            raise FDQException(
                status_code=500,
                code=ErrorCode.DATABASE_ERROR,
                message="User creation failed.",
            ) from exc

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            with db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, name, email, password_hash, role, created_at, updated_at FROM users WHERE email = %s",
                        (email,),
                    )
                    row = cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:
            logger.error("user_lookup_failed", error=str(exc))
            return None


class AuthService:
    """Issue tokens and manage Redis balance for authenticated sessions."""

    def signup(self, name: str, email: str, password: str, role: str = "user") -> dict[str, Any]:
        user_svc = UserService()
        user = user_svc.create(name=name, email=email, password=password, role=role)
        token = generate_rs256_token(user_id=user["id"], role=user["role"])
        cache_token_balance(token)
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": 3600,
            "user_id": str(user["id"]),
            "role": user["role"],
            "scope": _GATEWAY_SCOPE,
        }

    def login(self, email: str, password: str) -> dict[str, Any]:
        user_svc = UserService()
        user = user_svc.get_by_email(email)
        if not user:
            raise FDQException(
                status_code=401,
                code=ErrorCode.UNAUTHORIZED,
                message="Invalid email or password.",
            )
        if not verify_password(password, user["password_hash"]):
            raise FDQException(
                status_code=401,
                code=ErrorCode.UNAUTHORIZED,
                message="Invalid email or password.",
            )
        token = generate_rs256_token(user_id=user["id"], role=user["role"])
        cache_token_balance(token)
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": 3600,
            "user_id": str(user["id"]),
            "role": user["role"],
            "scope": _GATEWAY_SCOPE,
        }