"""
fdq_commons/notifications/teams_sender.py
--------------------------------------------
MS Teams delivery via Incoming Webhooks (spec §6.3.3).

"Webhook URLs are stored in configuration, never in the request body —
callers reference a channel_key (e.g., 'dq-alerts', 'etl-ops') and the
service resolves the URL internally."

This module is the ONLY place that knows how Teams messages get sent.
If the provider changes (e.g., Teams Workflows replacing Incoming
Webhooks), only this file changes.

Function signature is the contract:
    send_teams_message(channel_key, title, summary, facts, severity) -> dict

Usage:
    from fdq_commons.notifications.teams_sender import send_teams_message

    result = send_teams_message(
        channel_key="dq-alerts",
        title="Scan Job Completed",
        summary="Scan SCAN-009 finished with 42 issues found.",
        facts=[{"name": "Scan ID", "value": "SCAN-009"}],
        severity="WARNING",
    )
    # result = {"status": "SENT"}
"""
from __future__ import annotations

import requests
import structlog

from fdq_commons.config import settings

log = structlog.get_logger()


class TeamsSendError(Exception):
    """Raised when the Teams webhook call fails. Caller handles retry."""


# Severity -> Adaptive Card accent colour (spec §6.3.3 severity enum)
_SEVERITY_COLORS = {
    "CRITICAL": "Attention",
    "WARNING":  "Warning",
    "INFO":     "Good",
}


def send_teams_message(
    channel_key: str,
    title: str,
    summary: str,
    facts: list[dict[str, str]] | None = None,
    severity: str = "INFO",
) -> dict:
    """
    Post an Adaptive Card to a Teams channel via Incoming Webhook.

    Args:
        channel_key: key into TEAMS_CHANNEL_WEBHOOKS_RAW (e.g. "dq-alerts").
                     The webhook URL is resolved from settings — never
                     passed in by the caller.
        title:       card title
        summary:     short description / body text
        facts:       list of {"name": ..., "value": ...} key-value pairs
        severity:    CRITICAL | WARNING | INFO — controls accent colour

    Returns:
        {"status": "SENT"}

    Raises:
        TeamsSendError: if channel_key is unknown or the webhook call fails.
    """
    webhook_url = settings.teams_channel_webhooks.get(channel_key)
    if not webhook_url:
        raise TeamsSendError(
            f"No webhook configured for channel_key '{channel_key}'. "
            "Add it to TEAMS_CHANNEL_WEBHOOKS_RAW in .env."
        )

    severity = severity.upper()
    color = _SEVERITY_COLORS.get(severity, "Default")

    fact_set = [
        {"title": f.get("name", ""), "value": f.get("value", "")}
        for f in (facts or [])
    ]

    # MS Teams Adaptive Card via Incoming Webhook
    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": title,
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": color,
                        },
                        {
                            "type": "TextBlock",
                            "text": summary,
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": fact_set,
                        } if fact_set else None,
                        {
                            "type": "TextBlock",
                            "text": f"Severity: {severity}",
                            "isSubtle": True,
                            "size": "Small",
                        },
                    ],
                },
            }
        ],
    }

    # Remove the FactSet block entirely if there are no facts
    card["attachments"][0]["content"]["body"] = [
        block for block in card["attachments"][0]["content"]["body"] if block is not None
    ]

    try:
        response = requests.post(webhook_url, json=card, timeout=15)
        response.raise_for_status()

        log.info(
            "teams_message_sent",
            channel_key=channel_key,
            title=title,
            severity=severity,
            status_code=response.status_code,
        )
        return {"status": "SENT"}

    except requests.RequestException as exc:
        log.error(
            "teams_send_failed",
            channel_key=channel_key,
            title=title,
            error=str(exc),
        )
        raise TeamsSendError(f"Teams webhook call failed: {exc}") from exc