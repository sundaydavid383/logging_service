"""
services/notification_service/main.py
---------------------------------------
FastAPI application entry point for the Notification Service.

Start with:
    uvicorn services.notification_service.main:app --host 0.0.0.0 --port 8004 --reload

Delivery is always async (spec §6.1) — POST /dispatch enqueues via
Celery and returns 202 immediately. The worker handles delivery,
retries, and suppression.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fdq_commons.tasks.celery_app import celery_app
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
    title="FDQ Notification Service",
    version="1.0.0",
    description=(
        "Outbound communication hub for FDQ alerts. "
        "EMAIL (SMTP), MS Teams (Webhooks), DASHBOARD channels. "
        "Satisfies FR-NOTIF-01/02/04/05/06."
    ),
    docs_url="/docs" if settings.swagger_ui_enabled else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(RateLimitHeaderMiddleware, endpoint_group="notification_send")

register_exception_handlers(app)

app.include_router(health_router)
app.include_router(router)