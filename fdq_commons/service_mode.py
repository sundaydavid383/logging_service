"""
fdq_commons/service_mode.py
----------------------------
Detects the current service runtime mode for the Django process.

Detection order:
1. ``manage.py runserver <addr:port>`` — auto-detect from the port argument (local dev)
2. ``FDQ_SERVICE_MODE`` environment variable — explicit override (production / non-standard ports)
3. ``"gateway"`` — safe default

Port-to-mode mapping (local development defaults):
    8000 → gateway
    8001 → activity
    8002 → error
    8003 → audit
    8004 → notification
"""

from __future__ import annotations

import os
import sys


_PORT_MODE_MAP: dict[int, str] = {
    8000: 'gateway',
    8001: 'activity',
    8002: 'error',
    8003: 'audit',
    8004: 'notification',
}

# Reverse map: mode → expected port (for AppConfig guards)
_MODE_PORT_MAP: dict[str, int] = {v: k for k, v in _PORT_MODE_MAP.items()}


def get_service_mode() -> str:
    """
    Return the service mode for the current Django process.

    Checks ``sys.argv`` for a ``runserver <port>`` argument first (local dev
    auto-detection), then falls back to the ``FDQ_SERVICE_MODE`` env var, and
    finally to ``"gateway"`` as a safe default.
    """
    # 1. Auto-detect from `manage.py runserver` port argument
    for i, arg in enumerate(sys.argv):
        if arg == 'runserver' and i + 1 < len(sys.argv):
            addr: str = sys.argv[i + 1]
            # Handles "127.0.0.1:8001", "0.0.0.0:8001", or bare "8001"
            port_str = addr.split(':')[-1] if ':' in addr else addr
            if port_str.isdigit():
                port = int(port_str)
                if port in _PORT_MODE_MAP:
                    return _PORT_MODE_MAP[port]
            break

    # 2. Explicit environment variable (production / non-standard ports)
    env_mode = os.environ.get('FDQ_SERVICE_MODE', '').strip().lower()
    if env_mode:
        return env_mode

    # 3. Fallback
    return 'gateway'


def get_expected_mode(app_label: str) -> str | None:
    """
    Return the expected service mode for a given Django app label, or None
    if the label is not a recognised FDQ service app.

    Mapping:
        services.api_gateway        → gateway
        services.activity_logging   → activity
        services.error_logging      → error
        services.audit_trail        → audit
        services.notification_service → notification
    """
    mapping = {
        'services.api_gateway': 'gateway',
        'services.activity_logging': 'activity',
        'services.error_logging': 'error',
        'services.audit_trail': 'audit',
        'services.notification_service': 'notification',
    }
    return mapping.get(app_label)
