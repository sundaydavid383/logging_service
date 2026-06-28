"""
django_project/urls.py
------------------------
Root URL configuration for FDQ Django services.

Routes all service-specific URLs by include() ing their respective urls.py files.
Maintains the /api/v1/ prefix for all service endpoints per spec.
"""

from django.urls import path, include
from fdq_commons.config import settings

api_v1_prefix = settings.api_v1_prefix.strip('/')

urlpatterns = [
    # Health check endpoint (can be added if needed)
    # path('health/', health_check_view),

    # Activity Logging Service
    path(f'{api_v1_prefix}/activity-logs/', include('services.activity_logging.urls')),

    # Audit Trail Service
    path(f'{api_v1_prefix}/audit-events/', include('services.audit_trail.urls')),

    # Error Logging Service
    path(f'{api_v1_prefix}/error-logs/', include('services.error_logging.urls')),

    # Notification Service
    path(f'{api_v1_prefix}/notifications/', include('services.notification_service.urls')),
]
