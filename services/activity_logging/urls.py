"""
services/activity_logging/urls.py
-----------------------------------
URL configuration for Activity Logging Service endpoints.

Maps HTTP paths to Django views.
"""

from django.urls import path
from . import views

app_name = 'activity_logging'

urlpatterns = [
    # GET /api/v1/activity-logs/summary
    path('summary/', views.get_activity_summary, name='get_activity_summary'),
    
    # GET/POST /api/v1/activity-logs
    # POST = record_activity_log, GET = list_activity_logs (methods handled by @require_http_methods)
    path('', views.activity_logs_root, name='activity_logs_root'),
    
    # GET /api/v1/activity-logs/<log_id>
    path('<str:log_id>/', views.get_activity_log, name='get_activity_log'),
]
