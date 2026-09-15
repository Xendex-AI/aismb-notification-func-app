"""WhatsApp Cloud API sender for the notification function app.

Two send modes:
1. send_whatsapp()         — free-form text (test endpoint only)
2. send_whatsapp_template() — pre-approved template (production reminders)

This app only SENDS; inbound /webhook handling stays in the main app.
"""
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
GRAPH_API_URL   = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

_HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def _check_config() -> bool:
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        logger.error("PHONE_NUMBER_ID / ACCESS_TOKEN not configured")
        return False
    return True


def _post(payload: dict) -> bool:
    """Send a payload to the Graph API. Returns True on HTTP 200."""
    if not _check_config():
        return False
    try:
        response = httpx.post(GRAPH_API_URL, headers=_HEADERS, json=payload, timeout=10.0)
    except Exception as exc:
        logger.error("WhatsApp API error: %s", exc)
        return False

    if response.status_code != 200:
        logger.error("WhatsApp API failed (%s): %s", response.status_code, response.text)
        return False

    try:
        body = response.json()
        if body.get("messages"):
            logger.info("WhatsApp accepted: %s", body)
        elif body.get("error"):
            logger.warning("WhatsApp 200 but error in body: %s", body)
    except Exception:
        pass
    return True


# -----------------------------------------------------------------------
# Free-form text (test endpoint only — won't work outside 24h window)
# -----------------------------------------------------------------------

def _to_whatsapp_markdown(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def send_whatsapp(to_number: str, text: str) -> bool:
    """Send a free-form WhatsApp text message."""
    return _post({
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to_number,
        "type":              "text",
        "text":              {"body": _to_whatsapp_markdown(text), "preview_url": False},
    })


# -----------------------------------------------------------------------
# Template messages (production reminders — works outside 24h window)
# -----------------------------------------------------------------------

def send_whatsapp_template(to_number: str, template_name: str,
                           language_code: str, parameters: list[str]) -> bool:
    """Send a pre-approved WhatsApp template message.

    parameters: ordered list of template variable values
                ["Ramadevi", "Dr. Smith", "ORTHOPAEDICS", "10 Sep 2026", "10:30 AM"]
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to_number,
        "type":              "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": [{
                "type":       "body",
                "parameters": [{"type": "text", "text": v} for v in parameters],
            }],
        },
    }
    return _post(payload)
