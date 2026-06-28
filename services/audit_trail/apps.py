"""
services/audit_trail/apps.py
-----------------------------------
Django AppConfig for the Audit Trail Service.

Handles lifecycle initialization:
- Startup: Initialize connection pools and Redis client
- Shutdown: Clean up resources
"""

import logging
import atexit

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class AuditTrailConfig(AppConfig):
    """Django app configuration for the Audit Trail Service."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'services.audit_trail'
    verbose_name = 'FDQ Audit Trail Service'

    def ready(self) -> None:
        logger.info("Initializing Audit Trail Service...")

        from fdq_commons.logging_setup import configure_logging
        configure_logging()
        logger.info("Structured logging configured.")

        from fdq_commons.db.session import get_pool
        try:
            pool = get_pool()
            logger.info(
                "PostgreSQL connection pool initialized: min=%d max=%d",
                pool.minconn,
                pool.maxconn,
            )
        except Exception as exc:
            logger.error("Failed to initialize PostgreSQL pool: %s", exc)
            raise

        from fdq_commons.db.redis_client import get_redis, check_redis_health
        try:
            redis_client = get_redis()
            health = check_redis_health()
            logger.info("Redis client initialized: %s", health)
        except Exception as exc:
            logger.error("Failed to initialize Redis: %s", exc)
            raise

        from fdq_commons.db.session import close_pool
        from fdq_commons.db.redis_client import close_redis

        def shutdown_handler() -> None:
            logger.info("Shutting down FDQ Audit Trail Service...")
            close_pool()
            close_redis()
            logger.info("Shutdown complete.")

        atexit.register(shutdown_handler)
        logger.info("Audit Trail Service ready.")
