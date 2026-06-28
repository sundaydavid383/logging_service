"""
fdq_commons
-----------
Shared commons library for all Fiducia DQMS microservices.

Every service imports from this package. Nothing is hardcoded here —
all configuration is driven by environment variables via fdq_commons.config.

Public API surface (import from these paths in service code):

    Settings & Config:
        from fdq_commons.config import settings

    Logging:
        from fdq_commons.logging_setup import configure_logging, bind_request_context

    Error models:
        from fdq_commons.models.errors import FDQException, ErrorCode, register_exception_handlers

    Pagination:
        from fdq_commons.models.pagination import PaginationParams, PaginatedResponse

    JWT Auth:
        from fdq_commons.middleware.jwt_auth import require_scope, require_any_scope, parse_caller

    Rate limit headers:
        from fdq_commons.middleware.rate_limit_headers import RateLimitHeaderMiddleware

    Request context:
        from fdq_commons.middleware.django_request_context import DjangoRequestContextMiddleware

    Health checks:
        from fdq_commons.middleware.health import health_router

    Database:
        from fdq_commons.db.session import get_db_conn, db_connection, check_db_health
        from fdq_commons.db.base_model import BaseRecord
        from fdq_commons.db.redis_client import get_redis, activity_idempotency_cache

    Utilities:
        from fdq_commons.utils.ip_validator import validate_ip_address, IPvAnyAddressStr
        from fdq_commons.utils.sanitiser import sanitise_free_text, apply_pii_mask, SanitisedFreeText
"""

__version__ = "1.0.0"
__author__ = "Fiducia Engineering Team"