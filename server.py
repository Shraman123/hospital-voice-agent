"""server.py - webhook server that dials patients via Twilio and hands the
call's audio stream off to bot.py.

Three endpoints, matching Twilio's outbound-call flow:

    POST /start   -> we call Twilio's REST API to dial the patient
    GET  /answer  -> Twilio fetches this once the patient picks up; we return
                      TwiML telling Twilio to open a WebSocket back to us
    WS   /ws      -> Twilio streams call audio here; we start bot.py's pipeline

Adapted from Pipecat's official Twilio outbound-call example pattern:
https://github.com/pipecat-ai/pipecat-examples/tree/main/twilio-chatbot
"""

import csv
import json
import os
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

load_dotenv(override=True)

PATIENTS_FILE = Path(__file__).parent / "patients.json"
CALL_LOG_FILE = Path(__file__).parent / "logs" / "call_log.csv"
DASHBOARD_HTML_FILE = Path(__file__).parent / "dashboard.html"

security = HTTPBasic()


def require_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    """Protects the dashboard and /start - anyone with this password can trigger
    real, billed phone calls, so this must not be left open once deployed.
    Twilio's own callbacks (/answer, /ws) are deliberately NOT behind this,
    since Twilio can't do interactive HTTP Basic auth.
    """
    expected_user = os.getenv("DASHBOARD_USERNAME", "admin")
    expected_pass = os.getenv("DASHBOARD_PASSWORD")
    if not expected_pass:
        raise HTTPException(
            status_code=500, detail="DASHBOARD_PASSWORD is not set in .env"
        )
    valid_user = secrets.compare_digest(credentials.username, expected_user)
    valid_pass = secrets.compare_digest(credentials.password, expected_pass)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def load_patients() -> list[dict]:
    with open(PATIENTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_call_log() -> list[dict]:
    if not CALL_LOG_FILE.exists():
        return []
    with open(CALL_LOG_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))


async def make_twilio_call(
    session: aiohttp.ClientSession, to_number: str, from_number: str, answer_url: str
):
    """Dial a phone number using Twilio's REST API."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        raise ValueError("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in .env")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
    # Twilio's Calls API is form-urlencoded, not JSON, and capitalizes param names.
    data = {
        "To": to_number,
        "From": from_number,
        "Url": answer_url,
        "Method": "GET",
    }
    auth = aiohttp.BasicAuth(account_sid, auth_token)

    async with session.post(url, data=data, auth=auth) as response:
        if response.status != 201:
            error_text = await response.text()
            raise Exception(f"Twilio API error ({response.status}): {error_text}")
        return await response.json()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.session = aiohttp.ClientSession()
    yield
    await app.state.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def get_dashboard() -> FileResponse:
    """Simple dashboard: trigger calls and browse past results.

    The page itself is a static shell with no sensitive data - it's left
    unauthenticated so it always loads. The actual protection is on the
    /api/* and /start calls the page's JS makes, which it authenticates
    explicitly (see dashboard.html's apiFetch()) rather than relying on the
    browser to reattach cached HTTP Basic Auth credentials to fetch() calls,
    which proved unreliable in testing.
    """
    return FileResponse(DASHBOARD_HTML_FILE)


@app.get("/api/patients")
async def get_patients(_: None = Depends(require_dashboard_auth)) -> JSONResponse:
    return JSONResponse(load_patients())


@app.get("/api/call-log")
async def get_call_log(_: None = Depends(require_dashboard_auth)) -> JSONResponse:
    return JSONResponse(load_call_log())


@app.post("/start")
async def initiate_reminder_call(
    request: Request, _: None = Depends(require_dashboard_auth)
) -> JSONResponse:
    """Trigger an outbound reminder call for a patient in patients.json.

    Body: {"patient_id": "P001"}
    """
    data = await request.json()
    patient_id = data.get("patient_id")
    if not patient_id:
        raise HTTPException(status_code=400, detail="Missing 'patient_id' in request body")

    patient = next(
        (p for p in load_patients() if p["patient_id"] == patient_id), None
    )
    if not patient:
        raise HTTPException(status_code=404, detail=f"No patient with patient_id '{patient_id}'")

    host = request.headers.get("host")
    if not host:
        raise HTTPException(status_code=400, detail="Unable to determine server host")

    protocol = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    body_encoded = urllib.parse.quote(json.dumps(patient))
    answer_url = f"{protocol}://{host}/answer?body_data={body_encoded}"

    try:
        call_result = await make_twilio_call(
            session=request.app.state.session,
            to_number=patient["phone_number"],
            from_number=os.getenv("TWILIO_PHONE_NUMBER"),
            answer_url=answer_url,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initiate call: {e}")

    call_uuid = call_result.get("sid", "unknown")

    return JSONResponse(
        {"call_uuid": call_uuid, "status": "call_initiated", "patient_id": patient_id}
    )


@app.get("/answer")
async def get_answer_xml(
    request: Request,
    body_data: str = Query(None, description="JSON-encoded patient data"),
) -> HTMLResponse:
    """Twilio calls this once the patient answers. Tells Twilio to stream audio to /ws."""
    host = request.headers.get("host")
    if not host:
        raise HTTPException(status_code=400, detail="Unable to determine server host")

    ws_url = f"wss://{host}/ws"

    # Twilio silently drops query strings on the Stream URL, unlike Plivo - it has
    # its own mechanism for passing custom data through instead: <Parameter>
    # elements, delivered inside the WebSocket's first "start" message as
    # customParameters. body_data is already valid JSON text (just URL-decoded by
    # FastAPI), so we pass it through as one parameter's value, XML-escaped.
    param_xml = ""
    if body_data:
        escaped_json = xml_escape(body_data, {'"': "&quot;"})
        param_xml = f'\n            <Parameter name="patient_json" value="{escaped_json}" />'

    # Twilio's <Connect><Stream> is bidirectional by default (no extra attributes
    # needed, unlike Plivo) and always sends/expects 8kHz mu-law audio.
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">{param_xml}
        </Stream>
    </Connect>
</Response>"""
    return HTMLResponse(content=xml_content, media_type="application/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Twilio's Media Stream connects here; we hand it off to bot.py.

    Unlike Plivo, the patient data doesn't arrive as a URL query param - it
    comes through inside the WebSocket protocol itself (Twilio's
    customParameters), which bot.py reads off runner_args.call_data.body after
    create_transport() parses the connection.
    """
    await websocket.accept()

    from pipecat.runner.types import WebSocketRunnerArguments

    from bot import bot

    try:
        runner_args = WebSocketRunnerArguments(websocket=websocket)
        await bot(runner_args)
    except Exception as e:
        print(f"Error in WebSocket endpoint: {e}")
        await websocket.close()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
