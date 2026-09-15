"""WhatsApp Cloud API sender for the notification function app.

Sends outgoing text messages directly via the Meta Graph API — the same
payload format as the main app's WhatsAppNotifier. This app only SENDS
(inbound /webhook handling stays in the main app), so no webhook URL,
verify token, or signature handling is needed here.
"""
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
GRAPH_API_URL   = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"


def _to_whatsapp_markdown(text: str) -> str:
    """Convert standard **bold** markdown to WhatsApp's own *bold* syntax."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def send_whatsapp(to_number: str, text: str) -> bool:
    """Send a WhatsApp text message. Returns True on HTTP 200."""
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        logger.error("PHONE_NUMBER_ID / ACCESS_TOKEN not configured")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to_number,
        "type":              "text",
        "text":              {"body": _to_whatsapp_markdown(text), "preview_url": False},
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    try:
        response = httpx.post(GRAPH_API_URL, headers=headers, json=payload, timeout=10.0)
    except Exception as exc:
        logger.error("WhatsApp send error for %s: %s", to_number, exc)
        return False

    if response.status_code != 200:
        logger.error("WhatsApp send failed (%s): %s", response.status_code, response.text)
        return False

    # Meta returns 200 even when the message is NOT delivered (recipient not
    # opted in / outside the 24h customer-service window). Log the body so the
    # real status (message_status, wamid) is visible while debugging.
    try:
        body = response.json()
        if body.get("messages"):
            logger.info("WhatsApp accepted for %s: %s", to_number, body)
        elif body.get("error") is None and not body.get("messages"):
            logger.warning("WhatsApp 200 but no messages in response for %s: %s", to_number, body)
    except Exception:
        pass
    return True
