"""One-off script: verify API keys in .env are valid. Not part of the app - delete anytime."""

import os

import requests
from dotenv import load_dotenv

load_dotenv()


def check_groq():
    key = os.getenv("GROQ_API_KEY")
    r = requests.get(
        "https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}
    )
    print(f"Groq:       {'OK' if r.status_code == 200 else f'FAIL ({r.status_code}) {r.text[:200]}'}")


def check_deepgram():
    key = os.getenv("DEEPGRAM_API_KEY")
    r = requests.get(
        "https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {key}"}
    )
    print(f"Deepgram:   {'OK' if r.status_code == 200 else f'FAIL ({r.status_code}) {r.text[:200]}'}")


def check_sarvam():
    key = os.getenv("SARVAM_API_KEY")
    if not key:
        print("Sarvam:     SKIP (SARVAM_API_KEY not set yet)")
        return
    r = requests.post(
        "https://api.sarvam.ai/text-to-speech",
        headers={"api-subscription-key": key, "Content-Type": "application/json"},
        json={
            "text": "test",
            "target_language_code": "en-IN",
            "speaker": os.getenv("SARVAM_VOICE", "anushka"),
            "model": "bulbul:v2",
        },
    )
    print(f"Sarvam:     {'OK' if r.status_code == 200 else f'FAIL ({r.status_code}) {r.text[:200]}'}")


def check_twilio():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        print("Twilio:     SKIP (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set yet)")
        return
    r = requests.get(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json",
        auth=(account_sid, auth_token),
    )
    print(f"Twilio:     {'OK' if r.status_code == 200 else f'FAIL ({r.status_code}) {r.text[:200]}'}")


if __name__ == "__main__":
    check_groq()
    check_deepgram()
    check_sarvam()
    check_twilio()
