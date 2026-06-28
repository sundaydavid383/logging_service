"""
services/error_logging/urls.py
-----------------------------------
URL configuration for Error Logging Service endpoints.

Maps HTTP paths to Django views.
"""

from django.urls import path
from . import views

app_name = 'error_logging'

urlpatterns = [
    path('stats/', views.get_error_stats, name='get_error_stats'),
    path('', views.list_error_logs, name='list_error_logs'),
    path('<uuid:error_id>/status/', views.update_error_status, name='update_error_status'),
    path('<uuid:error_id>/', views.get_error_log_by_id, name='get_error_log_by_id'),
]
