"""
fdq_commons/middleware/health.py
----------------------------------
Health check endpoints for all FDQ services.
"""

from __future__ import annotations

import asyncio
from __future__ import annotations

import asyncio
from typing import Any

from django.http import JsonResponse

from fdq_commons.config import settings
from fdq_commons.db.session import check_db_health
from fdq_commons.db.redis_client import check_redis_health


def liveness(request=None) -> JsonResponse:
    """Liveness probe — returns 200 if process is alive."""
    return JsonResponse(
        {"status": "ok", "service": settings.service_name, "environment": settings.environment},
        status=200,
    )


async def readiness(request=None) -> JsonResponse:
    """Readiness probe — checks DB and Redis connectivity concurrently."""
    loop = asyncio.get_running_loop()
    db_task = loop.run_in_executor(None, check_db_health)
    redis_task = loop.run_in_executor(None, check_redis_health)
    db_result, redis_result = await asyncio.gather(db_task, redis_task, return_exceptions=True)

    checks: dict[str, Any] = {}
    all_ok = True

    if isinstance(db_result, Exception):
        checks["database"] = {"status": "error", "detail": str(db_result)}
        all_ok = False
    else:
        checks["database"] = db_result
        if isinstance(db_result, dict) and db_result.get("status") in ("error", "unhealthy", "failed"):
            all_ok = False

    if isinstance(redis_result, Exception):
        checks["redis"] = {"status": "error", "detail": str(redis_result)}
        all_ok = False
    else:
        checks["redis"] = redis_result
        if isinstance(redis_result, dict) and redis_result.get("status") in ("error", "unhealthy", "failed"):
            all_ok = False

    status_code = 200 if all_ok else 503
    return JsonResponse(
        {"status": "ok" if all_ok else "degraded", "service": settings.service_name, "environment": settings.environment, "checks": checks},
        status=status_code,
    )