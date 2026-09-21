"""Azure Function — appointment + follow-up reminders (standalone).

Two timer triggers (one shared 30-min schedule):

1. Slot-based appointment reminders:
   Queries appointments + slots, sends appointment_reminder template.

2. Follow-up reminders:
   Queries followup_offers, sends followup_reminder template.

All sending is via the Meta WhatsApp Graph API template endpoint — works
outside the 24-hour customer-service window (requires approved templates).

Environment variables (App Settings):
    DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD   — Postgres
    PHONE_NUMBER_ID / ACCESS_TOKEN                        — WhatsApp Cloud API (send-only)
    REMINDER_RUN_ON_STARTUP (true/false)                  — fire once on cold start
"""
import logging
import os

import azure.functions as func

from db import (
    get_connection,
    fetch_slot_reminders,  mark_slot_reminded,
    fetch_followup_reminders, mark_followup_reminded,
)
from whatsapp_client import send_whatsapp, send_whatsapp_template

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

logger = logging.getLogger(__name__)

SLOT_TEMPLATE     = os.getenv("SLOT_TEMPLATE_NAME", "appointment_reminder")
FOLLOWUP_TEMPLATE = os.getenv("FOLLOWUP_TEMPLATE_NAME", "follow_up_reminder")
TEMPLATE_LANG     = os.getenv("TEMPLATE_LANG", "en_US")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(d) -> str:
    """10 September 2026"""
    if hasattr(d, "strftime"):
        return d.strftime("%d %B %Y")
    return str(d)


def _fmt_time(t) -> str:
    """10:30 AM"""
    if hasattr(t, "strftime"):
        return t.strftime("%I:%M %p").lstrip("0")
    return str(t)


def _send_template(phone: str, template: str, params: list[str],
                   label: str, row: dict) -> bool:
    """Send a template and log the outcome."""
    if not phone:
        logger.info("Skipping %s — no phone", label)
        return False

    ok = send_whatsapp_template(phone, template, TEMPLATE_LANG, params)
    if ok:
        logger.info("Template %s sent to %s for %s", template, phone, label)
    else:
        logger.error("Template %s FAILED for %s (%s)", template, phone, label)
    return ok


# ---------------------------------------------------------------------------
# Slot-based appointment reminders
# ---------------------------------------------------------------------------

def _process_slot_reminders(conn) -> tuple[int, int, int]:
    """Query + send slot-based reminders. Returns (sent, failed, skipped)."""
    rows = fetch_slot_reminders(conn)
    logger.info("Slot reminders found: %d", len(rows))

    sent = failed = skipped = 0
    for row in rows:
        phone = (row.get("send_to_phone_normalized") or "").strip()
        params = [
            row.get("patient_name") or "the patient",
            row.get("doctor_name") or "",
            row.get("department") or "",
            _fmt_date(row.get("appointment_date")),
            _fmt_time(row.get("start_time")),
        ]

        if _send_template(phone, SLOT_TEMPLATE, params,
                          "slot {}:{}".format(row.get("patient_name"), row.get("appointment_date")),
                          row):
            mark_slot_reminded(conn, row["patient_id"], row["appointment_date"])
            sent += 1
        else:
            failed += 1

    return sent, failed, skipped


# ---------------------------------------------------------------------------
# Follow-up reminders
# ---------------------------------------------------------------------------

def _process_followup_reminders(conn) -> tuple[int, int, int]:
    """Query + send follow-up reminders. Returns (sent, failed, skipped)."""
    rows = fetch_followup_reminders(conn)
    logger.info("Follow-up reminders found: %d", len(rows))

    sent = failed = skipped = 0
    for row in rows:
        phone = (row.get("send_to_phone_normalized") or "").strip()
        params = [
            row.get("patient_name") or "the patient",
            row.get("doctor_name") or "",
            row.get("department") or "",
        ]

        if _send_template(phone, FOLLOWUP_TEMPLATE, params,
                          "followup {}:{}".format(row.get("patient_name"), row.get("offer_id")),
                          row):
            mark_followup_reminded(conn, row["patient_id"], row["encounter_id"])
            sent += 1
        else:
            failed += 1

    return sent, failed, skipped


# ---------------------------------------------------------------------------
# HTTP test endpoint
# ---------------------------------------------------------------------------

@app.route(route="test-send", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def test_send(req: func.HttpRequest) -> func.HttpResponse:
    """TEST ONLY — send a free-form WhatsApp message (text or template).

    Text:    {"to": "91...", "text": "hello"}
    Template: {"to": "91...", "template": "appointment_reminder",
               "params": ["Ramadevi", "Dr. Smith", "ORTHOPAEDICS", "10 Sep 2026", "10:30 AM"]}
    """
    try:
        payload = req.get_json()
    except Exception:
        return func.HttpResponse('{"error": "invalid json"}',
                                  status_code=400, mimetype="application/json")

    to = (payload.get("to") or "").strip()
    if not to:
        return func.HttpResponse('{"error": "missing to"}',
                                  status_code=400, mimetype="application/json")

    # Template mode
    if payload.get("template"):
        ok = send_whatsapp_template(
            to,
            payload["template"],
            payload.get("lang", TEMPLATE_LANG),
            payload.get("params", []),
        )
    else:
        text = payload.get("text") or "Test message from notification func app."
        ok = send_whatsapp(to, text)

    if ok:
        return func.HttpResponse('{"status":"sent","to":"%s"}' % to,
                                  status_code=200, mimetype="application/json")
    return func.HttpResponse('{"error":"send failed"}',
                              status_code=502, mimetype="application/json")


# ---------------------------------------------------------------------------
# Timer trigger (shared 30-min schedule)
# ---------------------------------------------------------------------------

@app.timer_trigger(
    schedule="0 */30 * * * *",
    arg_name="mytimer",
    run_on_startup=os.getenv("REMINDER_RUN_ON_STARTUP", "").lower() in ("1", "true", "yes", "on"),
    use_monitor=False,
)
def reminder_dispatcher(mytimer: func.TimerRequest) -> None:
    """Every 30 minutes: send both slot-based and follow-up reminders."""
    logger.info("Reminder timer fired (past_due=%s)", mytimer.past_due)

    conn = get_connection()
    try:
        s_sent, s_fail, s_skip = _process_slot_reminders(conn)
        f_sent, f_fail, f_skip = _process_followup_reminders(conn)

        logger.info(
            "Done — slot: sent=%d fail=%d skip=%d | followup: sent=%d fail=%d skip=%d",
            s_sent, s_fail, s_skip, f_sent, f_fail, f_skip,
        )
    finally:
        conn.close()
