"""
fdq_commons/middleware/django_exception_handler.py
-----------------------------------------------------
Global exception-to-JSON middleware for Django.

Solves:
  1. Django's default 404/500 HTML pages — replaced with JSON envelope
     via handler404/handler500 (registered in django_project/urls.py)
  2. Unhandled exceptions (e.g. raw psycopg2.IntegrityError) — caught
     via process_exception, real error detail ALWAYS included in the
     response (every caller is a developer integrating with this API —
     there is no "production end user" hitting these endpoints directly)
  3. FDQException — routed to the structured envelope as before

This middleware must be LAST in MIDDLEWARE so process_exception sees
exceptions raised anywhere downstream, including from views.py.
"""

from __future__ import annotations

from django.http import JsonResponse

from fdq_commons.models.errors import (
    FDQException,
    fdq_exception_handler_django,
    generic_exception_handler_django,
)


class DjangoExceptionMappingMiddleware:
    """
    Global Django WSGI Middleware to catch exceptions and wrap them
    into spec-compliant JSON envelopes.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        """
        Django automatically calls this hook when any view raises an
        unhandled error — including raw psycopg2 errors, KeyErrors,
        anything that escapes a view's own try/except.
        """
        if isinstance(exception, FDQException):
            return fdq_exception_handler_django(request, exception)

        return generic_exception_handler_django(request, exception)