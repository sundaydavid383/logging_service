"""
services/api_gateway/urls.py
---------------------------------
URL routing for the API Gateway: auth endpoints and proxy catch-all.
"""

from django.urls import path
from . import views

app_name = 'api_gateway'

urlpatterns = [
    # Auth Endpoints
    path('auth/signup/', views.signup_view, name='auth_signup'),
    path('auth/token/', views.token_view, name='auth_token'),
    
    # Activity Logs Routes
    path('activity-logs/', views.proxy_view, {'service': 'activity', 'subpath': ''}, name='proxy_activity_base'),
    path('activity-logs/<path:subpath>', views.proxy_view, {'service': 'activity'}, name='proxy_activity_subpath'),
    
    # Error Logs Routes
    path('error-logs/', views.proxy_view, {'service': 'error', 'subpath': ''}, name='proxy_error_base'),
    path('error-logs/<path:subpath>', views.proxy_view, {'service': 'error'}, name='proxy_error_subpath'),
    
    # Audit Events Routes
    path('audit-events/', views.proxy_view, {'service': 'audit', 'subpath': ''}, name='proxy_audit_base'),
    path('audit-events/<path:subpath>', views.proxy_view, {'service': 'audit'}, name='proxy_audit_subpath'),
    
    # Notifications Routes
    path('notifications/', views.proxy_view, {'service': 'notification', 'subpath': ''}, name='proxy_notification_base'),
    path('notifications/<path:subpath>', views.proxy_view, {'service': 'notification'}, name='proxy_notification_subpath'),
]