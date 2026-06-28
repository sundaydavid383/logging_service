"""
services/audit_trail/urls.py
-----------------------------------
URL configuration for Audit Trail Service endpoints.

Maps HTTP paths to Django views.
"""

from django.urls import path
from . import views

app_name = 'audit_trail'

urlpatterns = [
    path('', views.audit_events_root, name='audit_events_root'),
    path('verify/', views.verify_chain, name='verify_chain'),
    path('verify-all/', views.verify_all_chains, name='verify_all_chains'),
    path('verify-all/<str:task_id>/', views.get_verify_all_status, name='get_verify_all_status'),
    path('entity/<str:aggregate_type>/<str:aggregate_id>/', views.get_entity_history, name='get_entity_history'),
    path('<uuid:event_id>/', views.get_audit_event, name='get_audit_event'),
]
