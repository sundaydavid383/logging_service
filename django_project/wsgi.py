"""
django_project/wsgi.py
------------------------
WSGI config for FDQ services.

This is the entry point for production WSGI servers (Gunicorn, uWSGI, etc).

Start with: gunicorn django_project.wsgi:application
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'django_project.settings')

application = get_wsgi_application()
