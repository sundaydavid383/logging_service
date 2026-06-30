"""
django_project/settings.py
----------------------------
Django settings for FDQ API Gateway.

Architecture:
- This Django project acts as the API Gateway
- It validates JWT, enforces rate limits, then proxies to FastAPI microservices
- FastAPI services still run on ports 8001-8004
- All DB queries use raw psycopg2 — Django ORM is disabled
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

from fdq_commons.config import settings as fdq_settings

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-temporary-key-use-environment-variables-in-production"
)

DEBUG = fdq_settings.is_development

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    # No django.contrib.admin — we don't use the ORM or admin panel
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "drf_spectacular",
    # FDQ services
    "services.activity_logging",
    "services.audit_trail",
    "services.error_logging",
    "services.notification_service",
    "services.api_gateway",
]

MIDDLEWARE = [
    # Order matters — context middleware must be first
    "fdq_commons.middleware.django_request_context.DjangoRequestContextMiddleware",
    "fdq_commons.middleware.django_rate_limit_headers.DjangoRateLimitHeaderMiddleware",
    "fdq_commons.middleware.django_exception_handler.DjangoExceptionMappingMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "django_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "django_project.wsgi.application"

# Database — only used for Django internals, NOT for FDQ data
# FDQ services use psycopg2 pools directly via fdq_commons.db.session
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME":     fdq_settings.postgres_db,
        "USER":     fdq_settings.postgres_user,
        "PASSWORD": fdq_settings.postgres_password,
        "HOST":     fdq_settings.postgres_host,
        "PORT":     fdq_settings.postgres_port,
        "CONN_MAX_AGE":    0,
        "ATOMIC_REQUESTS": False,
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE     = "UTC"
USE_I18N      = True
USE_TZ        = True

STATIC_URL = "/static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
    ],
    "DEFAULT_PERMISSION_CLASSES": [],
    "EXCEPTION_HANDLER": "fdq_commons.models.errors.drf_exception_handler",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

# ---------------------------------------------------------------------------
# drf-spectacular — Swagger UI config
# Access at: http://localhost:8000/docs/
# ---------------------------------------------------------------------------
SPECTACULAR_SETTINGS = {
    "TITLE":       "FDQ — Logging, Audit Trail & Notification Services",
    "DESCRIPTION": (
        "Fiducia DQMS cross-cutting infrastructure services.\n\n"
        "**Services:**\n"
        "- Activity Logging `:8001`\n"
        "- Error Logging `:8002`\n"
        "- Audit Trail `:8003`\n"
        "- Notification Service `:8004`\n\n"
        "All endpoints require a valid RS256 Bearer token.\n"
        "The API Gateway validates the signature; each service enforces OAuth 2.0 scopes."
    ),
    "VERSION":              "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX":   "/api/v1/",
    "SECURITY": [{"BearerAuth": []}],
    "SWAGGER_UI_SETTINGS": {
        "persistAuthorization": True,
        "displayRequestDuration": True,
        "filter": True,
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version":                  1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level":    fdq_settings.log_level,
    },
}

# ---------------------------------------------------------------------------
# FDQ microservice URLs (where the proxy forwards to)
# ---------------------------------------------------------------------------
FDQ_SERVICE_URLS = {
    "activity":    "http://localhost:8001",
    "error":       "http://localhost:8002",
    "audit":       "http://localhost:8003",
    "notification": "http://localhost:8004",
}

FDQ_SETTINGS = {
    "api_v1_prefix": fdq_settings.api_v1_prefix,
    "environment":   fdq_settings.environment,
    "service_name":  fdq_settings.service_name,
}

# Force Celery app registration so tasks bind to Redis broker
from fdq_commons.tasks.celery_app import celery_app  # noqa: F401, E402

# ---------------------------------------------------------------------------
# Service Runtime Mode — consumed by django_project/urls.py to decide which
# route set to register (gateway proxy vs. individual microservice endpoints).
# Set via the env var FDQ_SERVICE_MODE or within DJANGO_SERVICE in .env.
# ---------------------------------------------------------------------------
FDQ_SERVICE_MODE = fdq_settings.fdq_service_mode