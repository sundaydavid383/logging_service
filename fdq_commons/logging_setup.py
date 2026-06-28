"""
fdq_commons/logging_setup.py
-----------------------------
Structured logging for the entire FDQ platform.

Design decisions driven by the tech spec:
  - JSON output in production  →  ELK stack can index every field (§1.3)
  - Human-readable in dev      →  developer experience
  - Every log line carries service_name, environment, and a trace/correlation_id
    automatically so logs can be correlated without manual instrumentation
  - Uses structlog so callers write:
        log.info("activity_logged", log_id=..., event_type=...)
    and the output is always structured, never a bare printf string
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from fdq_commons.config import settings


# ---------------------------------------------------------------------------
# Custom processors
# ---------------------------------------------------------------------------

def _add_service_context(
    logger: Any,
    method: str,
    event_dict: EventDict,
) -> EventDict:
    """
    Inject service_name and environment into every log record automatically.
    Callers never need to remember to add these.
    """
    event_dict.setdefault("service", settings.service_name)
    event_dict.setdefault("environment", settings.environment)
    return event_dict


def _sanitise_log_record(
    logger: Any,
    method: str,
    event_dict: EventDict,
) -> EventDict:
    """
    Prevent log injection (spec §11.3).
    Strip newlines and null bytes from the 'event' field and any string value
    so a crafted message cannot split a log line or inject fake log entries.
    """
    _UNSAFE = ("\n", "\r", "\x00")
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            for char in _UNSAFE:
                value = value.replace(char, " ")
            event_dict[key] = value
    return event_dict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_logging(
    log_level: str | None = None,
    force_json: bool | None = None,
) -> None:
    """
    Configure structlog and the standard library root logger.

    Call once at application startup — in your Django AppConfig.ready() or WSGI entrypoint. Calling more than once is safe (idempotent).

    Args:
        log_level:  Override the log level from settings (useful in tests).
        force_json: Override the JSON/pretty decision (useful in tests).
    """
    resolved_level = (log_level or settings.log_level).upper()
    use_json = force_json if force_json is not None else settings.is_production

    # -- Standard library logging → structlog bridge ----------------------
    # Any third-party library that uses stdlib logging (web server, psycopg2,
    # celery, etc.) will emit structured logs via this bridge.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=resolved_level,
    )
    # Silence overly verbose libraries at WARNING unless we are in DEBUG
    _noisy = ["gunicorn.error", "httpx", "asyncio"]
    for name in _noisy:
        lvl = logging.DEBUG if resolved_level == "DEBUG" else logging.WARNING
        logging.getLogger(name).setLevel(lvl)

    # -- Shared processors run on every log call --------------------------
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,   # inject request-scoped ctx
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_service_context,
        _sanitise_log_record,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if use_json:
        # Production: one JSON object per line — ELK ingests directly
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        # Development: coloured, readable output
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, resolved_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Request-scoped context helpers
# ---------------------------------------------------------------------------

def bind_request_context(
    correlation_id: str | None = None,
    request_id: str | None = None,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
) -> None:
    """
    Bind values to the current async context so every subsequent log call
    in this request automatically includes them without explicit passing.

    Call this inside your Django middleware after extracting JWT claims.

    Example:
        bind_request_context(
            correlation_id=str(body.correlation_id),
            actor_user_id=str(claims["sub"]),
        )
        log.info("endpoint_called")  # → includes correlation_id automatically
    """
    structlog.contextvars.bind_contextvars(
        **{k: v for k, v in {
            "correlation_id": correlation_id,
            "request_id": request_id,
            "actor_user_id": actor_user_id,
            "actor_role": actor_role,
        }.items() if v is not None}
    )


def clear_request_context() -> None:
    """
    Clear all request-scoped context variables.
    Call at the end of each request (e.g., in a finally block or middleware).
    """
    structlog.contextvars.clear_contextvars()


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """
    Convenience wrapper. Prefer module-level usage:
        log = structlog.get_logger()  ← identical behaviour, less coupling
    """
    return structlog.get_logger(name)