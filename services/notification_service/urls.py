"""
services/notification_service/urls.py
------------------------------------------
URL configuration for Notification Service endpoints.

Maps HTTP paths to Django views.
"""

from django.urls import path
from . import views

app_name = 'notification_service'

urlpatterns = [
    path('dispatch/', views.dispatch_notification, name='dispatch_notification'),
    path('email/', views.send_direct_email, name='send_direct_email'),
    path('teams/', views.send_direct_teams, name='send_direct_teams'),
    path('history/', views.notification_history, name='notification_history'),
    path('<uuid:notification_id>/status/', views.get_notification_status, name='get_notification_status'),
    path('preferences/<uuid:user_id>/', views.update_preferences, name='update_preferences'),
    path('templates/<str:template_id>/', views.notification_template, name='notification_template'),
]
