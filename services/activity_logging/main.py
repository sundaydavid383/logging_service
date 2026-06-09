"""
services/activity_logging/main.py
-----------------------------------
FastAPI application entry point for the Activity Logging Service.

Start with:
turn on main.py: uvicorn services.activity_logging.main:app --host 0.0.0.0 --port 8001 --reload
turn on celery work: celery -A fdq_commons.tasks.celery_app worker --pool=solo --loglevel=info -Q fdq_default,fdq_logging,fdq_notifications,fdq_maintenance
turn on celery beats: celery -A fdq_commons.tasks.celery_app beat --loglevel=info
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fdq_commons.tasks.celery_app import celery_app
from fastapi.concurrency import run_in_threadpool

from fdq_commons.config import settings
from fdq_commons.logging_setup import configure_logging
from fdq_commons.middleware.rate_limit_headers import RateLimitHeaderMiddleware
from fdq_commons.middleware.request_context import RequestContextMiddleware
from fdq_commons.models.errors import register_exception_handlers
from fdq_commons.middleware.health import health_router

# Ensure we import the initiators alongside the closers
from fdq_commons.db.session import get_pool, close_pool
from fdq_commons.db.redis_client import get_redis, close_redis

from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # -----------------------------------------------------------------------
    # Startup Phase: Secure network resources before accepting traffic
    # -----------------------------------------------------------------------
    configure_logging()
    
    # Pre-warm connection pools so the first incoming request doesn't experience latency spikes
    if hasattr(settings, "postgres_dsn"):
        get_pool()
    
    get_redis()
    
    yield
    
    # -----------------------------------------------------------------------
    # Shutdown Phase: Safe, non-blocking cleanup of connection pool sockets
    # -----------------------------------------------------------------------
    # run_in_threadpool prevents sync socket closing from freezing the async event loop
    await run_in_threadpool(close_pool)
    await run_in_threadpool(close_redis)


app = FastAPI(
    title="FDQ Activity Logging Service",
    version="1.0.0",
    description="Captures every meaningful user or system action. Satisfies FR-SEC-05, FR-VER-10.",
    docs_url="/docs" if settings.swagger_ui_enabled else None,
    redoc_url=None,
    lifespan=lifespan,
)

# Middleware — order matters: context first, then rate limit headers
app.add_middleware(RequestContextMiddleware)
app.add_middleware(RateLimitHeaderMiddleware, endpoint_group="activity_write")

# Exception handlers
register_exception_handlers(app)

# Routers
app.include_router(health_router)
app.include_router(router)