"""
services/error_logging/main.py
--------------------------------
FastAPI application entry point for the Error Logging Service.

Start with:
    uvicorn services.error_logging.main:app --host 0.0.0.0 --port 8002 --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from fdq_commons.config import settings
from fdq_commons.logging_setup import configure_logging
from fdq_commons.middleware.rate_limit_headers import RateLimitHeaderMiddleware
from fdq_commons.middleware.request_context import RequestContextMiddleware
from fdq_commons.models.errors import register_exception_handlers
from fdq_commons.middleware.health import health_router
from fdq_commons.db.session import get_pool, close_pool
from fdq_commons.db.redis_client import get_redis, close_redis

from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    get_pool()
    get_redis()
    yield
    close_pool()
    close_redis()


app = FastAPI(
    title="FDQ Error Logging Service",
    version="1.0.0",
    description=(
        "Captures exceptions and failures with full context for incident response. "
        "Satisfies FR-ETL-06, FR-ORCH-04."
    ),
    docs_url="/docs" if settings.swagger_ui_enabled else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(RateLimitHeaderMiddleware, endpoint_group="error_write")

register_exception_handlers(app)

app.include_router(health_router)
app.include_router(router)