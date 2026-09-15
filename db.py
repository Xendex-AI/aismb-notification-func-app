"""Database access for the appointment-reminder function app.

Reads WAITING tokens whose estimated consultation time falls inside the
reminder window, and records which reminders have already been forwarded so
each appointment is reminded exactly once. Pure polling — no WhatsApp, no LLM.
"""
import os

import psycopg2
import psycopg2.extras

DEDUP_TABLE = "appointment_reminders"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS appointment_reminders (
    hospital_id  TEXT         NOT NULL,
    token_id     UUID         NOT NULL,
    reminded_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (hospital_id, token_id)
)
"""


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def ensure_dedup_table(conn) -> None:
    """Create the reminder bookkeeping table if it does not exist yet."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
    conn.commit()


def fetch_due_appointments(conn, window_minutes: int, lead_minutes: int):
    """Return WAITING tokens whose estimated consultation time is within the
    next `window_minutes` (and at least `lead_minutes` ahead), excluding those
    already reminded.

    Estimated consultation time per token:
        COALESCE(ds.started_at, ds.date + avg_checkin_time)
            + (token_number - 1) * avg_consultation_minutes
    """
    sql = f"""
        WITH queue AS (
            SELECT
                t.token_id,
                t.hospital_id,
                t.token_number,
                t.department,
                p.name                AS patient_name,
                p.relation_to_requester,
                p.requested_by_phone  AS patient_phone,
                d.name                AS doctor_name,
                ds.date,
                COALESCE(ds.started_at, ds.date::timestamp + d.avg_checkin_time)
                    + ((t.token_number - 1) * d.avg_consultation_minutes
                       * INTERVAL '1 minute')          AS est_consultation_at
            FROM tokens t
            JOIN patients p          ON p.patient_id  = t.patient_id
            JOIN doctors d           ON d.doctor_id   = t.doctor_id
            JOIN doctor_sessions ds  ON ds.session_id = t.session_id
            LEFT JOIN {DEDUP_TABLE} r
                   ON r.hospital_id = t.hospital_id AND r.token_id = t.token_id
            WHERE t.status = 'WAITING'
              AND r.token_id IS NULL
        )
        SELECT *
        FROM queue
        WHERE est_consultation_at BETWEEN
                  NOW() + (%s * INTERVAL '1 minute')
              AND NOW() + (%s * INTERVAL '1 minute')
        ORDER BY est_consultation_at ASC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (lead_minutes, window_minutes))
        return cur.fetchall()


def mark_reminded(conn, hospital_id: str, token_id: str) -> None:
    """Record a reminder as forwarded (idempotent on conflict)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {DEDUP_TABLE} (hospital_id, token_id)
            VALUES (%s, %s)
            ON CONFLICT (hospital_id, token_id) DO NOTHING
            """,
            (str(hospital_id), str(token_id)),
        )
    conn.commit()
