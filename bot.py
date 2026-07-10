"""bot.py - the Pipecat voice pipeline for one phone call.

This file is invoked once per call (server.py calls bot() for every new
Twilio WebSocket connection). It builds a pipeline:

    caller's audio -> Deepgram (speech-to-text)
                    -> conversation context
                    -> Groq LLM (decides what to say, when to log a result)
                    -> Sarvam AI (text-to-speech)
                    -> caller's audio

and wires up one tool the LLM can call: log_patient_response, which writes
the patient's confirm/reschedule/cancel decision to logs/call_log.csv and
then ends the call a few seconds later.

Adapted from Pipecat's official Twilio outbound-call example pattern:
https://github.com/pipecat-ai/pipecat-examples/tree/main/twilio-chatbot
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

import call_log
from language_router import LanguageRouter

load_dotenv(override=True)

HOSPITAL_NAME = os.getenv("HOSPITAL_NAME", "the hospital")

# Seconds to let the bot's goodbye finish playing before we hang up.
HANGUP_DELAY_SECS = 6


def build_system_instruction(patient: dict) -> str:
    """Build the per-patient system prompt, including a verbatim consent script."""
    name = patient.get("name", "there")
    doctor = patient.get("doctor_name", "your doctor")
    date = patient.get("appointment_date", "your scheduled date")
    time = patient.get("appointment_time", "your scheduled time")

    opening_en = (
        f"Hello, this is an automated call from {HOSPITAL_NAME}. This call may be recorded "
        f"for quality and training purposes. Am I speaking with {name}?"
    )
    opening_hi = (
        f"नमस्ते, यह {HOSPITAL_NAME} की तरफ से एक स्वचालित कॉल है। गुणवत्ता और प्रशिक्षण उद्देश्यों "
        f"के लिए इस कॉल को रिकॉर्ड किया जा सकता है। क्या मेरी बात {name} जी से हो रही है?"
    )

    return f"""You are an automated appointment-reminder assistant calling on behalf of \
{HOSPITAL_NAME}. You are calling {name} about their upcoming appointment with {doctor} on \
{date} at {time}.

LANGUAGE: The patient may speak Hindi, English, or a mix of both (Hinglish). Detect the \
language of every patient message and always reply in that same language. Keep every reply \
short, natural, and conversational, since it will be read aloud by a speech synthesizer - never \
use markdown, bullet points, or special characters.

CALL SCRIPT:
1. Your very first message of the call must be exactly this, verbatim, and nothing else:
"{opening_en} / {opening_hi}"
2. After the patient responds, confirm you're speaking to the right person, then remind them of \
the appointment - with {doctor} on {date} at {time} - in whichever language the patient has been \
using.
3. Ask clearly whether they want to CONFIRM, RESCHEDULE, or CANCEL the appointment.
4. Once their intent is unambiguous, call the log_patient_response function with status set to \
exactly "confirmed", "reschedule", or "cancel". If it's not clear which of the three they mean, \
ask a clarifying question first instead of guessing.
5. After logging, thank them and say a brief goodbye in the same language they used, then stop \
talking - the call will end automatically a few seconds later.
"""


def make_log_response_tool(patient: dict, hangup_event: asyncio.Event) -> FunctionSchema:
    """Build the log_patient_response tool the LLM can call.

    Attaching `handler` directly to the FunctionSchema auto-registers it with
    the LLM service - no separate register_function() call needed.
    """

    async def handler(params: FunctionCallParams):
        status = params.arguments.get("status")
        logger.info(f"Patient {patient.get('patient_id')} ({patient.get('name')}): {status}")
        call_log.log_response(patient, status)
        await params.result_callback({"logged": True})
        hangup_event.set()

    return FunctionSchema(
        name="log_patient_response",
        description="Record the patient's decision about their upcoming appointment.",
        properties={
            "status": {
                "type": "string",
                "enum": ["confirmed", "reschedule", "cancel"],
                "description": "The patient's decision about the appointment.",
            }
        },
        required=["status"],
        handler=handler,
    )


async def run_bot(transport: BaseTransport, handle_sigint: bool, patient: dict):
    llm = GroqLLMService(
        api_key=os.getenv("GROQ_API_KEY"),
        settings=GroqLLMService.Settings(
            model="llama-3.3-70b-versatile",
            system_instruction=build_system_instruction(patient),
        ),
    )

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramSTTService.Settings(
            model="nova-3",
            language="multi",  # Deepgram's Hindi/English code-switching mode
            smart_format=True,
            endpointing=100,
        ),
    )

    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        sample_rate=8000,  # matches Twilio's native call audio rate
        settings=SarvamTTSService.Settings(
            model="bulbul:v2",
            voice=os.getenv("SARVAM_VOICE", "anushka"),
            language=Language.EN,  # starting language; LanguageRouter retunes this per turn
        ),
    )

    # Fires once the LLM has logged a confirm/reschedule/cancel decision.
    hangup_event = asyncio.Event()
    log_response_tool = make_log_response_tool(patient, hangup_event)

    language_router = LanguageRouter(tts)

    context = LLMContext(tools=[log_response_tool])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),  # audio in from Twilio
            stt,
            language_router,  # retunes `tts`'s language based on what STT just detected
            user_aggregator,
            llm,
            tts,
            transport.output(),  # audio out to Twilio
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,  # Twilio's native phone-call audio rate
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Call connected with {patient.get('name')} - starting reminder script")
        # Trigger the LLM to speak first, using the empty context (system prompt only).
        await worker.queue_frames([LLMRunFrame()])

        async def hangup_after_response():
            await hangup_event.wait()
            await asyncio.sleep(HANGUP_DELAY_SECS)
            await worker.queue_frames([EndFrame()])

        asyncio.create_task(hangup_after_response())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Call ended")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=handle_sigint)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point called by server.py for every new Twilio WebSocket connection."""
    transport_params = {
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }

    # create_transport auto-detects that this is a Twilio connection and builds
    # the matching TwilioFrameSerializer (using TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN).
    # It also parses Twilio's WebSocket handshake, populating runner_args.call_data.
    transport = await create_transport(runner_args, transport_params)

    # The patient dict we passed from server.py's /start endpoint, round-tripped
    # through the /answer TwiML as a <Parameter> and delivered here as one of
    # Twilio's customParameters.
    call_data = getattr(runner_args, "call_data", None)
    patient_json = (call_data.body or {}).get("patient_json") if call_data else None
    patient = json.loads(patient_json) if patient_json else {}

    await run_bot(transport, runner_args.handle_sigint, patient)


if __name__ == "__main__":
    # Not the normal path for this project (server.py drives calls end-to-end),
    # kept for parity with Pipecat's own examples / Pipecat Cloud compatibility.
    from pipecat.runner.run import main

    main()
