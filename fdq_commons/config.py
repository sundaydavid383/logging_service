"""
fdq_commons/config.py
---------------------
Centralised application settings for the Fiducia DQMS platform.

Every service imports settings from here.

Usage:
    from fdq_commons.config import settings

    dsn = settings.postgres_dsn          # full connection string
    kwargs = settings.postgres_conn_kwargs # for psycopg2.connect(**kwargs)
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 1. Flexible .env Loader: Target the repo root directory relative to this package file
_root_repo_path = Path(__file__).resolve().parent.parent
_possible_env_path = _root_repo_path / ".env"

if not _possible_env_path.exists():
    _possible_env_path = Path(os.getcwd()) / ".env"

load_dotenv(dotenv_path=_possible_env_path, override=False)


class Settings(BaseSettings):
    """
    Single source of truth for every configurable value in FDQ.

    All fields map 1-to-1 to environment variables. Pydantic validates
    types and constraints at startup so misconfiguration fails fast with
    a clear error rather than a cryptic runtime crash.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,      # POSTGRES_HOST == postgres_host
        extra="ignore",            # unknown env vars are silently ignored
    )

    # ------------------------------------------------------------------
    # PostgreSQL Configuration (Sensible defaults for local Docker development)
    # ------------------------------------------------------------------
    postgres_user: str = "fdq_user"
    postgres_password: str = "fdq_password"
    postgres_db: str = "fdq_db"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Connection pool tuning (safe defaults; override per service if needed)
    postgres_min_connections: int = 1
    postgres_max_connections: int = 20

    # Statement timeout in milliseconds (0 = no timeout)
    postgres_statement_timeout_ms: int = 30_000

    # ------------------------------------------------------------------
    # Redis Configuration
    # ------------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    celery_result_expires_seconds: int = 86400

    # Idempotency key TTL (seconds) — per spec: 60 s for activity logs
    idempotency_ttl_activity: int = 60
    # Per spec: 24 hours for audit events and notifications
    idempotency_ttl_audit: int = 86_400
    # Per spec: 24 hours for notifications
    idempotency_ttl_notification: int = 86_400

    # ------------------------------------------------------------------
    # JWT / Auth Configuration
    # ------------------------------------------------------------------
    jwt_algorithm: str = "RS256"
    jwt_public_key_path: Path = Path("./keys/public.pem")
    jwt_private_key_path: Path | None = Path("./keys/private.pem")
    
    # Explicitly mapped environment variables for raw key strings to bypass file-system reads
    jwt_public_key_fallback: str | None = Field(default=None, validation_alias="JWT_PUBLIC_KEY_CONTENT")
    jwt_private_key_fallback: str | None = Field(default=None, validation_alias="JWT_PRIVATE_KEY_CONTENT")

    # Maximum token lifetime in seconds — per spec: 3600
    jwt_max_ttl_seconds: int = 3_600

    # ------------------------------------------------------------------
    # Rate Limiting (requests per minute) — per spec section 2.4
    # ------------------------------------------------------------------
    rate_limit_activity_write_rpm: int = 5_000
    rate_limit_activity_write_burst: int = 8_000
    rate_limit_activity_read_rpm: int = 1_000
    rate_limit_activity_read_burst: int = 2_000

    rate_limit_error_write_rpm: int = 2_000
    rate_limit_error_write_burst: int = 4_000

    rate_limit_audit_append_rpm: int = 10_000
    rate_limit_audit_append_burst: int = 15_000
    rate_limit_audit_read_rpm: int = 500
    rate_limit_audit_read_burst: int = 1_000

    rate_limit_notification_send_rpm: int = 1_000
    rate_limit_notification_send_burst: int = 2_000

    # ------------------------------------------------------------------
    # Pagination Rules
    # ------------------------------------------------------------------
    pagination_default_page_size: int = 50
    pagination_max_page_size_logs: int = 200       # activity / error logs
    pagination_max_page_size_audit: int = 100      # audit / notifications
    pagination_cursor_export_threshold: int = 10_000  # switch to cursor above

    # ------------------------------------------------------------------
    # Error Deduplication (Error Logging Service)
    # ------------------------------------------------------------------
    error_dedup_window_seconds: int = 300   # per spec default

    # ------------------------------------------------------------------
    # Notifications & Suppression
    # ------------------------------------------------------------------
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_from_address: str = "noreply@fiducia.internal"
    smtp_username: str | None = None
    smtp_password: str | None = None

    # JSON string  →  {"dq-alerts": "https://...", "etl-ops": "https://..."}
    teams_channel_webhooks_raw: str = "{}"
    notification_retry_max: int = 5
    notification_retry_backoff_base_seconds: int = 60
    notification_suppression_window_seconds: int = 300

    # ------------------------------------------------------------------
    # Audit Trail Configuration
    # ------------------------------------------------------------------
    audit_hash_algorithm: str = "sha256"
    
    # Missing parameters required by celery_app.py Beat Schedule:
    audit_integrity_check_hour: int = 2     # Runs at 2:00 AM
    audit_integrity_check_day: int | str = 0
    audit_integrity_check_days: int = 7
    audit_security_alert_channel: str = "security-alerts"
    jwt_clock_leeway_seconds: int = 5

    # ------------------------------------------------------------------
    # Application / Environment Context
    # ------------------------------------------------------------------
    environment: str = "production"   # production | staging | development
    service_name: str = "fdq-logging-service"  # overridden per-service container
    log_level: str = "INFO"
    swagger_ui_enabled: bool = False   # disabled in production per spec 10.4
    api_v1_prefix: str = "/api/v1"

    # ------------------------------------------------------------------
    # Service Runtime Mode
    # ------------------------------------------------------------------
    # Controls which URL routes this process registers.
    # "gateway"   — acts as the API gateway on port 8000 (proxies downstream)
    # "activity"  — downstream microservice on port 8001
    # "error"     — downstream microservice on port 8002
    # "audit"     — downstream microservice on port 8003
    # "notification" — downstream microservice on port 8004
    fdq_service_mode: str = "gateway"

    # ------------------------------------------------------------------
    # API Gateway Configuration
    # ------------------------------------------------------------------
    gateway_domain: str = "localhost:8000"
    gateway_token_balance_limit: int = 1000
    service_activity_logging_url: str = "http://localhost:8001"
    service_error_logging_url: str = "http://localhost:8002"
    service_audit_trail_url: str = "http://localhost:8003"
    service_notification_service_url: str = "http://localhost:8004"

    # ------------------------------------------------------------------
    # Derived / computed validation properties
    # ------------------------------------------------------------------

    @field_validator("jwt_public_key_path", "jwt_private_key_path", mode="before")
    @classmethod
    def _coerce_path(cls, v: Any) -> Path | None:
        if v is None:
            return None
        return Path(v)

    @model_validator(mode="after")
    def _check_key_paths_exist(self) -> "Settings":
        """Warn if key files are missing at startup but do not fail if a local 
        fallback key configuration parameter is populated in .env."""
        if not self.jwt_public_key_fallback and not self.jwt_public_key_path.exists():
            import warnings
            warnings.warn(
                f"JWT public key file not found at {self.jwt_public_key_path} and no string fallback is in .env. "
                "Token validation will fail at runtime if authentication middleware is executed.",
                stacklevel=2,
            )
        return self

    # ------------------------------------------------------------------ #
    # Convenience properties for raw driver integrations                 #
    # ------------------------------------------------------------------ #

    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy / Alembic / psycopg2 compatible URL format."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_conn_kwargs(self) -> dict[str, Any]:
        """Convenient key-value parameter dict for direct psycopg2.connect(**kwargs) usage."""
        return {
            "host": self.postgres_host,
            "port": self.postgres_port,
            "database": self.postgres_db,
            "user": self.postgres_user,
            "password": self.postgres_password,
            "options": f"-c statement_timeout={self.postgres_statement_timeout_ms}"
        }

    @property
    def redis_url(self) -> str:
        """redis-py / Celery broker URL structure."""
        if self.redis_password:
            return (
                f"redis://:{self.redis_password}@{self.redis_host}"
                f":{self.redis_port}/{self.redis_db}"
            )
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def teams_channel_webhooks(self) -> dict[str, str]:
        """Parsed Teams webhook map without throwing configuration crashes."""
        try:
            return json.loads(self.teams_channel_webhooks_raw)
        except json.JSONDecodeError:
            return {}

    @property
    def jwt_public_key(self) -> str:
        """Returns the RS256 public key. Prioritizes the direct .env string variable 
        before reading from a localized file path system."""
        if self.jwt_public_key_fallback:
            return self.jwt_public_key_fallback
        return self.jwt_public_key_path.read_text(encoding="utf-8")

    @property
    def jwt_private_key(self) -> str | None:
        """Returns the RS256 private key. Prioritizes the direct .env string variable."""
        if self.jwt_private_key_fallback:
            return self.jwt_private_key_fallback
        if self.jwt_private_key_path and self.jwt_private_key_path.exists():
            return self.jwt_private_key_path.read_text(encoding="utf-8")
        return None

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def is_development(self) -> bool:
        return self.environment.lower() == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance. lru_cache ensures .env is
    read exactly once per process.
    """
    return Settings(_env_file=_possible_env_path)


settings: Settings = get_settings()