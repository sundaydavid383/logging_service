"""
services/error_logging/apps.py
-----------------------------------
Django AppConfig for the Error Logging Service.

Handles lifecycle initialization:
- Startup: Initialize connection pools and Redis client
- Shutdown: Clean up resources
"""

import logging
import atexit

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class ErrorLoggingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'services.error_logging'
    verbose_name = 'FDQ Error Logging Service'

    def ready(self) -> None:
        logger.info("Initializing Error Logging Service...")

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
            logger.info("Shutting down FDQ Error Logging Service...")
            close_pool()
            close_redis()
            logger.info("Shutdown complete.")

        atexit.register(shutdown_handler)
        logger.info("Error Logging Service ready.")
