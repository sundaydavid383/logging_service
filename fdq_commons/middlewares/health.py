"""
fdq_commons/middleware/health.py
----------------------------------
Health check endpoints for all FDQ services.
"""

from __future__ import annotations

import asyncio
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from fdq_commons.config import settings
from fdq_commons.db.session import check_db_health
from fdq_commons.db.redis_client import check_redis_health

health_router = APIRouter(tags=["Health"])


@health_router.get(
    "/health",
    summary="Liveness probe",
    description="Returns 200 immediately. Used by Kubernetes to detect crashed pods.",
    response_description="Service is alive.",
)
async def liveness() -> JSONResponse:
    """
    Liveness — always returns 200 as long as the process is running.
    No external dependencies are evaluated here to prevent cascading restarts.
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "service": settings.service_name,
            "environment": settings.environment,
        },
    )


@health_router.get(
    "/ready",
    summary="Readiness probe",
    description=(
        "Checks database connectivity and Redis availability concurrently. "
        "Returns 503 if any required backend dependency is unhealthy."
    ),
)
async def readiness() -> JSONResponse:
    """
    Readiness — verifies required external dependencies are reachable.
    Offloads synchronous blocking database/Redis pings to an isolated
    worker thread-pool to keep the main ASGI event loop completely free.
    """
    
    # 1. Offload synchronous network pings to worker threads to execute concurrently
    loop = asyncio.get_running_loop()
    
    db_task = loop.run_in_executor(None, check_db_health)
    redis_task = loop.run_in_executor(None, check_redis_health)

    # Gather results simultaneously; maximum wait time is bounded by the slowest single check
    db_result, redis_result = await asyncio.gather(
        db_task, redis_task, return_exceptions=True
    )

    checks: dict[str, Any] = {}
    all_ok = True

    # 2. Process and defend Database health output
    if isinstance(db_result, Exception):
        checks["database"] = {"status": "error", "detail": str(db_result)}
        all_ok = False
    else:
        checks["database"] = db_result
        # Defensively check if the inner dictionary report explicitly states it failed
        if isinstance(db_result, dict) and db_result.get("status") in ("error", "unhealthy", "failed"):
            all_ok = False

    # 3. Process and defend Redis health output
    if isinstance(redis_result, Exception):
        checks["redis"] = {"status": "error", "detail": str(redis_result)}
        all_ok = False
    else:
        checks["redis"] = redis_result
        # Defensively check if the inner dictionary report explicitly states it failed
        if isinstance(redis_result, dict) and redis_result.get("status") in ("error", "unhealthy", "failed"):
            all_ok = False

    # 4. Construct response status code envelope
    status_code = 200 if all_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if all_ok else "degraded",
            "service": settings.service_name,
            "environment": settings.environment,
            "checks": checks,
        },
    )