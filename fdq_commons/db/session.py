"""
fdq_commons/db/session.py
--------------------------
Database session and connection pool management using psycopg2.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool

from fdq_commons.config import settings

logger = logging.getLogger(__name__)

# Global singletons guarded by a thread synchronization primitive
_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _build_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """
    Build and return the psycopg2 ThreadedConnectionPool.
    All parameters are pulled from settings — no hardcoded strings.
    """
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=settings.postgres_min_connections,
        maxconn=settings.postgres_max_connections,
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
        options=f"-c statement_timeout={settings.postgres_statement_timeout_ms}",
        cursor_factory=psycopg2.extras.RealDictCursor,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """
    Return the module-level connection pool, creating it on first call.
    Thread-safe: protected by a mutual-exclusion lock to prevent initialization races.
    """
    global _pool
    if _pool is None or _pool.closed:
        with _pool_lock:
            # Double-checked locking pattern to avoid overhead after initialization
            if _pool is None or _pool.closed:
                _pool = _build_pool()
                logger.info(
                    "psycopg2 connection pool created: min=%d max=%d host=%s db=%s",
                    settings.postgres_min_connections,
                    settings.postgres_max_connections,
                    settings.postgres_host,
                    settings.postgres_db,
                )
                # Secure global type adaptation exactly once at pool instantiation boundary
                psycopg2.extras.register_uuid()
    return _pool


def close_pool() -> None:
    """
    Close all connections in the pool safely under lock.
    """
    global _pool
    if _pool is not None:
        with _pool_lock:
            if _pool and not _pool.closed:
                _pool.closeall()
                logger.info("psycopg2 connection pool closed.")
            _pool = None


# ---------------------------------------------------------------------------
# Connection Context Managers / Dependencies
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def db_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Context manager that checks out a connection from the pool,
    yields it, commits on clean exit, and returns it to the pool.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        # Only commit if the transaction block didn't explicitly clear or rollback itself
        if conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
            conn.commit()
    except Exception:
        if conn.status != psycopg2.extensions.STATUS_READY:
            conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def get_db_conn() -> Generator[psycopg2.extensions.connection, None, None]:
    """
    FastAPI dependency providing a database connection for a single request.
    Identical business logic execution as db_connection context manager.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        if conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
            conn.commit()
    except Exception:
        if conn.status != psycopg2.extensions.STATUS_READY:
            conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------------------
# Clean Nested Transaction Isolation (Guards Spec §5.3.1)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def transaction(
    conn: psycopg2.extensions.connection,
) -> Generator[psycopg2.extensions.cursor, None, None]:
    """
    Context manager for a discrete nested transaction block within an active connection.
    Leverages native PostgreSQL SAVEPOINTS to prevent parent transaction pollution.
    """
    savepoint_name = f"sp_{int(time.perf_counter_ns())}"
    cur = conn.cursor()
    
    # Open the isolated transaction savepoint boundary
    cur.execute(f"SAVEPOINT {savepoint_name}")
    try:
        yield cur
        cur.execute(f"RELEASE SAVEPOINT {savepoint_name}")
    except Exception:
        cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
        raise
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Health check — for /ready endpoint
# ---------------------------------------------------------------------------

def check_db_health() -> dict[str, str]:
    """
    Verify database connectivity. Used by the /ready health check endpoint.
    """
    start = time.perf_counter()
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return {"status": "ok", "latency_ms": str(latency_ms)}
    except Exception as exc:
        raise RuntimeError(f"Database health check failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------

def dict_cursor(conn: psycopg2.extensions.connection) -> psycopg2.extensions.cursor:
    """Return a RealDictCursor for the given connection."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def uuid_cursor(conn: psycopg2.extensions.connection) -> psycopg2.extensions.cursor:
    """
    Return a uniform dictionary cursor. UUID casting is handled globally 
    at pool setup to prevent runtime serialization overhead loops.
    """
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)