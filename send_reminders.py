"""send_reminders.py - trigger reminder calls for every patient in patients.json.

Run with server.py already running and exposed via ngrok:

    python send_reminders.py            # calls every patient
    python send_reminders.py P002       # calls only patient P002

This just POSTs to server.py's own /start endpoint - it doesn't talk to
Twilio directly.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

PATIENTS_FILE = Path(__file__).parent / "patients.json"
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://localhost:7860")

# Seconds to wait between dialing each patient, so calls don't overlap.
DELAY_BETWEEN_CALLS_SECS = 3


def main():
    with open(PATIENTS_FILE, encoding="utf-8") as f:
        patients = json.load(f)

    only_patient_id = sys.argv[1] if len(sys.argv) > 1 else None

    for patient in patients:
        if only_patient_id and patient["patient_id"] != only_patient_id:
            continue

        print(f"Calling {patient['name']} ({patient['phone_number']})...")
        response = requests.post(
            f"{SERVER_BASE_URL}/start", json={"patient_id": patient["patient_id"]}
        )
        print(f"  -> {response.status_code} {response.json()}")
        time.sleep(DELAY_BETWEEN_CALLS_SECS)


if __name__ == "__main__":
    main()
