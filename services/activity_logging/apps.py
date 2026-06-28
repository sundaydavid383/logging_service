"""
services/activity_logging/apps.py
-----------------------------------
Django AppConfig for the Activity Logging Service.

Handles lifecycle initialization:
- Startup: Initialize connection pools and Redis client
- Shutdown: Clean up resources
"""

import logging
import atexit

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class ActivityLoggingConfig(AppConfig):
    """
    Django app configuration for Activity Logging Service.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'services.activity_logging'
    verbose_name = 'FDQ Activity Logging Service'

    def ready(self) -> None:
        """
        Called when Django app is ready. Initialize connection pools.
        
        This replaces the previous ASGI lifespan startup; AppConfig.ready() now handles startup.
        """
        logger.info("Initializing Activity Logging Service...")
        
        # 1. Configure structured logging
        from fdq_commons.logging_setup import configure_logging
        configure_logging()
        logger.info("Structured logging configured.")
        
        # 2. Pre-warm PostgreSQL connection pool
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
        
        # 3. Pre-warm Redis client
        from fdq_commons.db.redis_client import get_redis, check_redis_health
        try:
            redis_client = get_redis()
            health = check_redis_health()
            logger.info("Redis client initialized: %s", health)
        except Exception as exc:
            logger.error("Failed to initialize Redis: %s", exc)
            raise
        
        # 4. Register shutdown hooks (cleanup on graceful shutdown)
        from fdq_commons.db.session import close_pool
        from fdq_commons.db.redis_client import close_redis
        
        def shutdown_handler() -> None:
            """Called on application shutdown (SIGTERM, etc)."""
            logger.info("Shutting down FDQ Activity Logging Service...")
            close_pool()
            close_redis()
            logger.info("Shutdown complete.")
        
        atexit.register(shutdown_handler)
        logger.info("Activity Logging Service ready.")
