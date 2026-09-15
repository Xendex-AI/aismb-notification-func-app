"""Database access for the notification function app.

Two reminder types, both using pre-existing tables (no new tables):

1. Slot-based appointments:
   Data:  appointments + slots + patients + doctors
   Dedup: appointment_reminder_records  (patient_id + reminder_date)

2. Follow-up reminders:
   Data:  followup_offers + patients + doctors
   Dedup: post_visit_reminder_records   (patient_id + encounter_id)
"""
import os

import psycopg2
import psycopg2.extras


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


# ---------------------------------------------------------------------------
# Slot-based appointment reminders
# ---------------------------------------------------------------------------

_SLOT_QUERY = """
    SELECT
        a.appointment_id,
        a.hospital_id,
        a.department,
        a.appointment_date,
        s.start_time,
        s.end_time,
        p.name                AS patient_name,
        p.phone               AS patient_phone,
        p.requested_by_phone  AS requester_phone,
        p.relation_to_requester,
        d.name                AS doctor_name,
        CASE
            WHEN p.relation_to_requester = 'self' THEN p.phone
            ELSE p.requested_by_phone
        END AS send_to_phone
    FROM appointments a
    JOIN slots s     ON s.slot_id     = a.slot_id
    JOIN patients p  ON p.patient_id  = a.patient_id
    JOIN doctors d   ON d.doctor_id   = a.doctor_id
                  AND d.hospital_id  = a.hospital_id
    WHERE a.status = 'SCHEDULED'
      AND a.appointment_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '1 day'
      AND NOT EXISTS (
          SELECT 1
          FROM appointment_reminder_records r
          WHERE r.patient_id    = a.patient_id
            AND r.reminder_date = a.appointment_date
            AND r.status        = 'SENT'
      )
    ORDER BY a.appointment_date, s.start_time
"""


def fetch_slot_reminders(conn):
    """Return SCHEDULED slot-based appointments needing a reminder today/tomorrow."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(_SLOT_QUERY)
        rows = cur.fetchall()
    _normalize_phones(rows)
    return rows


def mark_slot_reminded(conn, patient_id, reminder_date) -> None:
    """Record that a slot-based reminder was sent (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO appointment_reminder_records
                (reminder_id, hospital_id, token_id, patient_id,
                 reminder_date, triggered_at, status, fired_at)
            VALUES (
                gen_random_uuid(), '', '00000000-0000-0000-0000-000000000000',
                %s, %s, NOW(), 'SENT', NOW()
            )
            ON CONFLICT DO NOTHING
            """,
            (str(patient_id), reminder_date),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Follow-up reminders
# ---------------------------------------------------------------------------

_FOLLOWUP_QUERY = """
    SELECT
        f.offer_id,
        f.hospital_id,
        f.department,
        f.patient_id,
        p.name                AS patient_name,
        p.phone               AS patient_phone,
        p.requested_by_phone  AS requester_phone,
        p.relation_to_requester,
        d.name                AS doctor_name,
        CASE
            WHEN p.relation_to_requester = 'self' THEN p.phone
            ELSE p.requested_by_phone
        END AS send_to_phone
    FROM followup_offers f
    JOIN patients p  ON p.patient_id  = f.patient_id
    JOIN doctors d   ON d.doctor_id   = f.doctor_id
                  AND d.hospital_id  = f.hospital_id
    WHERE f.status = 'PENDING'
      AND NOT EXISTS (
          SELECT 1
          FROM post_visit_reminder_records r
          WHERE r.patient_id    = f.patient_id
            AND r.encounter_id  = f.encounter_id
            AND r.status        = 'SENT'
      )
    ORDER BY f.created_at
"""


def fetch_followup_reminders(conn):
    """Return PENDING follow-up offers needing a reminder."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(_FOLLOWUP_QUERY)
        rows = cur.fetchall()
    _normalize_phones(rows)
    return rows


def mark_followup_reminded(conn, patient_id, encounter_id) -> None:
    """Record that a follow-up reminder was sent (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO post_visit_reminder_records
                (reminder_id, encounter_id, patient_id, hospital_id,
                 triggered_at, status, fired_at)
            VALUES (
                gen_random_uuid(), %s, %s, '', NOW(), 'SENT', NOW()
            )
            ON CONFLICT DO NOTHING
            """,
            (str(encounter_id), str(patient_id)),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_phones(rows) -> None:
    """Ensure every send_to_phone is 12-digit (91XXXXXXXXXX)."""
    for row in rows:
        phone = (row.get("send_to_phone") or "").strip()
        if len(phone) == 10:
            phone = "91" + phone
        row["send_to_phone_normalized"] = phone
