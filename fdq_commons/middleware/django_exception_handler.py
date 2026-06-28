from django.http import JsonResponse
from fdq_commons.models.errors import (
    FDQException, 
    fdq_exception_handler_django, 
    generic_exception_handler_django
)

class DjangoExceptionMappingMiddleware:
    """
    Global Django WSGI Middleware to catch exceptions and wrap them
    into spec-compliant JSON envelopes.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Let the request pass normally through the view
        return self.get_response(request)

    def process_exception(self, request, exception):
        """
        Django automatically calls this hook when any view raises an unhandled error.
        """
        # 1. Catch our planned custom framework errors
        if isinstance(exception, FDQException):
            return fdq_exception_handler_django(request, exception)
        
        # 2. Catch unexpected system bugs or raw library crashes (500 errors)
        return generic_exception_handler_django(request, exception)