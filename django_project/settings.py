"""
django_project/settings.py
----------------------------
Django settings for FDQ services migration from FastAPI.

This configuration:
1. Uses WSGI (sync views only)
2. Does NOT use Django ORM; all queries via raw psycopg2
3. Loads all settings from fdq_commons.config
4. Maintains tight parity with prior middleware/exception handling
"""

import os
from pathlib import Path

# Build paths inside the project
BASE_DIR = Path(__file__).resolve().parent.parent

# Load FDQ configuration
from fdq_commons.config import settings as fdq_settings

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-temporary-key-use-environment-variables-in-production'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = fdq_settings.is_development

ALLOWED_HOSTS = ['*']

# Application definition
INSTALLED_APPS = [
    # Built-in Django apps (minimal subset needed)
    # We do NOT use django.contrib.admin, auth, or contenttypes
    # since we're avoiding the ORM entirely
    
    # FDQ Services
    'services.activity_logging',
    'services.audit_trail',
    'services.error_logging',
    'services.notification_service',
]

MIDDLEWARE = [
    # Order matters! RequestContextMiddleware must be first to set correlation_id
    'fdq_commons.middleware.django_request_context.DjangoRequestContextMiddleware',
    'fdq_commons.middleware.django_rate_limit_headers.DjangoRateLimitHeaderMiddleware',
    'fdq_commons.middleware.django_exception_handler.DjangoExceptionMappingMiddleware',
    # Standard Django middleware (minimal set)
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'django_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'django_project.wsgi.application'

# Database — we do NOT use Django ORM, but keep this for migrations/schema management
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': fdq_settings.postgres_db,
        'USER': fdq_settings.postgres_user,
        'PASSWORD': fdq_settings.postgres_password,
        'HOST': fdq_settings.postgres_host,
        'PORT': fdq_settings.postgres_port,
        'CONN_MAX_AGE': 0,  # Disable persistent connections; we use psycopg2 pools directly
        'ATOMIC_REQUESTS': False,  # We manage transactions explicitly
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': fdq_settings.log_level,
    },
}

# FDQ-specific settings
FDQ_SETTINGS = {
    'api_v1_prefix': fdq_settings.api_v1_prefix,
    'environment': fdq_settings.environment,
    'service_name': fdq_settings.service_name,
}
