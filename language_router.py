"""language_router.py - keeps the TTS voice's language in sync with whatever
language the patient is currently speaking.

Deepgram's language="multi" STT mode (configured in bot.py) tags every
transcript with the language it detected. Unlike ElevenLabs' multilingual
model (which auto-detects language from the text itself), Sarvam's TTS needs
to be told explicitly which language to speak next - so this small processor
sits right after STT in the pipeline, watches transcripts go by, and retunes
the TTS service whenever the patient's detected language changes.
"""

from loguru import logger
from pipecat.frames.frames import Frame, TranscriptionFrame, TTSUpdateSettingsFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language

# Sarvam's bulbul:v2 model only speaks these languages (see
# pipecat.services.sarvam.tts.language_to_sarvam_language's LANGUAGE_MAP).
# Deepgram's language="multi" STT mode will occasionally misdetect a stray
# utterance as something outside this set (e.g. "es") - forwarding that
# straight to Sarvam breaks its websocket connection outright, so anything
# not in this set is ignored and the current TTS language is kept.
SARVAM_SUPPORTED_LANGUAGES = {
    Language.BN,
    Language.BN_IN,
    Language.EN,
    Language.EN_IN,
    Language.GU,
    Language.GU_IN,
    Language.HI,
    Language.HI_IN,
    Language.KN,
    Language.KN_IN,
    Language.ML,
    Language.ML_IN,
    Language.MR,
    Language.MR_IN,
    Language.OR,
    Language.OR_IN,
    Language.PA,
    Language.PA_IN,
    Language.TA,
    Language.TA_IN,
    Language.TE,
    Language.TE_IN,
}


class LanguageRouter(FrameProcessor):
    """Place this in the pipeline right after the STT service."""

    def __init__(self, tts: TTSService, default_language: Language = Language.EN):
        super().__init__()
        self._tts = tts
        self._current_language = default_language

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.language:
            if frame.language not in SARVAM_SUPPORTED_LANGUAGES:
                logger.warning(
                    f"Ignoring detected language {frame.language} - Sarvam doesn't support it"
                )
            elif frame.language != self._current_language:
                logger.info(f"Detected language change -> {frame.language}")
                self._current_language = frame.language
                await self.push_frame(
                    TTSUpdateSettingsFrame(
                        delta=type(self._tts).Settings(language=frame.language),
                        service=self._tts,
                    ),
                    direction,
                )

        await self.push_frame(frame, direction)
