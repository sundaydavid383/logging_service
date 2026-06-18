"""
fdq_commons/notifications/email_sender.py
--------------------------------------------
Email delivery via raw SMTP (spec §1.3: "On-premise SMTP relay, no SaaS
dependency"). Uses Python's built-in smtplib — no third-party email API.

This module is the ONLY place that knows how email gets sent. If the
provider ever changes (different SMTP relay, different auth method),
only this file changes. Callers (Celery tasks, routes) never change.

Function signature is the contract:
    send_email(to, subject, html_body, text_body=None, cc=None) -> dict

Usage:
    from fdq_commons.notifications.email_sender import send_email

    result = send_email(
        to=["user@bank.ng"],
        subject="FDQ | Scan Job Completed",
        html_body="<h2>Scan complete</h2>...",
        text_body="Scan complete...",
    )
    # result = {"provider_message_id": "<...>", "status": "SENT"}
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

import structlog

from fdq_commons.config import settings

log = structlog.get_logger()


class EmailSendError(Exception):
    """Raised when SMTP send fails. Caller (Celery task) handles retry."""


def send_email(
    to: list[str],
    subject: str,
    html_body: str,
    text_body: str | None = None,
    cc: list[str] | None = None,
) -> dict:
    """
    Send an email via SMTP.

    Args:
        to:        list of recipient email addresses
        subject:   email subject line
        html_body: HTML content
        text_body: plain-text fallback (auto-generated if not provided)
        cc:        optional CC recipients

    Returns:
        {"provider_message_id": str, "status": "SENT"}

    Raises:
        EmailSendError: on any SMTP failure — caller decides retry policy.
    """
    if not to:
        raise EmailSendError("At least one recipient ('to') is required.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_address
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)

    # Message-ID — becomes the provider_message_id stored in notification_logs
    message_id = make_msgid(domain="fdq.internal")
    msg["Message-ID"] = message_id

    # Plain-text fallback — strip tags crudely if not provided
    if text_body is None:
        import re
        text_body = re.sub(r"<[^>]+>", "", html_body)

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    all_recipients = list(to) + (cc or [])

    try:
        if settings.smtp_use_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            server.starttls()
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)

        try:
            if settings.smtp_username and settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password)

            server.send_message(msg, to_addrs=all_recipients)
        finally:
            server.quit()

        log.info(
            "email_sent",
            to=to,
            subject=subject,
            provider_message_id=message_id,
        )
        return {"provider_message_id": message_id, "status": "SENT"}

    except smtplib.SMTPException as exc:
        log.error("email_send_failed", to=to, subject=subject, error=str(exc))
        raise EmailSendError(f"SMTP send failed: {exc}") from exc

    except OSError as exc:
        # Connection-level errors (DNS, timeout, refused)
        log.error("email_connection_failed", host=settings.smtp_host,
                  port=settings.smtp_port, error=str(exc))
        raise EmailSendError(f"SMTP connection failed: {exc}") from exc