# Hospital Appointment Reminder Voice Agent

An outbound-calling voice bot built on [Pipecat](https://github.com/pipecat-ai/pipecat) that
calls patients, reminds them of an upcoming appointment, and asks them to confirm, reschedule,
or cancel - in Hindi or English, detected automatically.

Adapted from Pipecat's official [Twilio outbound-call
example](https://github.com/pipecat-ai/pipecat-examples/tree/main/twilio-chatbot).

## Stack

| Stage | Service | Why |
|---|---|---|
| Telephony | [Twilio](https://twilio.com) | Dials the patient, streams call audio to us |
| Speech-to-text | [Deepgram](https://deepgram.com) (Nova-3, `multi` mode) | Free tier; code-switches between Hindi and English in real time |
| LLM | [Groq](https://groq.com) (Llama 3.3 70B) | Genuine ongoing free tier, very fast |
| Text-to-speech | [Sarvam AI](https://sarvam.ai) (`bulbul:v2`) | Built specifically for Hindi/Indian-language speech; real speaker voices |

**A note on "free":** Deepgram and Groq have real, ongoing free tiers used as-is here (see the
[Costs & limits](#costs--limits) section for exact numbers). We originally tried ElevenLabs for
TTS, but discovered its free tier blocks *all* API access to voices (confirmed by testing, not
just reading docs) - the web app is free, the API isn't, even on a $0 plan. Sarvam AI replaced it;
check their free-tier terms yourself at signup, since this project hasn't been running long enough
to confirm their exact limits. Twilio is the one guaranteed non-free piece - making an actual phone
call over the real telephone network always costs something (a small amount to rent a phone
number, plus a per-minute rate), though new accounts get ~$15 in trial credit (no card required to
start) that's enough to test this project. We originally tried Plivo here too, but its signup flow
rejected personal Gmail addresses for this account, so we switched to Twilio, which accepts them.

## Files

- **`patients.json`** - your patient list: name, phone number, doctor, appointment date/time.
  Stand-in for a real patient database. Edit this with real (or your own, for testing) phone
  numbers.
- **`call_log.py`** - one function, `log_response()`, that appends a row to
  `logs/call_log.csv` every time a patient confirms, reschedules, or cancels. Swap this for a
  real database later without touching anything else.
- **`bot.py`** - the actual conversation. For one phone call, it wires together:
  `Twilio audio in -> Deepgram (speech-to-text) -> language router -> conversation context ->
  Groq LLM -> Sarvam (text-to-speech) -> Twilio audio out`. It builds a per-patient system prompt
  (containing the consent line and the appointment details), gives the LLM one tool -
  `log_patient_response(status)` - and hangs up a few seconds after that tool is called.
- **`language_router.py`** - a small custom Pipecat processor. Unlike ElevenLabs' multilingual
  model (which auto-detects language from text), Sarvam's TTS needs to be told explicitly which
  language to speak. This watches Deepgram's per-utterance detected language go by and retunes
  the TTS service whenever the patient switches between Hindi and English.
- **`server.py`** - the webhook server Twilio talks to. `POST /start` looks up a patient and asks
  Twilio to dial them; `GET /answer` is what Twilio fetches once the patient picks up (it returns
  TwiML telling Twilio to open a WebSocket back to us); `WS /ws` is where the call's actual audio
  streams in both directions, and where `bot.py`'s pipeline gets started.
- **`send_reminders.py`** - a CLI script that loops over `patients.json` and calls `POST /start`
  for each one, so you can send a whole day's reminders with one command.
- **`.env.example`** - template for all API keys and config. Copy to `.env` and fill in; `.env`
  is gitignored.
- **`dashboard.html`** - a small web dashboard: trigger calls with a button (instead of curl) and
  browse `call_log.csv` results in a table. Served at `/` by `server.py`, protected by HTTP Basic
  auth (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` in `.env`) since it can trigger real, billed
  phone calls.

## How a call actually flows

```
send_reminders.py  --POST /start-->  server.py  --REST API-->  Twilio dials the patient
                                                                        |
                                          patient picks up             |
                                                                        v
                                     server.py <--GET /answer-- Twilio
                                          |
                                          | returns TwiML <Connect><Stream> pointing at wss://.../ws
                                          v
                                     Twilio opens a WebSocket --> server.py's /ws
                                          |
                                          v
                                     bot.py's pipeline runs for the rest of the call
```

## Setup

1. **Get API keys** (all links above).
2. **Buy a Twilio phone number** that supports voice, from the
   [Twilio console](https://console.twilio.com/us1/develop/phone-numbers/manage/search).
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in every key.
5. Edit `patients.json` - at minimum, put your own phone number in one entry so you can test
   safely (`+91XXXXXXXXXX` format).
6. Set `DASHBOARD_PASSWORD` in `.env` to something of your own choosing (any value - it just
   needs to match between the server and anything calling `/start`, including
   `send_reminders.py` and the dashboard login prompt).

## Running it

1. Start the server:
   ```bash
   python server.py
   ```
2. In a second terminal, expose it to the internet with [ngrok](https://ngrok.com) (Twilio needs
   a public URL to reach you - it can't call `localhost`):
   ```bash
   ngrok http 7860
   ```
   Copy the `https://....ngrok-free.app` URL it prints.
3. Put that URL in `.env` as `SERVER_BASE_URL` (needed by `send_reminders.py`).
4. Trigger a call:
   ```bash
   python send_reminders.py P001
   ```
   or call every patient in the file:
   ```bash
   python send_reminders.py
   ```
   or trigger one directly with curl (always hit the **ngrok** URL, not localhost - the host
   Twilio sees in this request is what gets used to build the callback URL it calls back):
   ```bash
   curl -u admin:your-dashboard-password -X POST https://your-ngrok-url.ngrok-free.app/start \
     -H "Content-Type: application/json" \
     -d '{"patient_id": "P001"}'
   ```
   or open the dashboard in your browser at your ngrok URL (or `http://localhost:7860` for local
   only) and log in with `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` - click "Call now" next to a
   patient instead.
5. Your phone rings. Answer it - you should hear the bilingual consent line, then the
   appointment reminder, then a question about confirming/rescheduling/cancelling.
6. Check `logs/call_log.csv` afterward for the recorded outcome, or refresh the dashboard's
   "Call Log" table.

## Consent and language handling

- The very first thing the bot says, in every call, is a fixed (not LLM-improvised) consent
  line in English and Hindi, built in `bot.py`'s `build_system_instruction()`. The system prompt
  instructs the LLM to say this "verbatim, and nothing else" as its first turn - see
  [Possible improvements](#possible-improvements) for a more bulletproof way to guarantee this.
- Deepgram's `language="multi"` setting transcribes whatever language the patient speaks
  (Hindi/English code-switching) and tags each transcript with the language it detected. The
  system prompt instructs the LLM to always reply in the same language it was just addressed in.
  Unlike ElevenLabs' multilingual model (which auto-detects language from text), Sarvam's TTS
  needs to be told explicitly which language to speak - so `language_router.py` watches the
  detected language on each transcript and pushes a `TTSUpdateSettingsFrame` to retune Sarvam
  whenever it changes.

## Deploying (Render)

Once you're done testing locally with ngrok, deploy to [Render](https://render.com) for a
persistent public URL - no more localhost, no more manual tunnels.

1. Push this repo to GitHub (private is fine).
2. On [dashboard.render.com](https://dashboard.render.com), click **New > Blueprint** and connect
   the repo. Render reads `render.yaml` and configures the service automatically.
3. Render will prompt you to fill in the secret env vars (`TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `DEEPGRAM_API_KEY`, `SARVAM_API_KEY`,
   `GROQ_API_KEY`) - paste them there, not into `render.yaml` or `.env` in git.
4. Deploy. Render gives you a persistent URL like `https://hospital-voice-agent.onrender.com`.
5. Point `send_reminders.py` at that URL instead of your ngrok URL (set `SERVER_BASE_URL` in
   whatever `.env` you run `send_reminders.py` from).

**Free tier tradeoff**: Render's free web services spin down after ~15 minutes of inactivity, with
a ~1 minute cold-start delay on the next request. This project's flow is naturally resilient to
that: `send_reminders.py`'s call to `/start` is itself the request that wakes the server, and it
happens *before* Twilio ever dials the patient - so by the time Twilio calls back to `/answer` and
opens the `/ws` WebSocket, the server is already warm.

## Costs & limits (as of writing)

- **Deepgram**: free tier credit for new accounts; Hindi/English code-switch quality can vary -
  test with real Hinglish phrases before relying on it.
- **Groq**: free tier is rate-limited (roughly 30 requests/min, 1,000 requests/day on
  `llama-3.3-70b-versatile`), not credit-limited - fine for sequential reminder calls, watch the
  daily cap if you scale up call volume.
- **Sarvam AI**: check current free-tier terms at [sarvam.ai](https://sarvam.ai) when you sign
  up - not independently verified for this project.
- **Twilio**: not free - phone number rental + per-minute call charges apply. New accounts get
  ~$15 in trial credit (no card required to sign up), which is enough to buy a number and place
  several test calls.

## Possible improvements

- **Guarantee the consent line word-for-word**: right now the LLM is *instructed* to say the
  consent line verbatim, but an LLM can still paraphrase. For a production/compliance-sensitive
  deployment, speak the consent line directly via a `TTSSpeakFrame` pushed before the LLM ever
  runs, bypassing the LLM for that one line entirely.
- **Real database instead of `patients.json`/`call_log.csv`**: swap in Postgres/SQLite once you
  have more than a handful of patients; `call_log.py` is already isolated so this is a
  single-file change.
- **Retry logic**: no-answer/voicemail detection and automatic retry aren't implemented.
- **Call recording & consent enforcement**: today the bot states that the call *may* be
  recorded, but nothing here actually enables Twilio call recording or halts the call if consent
  is refused - add explicit handling if recording is something you need.
- **India-specific compliance**: outbound calling to patients may be subject to
  TRAI/DND-registry and DPDP Act (data protection) rules in India - this project is a technical
  starting point, not legal compliance; check applicable regulations before calling real
  patients at scale.
