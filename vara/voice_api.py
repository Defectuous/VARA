from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from vara.tts import SpeechUnavailable, Synthesizer, UnknownVoice, to_wav
from vara.voice import AudioError, Transcriber, decode_wav, split_wake_word

voice = APIRouter(prefix="/v1/voice")

# About five minutes of 16 kHz mono 16-bit audio; requests are spoken questions, not recordings.
MAX_AUDIO_BYTES = 10 * 1024 * 1024


@voice.post("/transcribe")
async def transcribe(request: Request) -> dict:
    """Speech to text, on this PC. The body is a WAV file (16-bit PCM, `Content-Type: audio/wav`).

    `wake_word` says whether the transcript starts with "VARA" (or "hey VARA"), and `request` is
    what follows it. Clients use this to ignore false wake-ups. Nothing is stored; send `request`
    to `/v1/chat` to ask it.
    """
    body = await request.body()
    if len(body) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio is too long")
    try:
        audio = decode_wav(body)
    except AudioError as exc:
        raise HTTPException(400, str(exc)) from exc
    transcriber: Transcriber = request.app.state.transcriber
    result = await run_in_threadpool(transcriber.transcribe, audio)
    wake_word, spoken_request = split_wake_word(result.text)
    return {
        "text": result.text,
        "wake_word": wake_word,
        "request": spoken_request,
        "language": result.language,
        "audio_seconds": result.audio_seconds,
        "processing_seconds": result.processing_seconds,
    }


class SpeakRequest(BaseModel):
    # A sentence or two at a time: clients speak replies sentence by sentence as they stream.
    text: str = Field(min_length=1, max_length=1000)
    voice: str | None = None
    speed: float | None = Field(default=None, ge=0.5, le=2.0)


def _synthesizer(request: Request) -> Synthesizer:
    return request.app.state.synthesizer


@voice.post("/speak", response_class=Response)
async def speak(body: SpeakRequest, request: Request) -> Response:
    """Text to speech, on this PC, with V.A.R.A.'s voice. Returns a WAV file. Nothing is stored."""
    settings = request.app.state.settings
    voice_name = body.voice or settings.tts_voice
    speed = body.speed or settings.tts_speed
    try:
        speech = await run_in_threadpool(
            _synthesizer(request).synthesize, body.text, voice_name, speed
        )
    except UnknownVoice as exc:
        raise HTTPException(400, f"Unknown voice: {exc}") from exc
    except SpeechUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(to_wav(speech), media_type="audio/wav")


@voice.get("/voices")
async def voices(request: Request) -> dict:
    """The installed voices and the default one (`VARA_TTS_VOICE`)."""
    try:
        names = await run_in_threadpool(_synthesizer(request).voices)
    except SpeechUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"default": request.app.state.settings.tts_voice, "voices": names}
