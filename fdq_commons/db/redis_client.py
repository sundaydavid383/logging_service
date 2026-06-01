"""
fdq_commons/db/redis_client.py
--------------------------------
Redis client setup for the FDQ platform.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import redis

from fdq_commons.config import settings

logger = logging.getLogger(__name__)

# Global singletons guarded by a thread synchronization primitive
_redis_client: redis.Redis | None = None
_redis_lock = threading.Lock()


def get_redis() -> redis.Redis:
    """
    Return the module-level Redis client, creating it on first call.
    Thread-safe: protected by a mutual-exclusion lock to prevent initialization races.
    """
    global _redis_client
    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:
                _redis_client = redis.Redis(
                    host=settings.redis_host,
                    port=settings.redis_port,
                    db=settings.redis_db,
                    password=settings.redis_password,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                    retry_on_timeout=True,
                    health_check_interval=30,
                )
                logger.info(
                    "Redis client created: host=%s port=%d db=%d",
                    settings.redis_host,
                    settings.redis_port,
                    settings.redis_db,
                )
    return _redis_client


def close_redis() -> None:
    """Close the Redis connection. Call during application shutdown."""
    global _redis_client
    if _redis_client is not None:
        with _redis_lock:
            if _redis_client:
                _redis_client.close()
                logger.info("Redis client closed.")
            _redis_client = None


# ---------------------------------------------------------------------------
# Health check — for /ready endpoint
# ---------------------------------------------------------------------------

def check_redis_health() -> dict[str, str]:
    """Verify Redis connectivity. Used by the /ready health check endpoint."""
    start = time.perf_counter()
    try:
        r = get_redis()
        r.ping()
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return {"status": "ok", "latency_ms": str(latency_ms)}
    except Exception as exc:
        raise RuntimeError(f"Redis health check failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Idempotency cache with Atomic Reservation Mechanics
# ---------------------------------------------------------------------------

class IdempotencyCache:
    """
    Wrapper around Redis managing atomic idempotency states.
    
    States:
      - "PENDING": Key reserved; active request processing in progress.
      - Full Response Dict: Request completed successfully; return cached payload.
    """

    def __init__(self, namespace: str, default_ttl: int) -> None:
        self._namespace = namespace
        self._default_ttl = max(1, default_ttl)  # Guard against 0/negative limits

    def _key(self, idempotency_key: str) -> str:
        return f"idempotency:{self._namespace}:{idempotency_key}"

    def reserve_key(self, idempotency_key: str, lock_ttl: int = 15) -> bool:
        """
        Atomically attempts to reserve an execution key using SETNX.
        Returns True if reservation succeeded. Returns False if a duplicate
        request is currently processing or has already completed.
        """
        r = get_redis()
        # Set if not exists (nx=True) with a short lock timeout to avoid permanent lockouts on fatal crashes
        return bool(r.set(self._key(idempotency_key), "PENDING", ex=lock_ttl, nx=True))

    def get(self, idempotency_key: str) -> dict[str, Any] | str | None:
        """
        Retrieve cached context. Returns "PENDING" if another thread is currently
        running the query logic, or a dict response payload if finished.
        """
        r = get_redis()
        raw = r.get(self._key(idempotency_key))
        if raw is None:
            return None
        if raw == "PENDING":
            return "PENDING"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt idempotency cache entry: %s", idempotency_key)
            return None

    def set(
        self,
        idempotency_key: str,
        response_data: dict[str, Any],
        ttl: int | None = None,
    ) -> None:
        """Cache a completed response payload under the given idempotency key."""
        r = get_redis()
        effective_ttl = ttl if ttl is not None else self._default_ttl
        r.set(
            self._key(idempotency_key),
            json.dumps(response_data, default=str),
            ex=max(1, effective_ttl),
        )

    def delete(self, idempotency_key: str) -> None:
        """Explicitly remove an entry (crucial for resetting on execution crashes)."""
        r = get_redis()
        r.delete(self._key(idempotency_key))


# ---------------------------------------------------------------------------
# Pre-built cache instances
# ---------------------------------------------------------------------------

activity_idempotency_cache = IdempotencyCache(
    namespace="activity",
    default_ttl=settings.idempotency_ttl_activity,
)

audit_idempotency_cache = IdempotencyCache(
    namespace="audit",
    default_ttl=settings.idempotency_ttl_audit,
)

notification_idempotency_cache = IdempotencyCache(
    namespace="notification",
    default_ttl=settings.idempotency_ttl_notification,
)