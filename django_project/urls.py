"""
django_project/urls.py
----------------------
Root URL configuration for FDQ Django services.

Routing is determined automatically from the process's command-line arguments
(local development) or the ``FDQ_SERVICE_MODE`` environment variable (production):

  "gateway"       (port 8000) — full gateway routes: auth + proxy catch-all
  "activity"      (port 8001) — activity-logging microservice only
  "error"         (port 8002) — error-logging microservice only
  "audit"         (port 8003) — audit-trail microservice only
  "notification"  (port 8004) — notification-service microservice only

Local development: simply run ``python manage.py runserver <port>`` and the
correct routing is selected automatically. No manual env var required.

Running a downstream service on its own port without mode separation causes
every proxied request to match the gateway catch-all proxy routes again,
producing an infinite proxy loop and a 504 Gateway Timeout after 30 s.
"""

from __future__ import annotations

import sys
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

from fdq_commons.service_mode import get_service_mode

mode = get_service_mode()

urlpatterns = [
    # ---------------------------------------------------------------------------
    # API Documentation — always available in every process
    # ---------------------------------------------------------------------------
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]


if mode == "gateway":
    # ===========================================================================
    # PORT 8000 — API Gateway
    # Routes: auth endpoints + proxy catch-all for all downstream services
    # ===========================================================================
    from django.urls import path
    from services.api_gateway import views as gateway_views

    urlpatterns += [
        path('api/v1/auth/signup/', gateway_views.signup_view, name='auth_signup'),
        path('api/v1/auth/token/', gateway_views.token_view, name='auth_token'),

        # Activity Logging proxy
        path('api/v1/activity-logs/', gateway_views.proxy_view,
             {'service': 'activity', 'subpath': ''}, name='proxy_activity_base'),
        path('api/v1/activity-logs/<path:subpath>', gateway_views.proxy_view,
             {'service': 'activity'}, name='proxy_activity_subpath'),

        # Error Logging proxy
        path('api/v1/error-logs/', gateway_views.proxy_view,
             {'service': 'error', 'subpath': ''}, name='proxy_error_base'),
        path('api/v1/error-logs/<path:subpath>', gateway_views.proxy_view,
             {'service': 'error'}, name='proxy_error_subpath'),

        # Audit Trail proxy
        path('api/v1/audit-events/', gateway_views.proxy_view,
             {'service': 'audit', 'subpath': ''}, name='proxy_audit_base'),
        path('api/v1/audit-events/<path:subpath>', gateway_views.proxy_view,
             {'service': 'audit'}, name='proxy_audit_subpath'),

        # Notification Service proxy
        path('api/v1/notifications/', gateway_views.proxy_view,
             {'service': 'notification', 'subpath': ''}, name='proxy_notification_base'),
        path('api/v1/notifications/<path:subpath>', gateway_views.proxy_view,
             {'service': 'notification'}, name='proxy_notification_subpath'),
    ]

elif mode == "activity":
    # ===========================================================================
    # PORT 8001 — Activity Logging Microservice (business logic only, no proxies)
    # ===========================================================================
    urlpatterns += [
        path('api/v1/activity-logs/',
             include('services.activity_logging.urls', namespace='activity_logging')),
    ]

elif mode == "error":
    # ===========================================================================
    # PORT 8002 — Error Logging Microservice (business logic only, no proxies)
    # ===========================================================================
    urlpatterns += [
        path('api/v1/error-logs/',
             include('services.error_logging.urls', namespace='error_logging')),
    ]

elif mode == "audit":
    # ===========================================================================
    # PORT 8003 — Audit Trail Microservice (business logic only, no proxies)
    # ===========================================================================
    urlpatterns += [
        path('api/v1/audit-events/',
             include('services.audit_trail.urls', namespace='audit_trail')),
    ]

elif mode == "notification":
    # ===========================================================================
    # PORT 8004 — Notification Service Microservice (business logic only, no proxies)
    # ===========================================================================
    urlpatterns += [
        path('api/v1/notifications/',
             include('services.notification_service.urls', namespace='notification_service')),
    ]


# ──────────────────────────────────────────────────────────────────────────────

from fdq_commons.models.errors import make_django_error_response, ErrorCode, _get_active_trace_id


def handler404(request, exception=None):
    return make_django_error_response(
        status_code=404,
        code=ErrorCode.NOT_FOUND,
        message=f"No endpoint matches '{request.path}'.",
        trace_id=_get_active_trace_id(request),
    )


def handler500(request):
    return make_django_error_response(
        status_code=500,
        code=ErrorCode.INTERNAL_SERVER_ERROR,
        message="An unexpected error occurred.",
        trace_id=_get_active_trace_id(request),
    )