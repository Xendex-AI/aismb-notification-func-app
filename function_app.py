"""Azure Function — appointment reminders (standalone notification app).

A timer-triggered poller: every 30 minutes it queries Postgres for WAITING
appointments whose estimated consultation time falls within the next two
hours, then sends each patient a WhatsApp reminder DIRECTLY via the Meta
Graph API. Fully decomposed from the main WhatsApp app — this app only
sends outbound notifications; all inbound /webhook handling stays in the
main app. Each appointment is reminded exactly once (dedup via the
appointment_reminders table).

Environment variables (App Settings on the Function App):
    DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD   — Postgres
    PHONE_NUMBER_ID / ACCESS_TOKEN                        — WhatsApp Cloud API (send-only)
    REMINDER_WINDOW_MINUTES (default 120)                 — look-ahead window
    REMINDER_LEAD_MINUTES  (default 30)                   — don't remind sooner
"""
import logging
import os
from datetime import datetime

import azure.functions as func
import httpx

from db import ensure_dedup_table, fetch_due_appointments, get_connection, mark_reminded
from whatsapp_client import send_whatsapp

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

logger = logging.getLogger(__name__)


def _format_reminder(row: dict) -> str:
    """Compose the WhatsApp reminder body for one appointment row."""
    when = row["est_consultation_at"]
    if isinstance(when, datetime):
        when_str = when.strftime("%I:%M %p").lstrip("0")
    else:
        when_str = str(when)

    patient  = row.get("patient_name") or "the patient"
    relation = (row.get("relation_to_requester") or "").strip().lower()
    who = f"{patient} ({relation})" if relation and relation != "self" else patient

    lines = [
        "*Appointment Reminder*",
        "",
        f"Hello! This is a reminder for {who}'s appointment.",
        f"• Doctor: {row.get('doctor_name')}",
        f"• Department: {row.get('department') or '—'}",
        f"• Date: {row.get('date')}",
        f"• Token number: {row.get('token_number')}",
        f"• Estimated consultation time: ~{when_str}",
        "",
        "Please arrive 10–15 minutes early. Reply here if you need to cancel or reschedule.",
    ]
    return "\n".join(lines)


@app.route(route="test-send", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def test_send(req: func.HttpRequest) -> func.HttpResponse:
    """TEST ONLY — send a WhatsApp message to a real number.

    Body: {"to": "<phone>", "text": "<optional message>"}
    No auth required (anonymous) for easy testing.
    Remove this endpoint before production use.
    """
    try:
        payload = req.get_json()
    except Exception:
        return func.HttpResponse('{"error": "invalid json"}', status_code=400, mimetype="application/json")

    to_number = (payload.get("to") or "").strip()
    text      = payload.get("text") or "🧪 Test message from the notification function app — WhatsApp send path is working."

    if not to_number:
        return func.HttpResponse('{"error": "missing \'to\'"}', status_code=400, mimetype="application/json")

    ok = send_whatsapp(to_number, text)
    if ok:
        return func.HttpResponse(
            f'{{"status": "sent", "to": "{to_number}"}}',
            status_code=200, mimetype="application/json",
        )
    return func.HttpResponse(
        '{"error": "send failed — check PHONE_NUMBER_ID / ACCESS_TOKEN and the number format (e.g. 91XXXXXXXXXX)"}',
        status_code=502, mimetype="application/json",
    )


@app.timer_trigger(
    schedule="0 */30 * * * *",
    arg_name="mytimer",
    run_on_startup=os.getenv("REMINDER_RUN_ON_STARTUP", "").lower() in ("1", "true", "yes", "on"),
    use_monitor=False,
)
def appointment_reminder(mytimer: func.TimerRequest) -> None:
    """Every 30 minutes: poll DB and send due WhatsApp reminders directly."""
    window_minutes = int(os.getenv("REMINDER_WINDOW_MINUTES", "120"))
    lead_minutes   = int(os.getenv("REMINDER_LEAD_MINUTES", "30"))

    logger.info(
        "Reminder timer fired (past_due=%s, window=%dm, lead=%dm)",
        mytimer.past_due, window_minutes, lead_minutes,
    )

    conn = get_connection()
    try:
        ensure_dedup_table(conn)
        due = fetch_due_appointments(conn, window_minutes, lead_minutes)
        logger.info("Found %d appointment(s) due for reminder", len(due))

        sent = failed = skipped = 0
        for row in due:
            phone = (row.get("patient_phone") or "").strip()
            if not phone:
                skipped += 1
                continue

            if send_whatsapp(phone, _format_reminder(row)):
                mark_reminded(conn, row["hospital_id"], row["token_id"])
                sent += 1
            else:
                failed += 1

        logger.info(
            "Reminders complete — sent=%d failed=%d skipped(no phone)=%d",
            sent, failed, skipped,
        )
    finally:
        conn.close()
