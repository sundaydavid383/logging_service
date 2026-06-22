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
    Send an email via SMTP with an automatic Port 465 SSL fallback if the primary connection fails.

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

    server = None
    fallback_triggered = False

    try:
        # --- PRIMARY ATTEMPT ---
        try:
            log.debug("email_connect_attempt", host=settings.smtp_host, port=settings.smtp_port)
            
            if settings.smtp_use_tls:
                server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
                server.starttls()
            else:
                server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
                
        except OSError as primary_exc:
            # If primary port (e.g., 587) is blocked, trigger Port 465 Fallback
            log.warning(
                "email_primary_connection_failed_trying_fallback",
                host=settings.smtp_host,
                port=settings.smtp_port,
                fallback_port=465,
                error=str(primary_exc)
            )
            fallback_triggered = True
            # Port 465 requires SMTP_SSL instead of standard SMTP
            server = smtplib.SMTP_SSL(settings.smtp_host, 465, timeout=20)

        # --- AUTHENTICATION & DELIVERY ---
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
            fallback_used=fallback_triggered
        )
        return {"provider_message_id": message_id, "status": "SENT"}

    except smtplib.SMTPException as exc:
        log.error("email_send_failed", to=to, subject=subject, error=str(exc))
        raise EmailSendError(f"SMTP send failed: {exc}") from exc

    except OSError as exc:
        # Connection-level errors (if even the fallback connection fails)
        log.error("email_connection_failed", host=settings.smtp_host,
                  port=465 if fallback_triggered else settings.smtp_port, error=str(exc))
        raise EmailSendError(f"SMTP connection completely failed: {exc}") from exc