"""Append-only CSV log of how each reminder call ended.

Kept deliberately dumb (flat file, no database) so it's easy to open in Excel
or read with pandas. Swap this module out for a real database write later
without touching bot.py - it only calls log_response().
"""

import csv
import os
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "call_log.csv")
FIELDNAMES = ["timestamp_utc", "patient_id", "patient_name", "phone_number", "status"]


def log_response(patient: dict, status: str) -> None:
    """Record a patient's confirm/reschedule/cancel decision."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    is_new_file = not os.path.exists(LOG_PATH)

    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new_file:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "patient_id": patient.get("patient_id", ""),
                "patient_name": patient.get("name", ""),
                "phone_number": patient.get("phone_number", ""),
                "status": status,
            }
        )
