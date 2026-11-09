import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from vara.config import Settings
from vara.main import create_app
from vara.voice import AudioError, Transcript, decode_wav, split_wake_word


def wav_bytes(samples: np.ndarray, rate: int = 16_000, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            w.writeframes((samples * 32767).astype("<i2").tobytes())
        else:
            w.writeframes(bytes(len(samples)))
    return buffer.getvalue()


def test_decode_wav_resamples_to_16khz() -> None:
    audio = decode_wav(wav_bytes(np.zeros(44_100), rate=44_100))

    assert audio.dtype == np.float32
    assert audio.size == 16_000


def test_decode_wav_mixes_stereo_to_mono() -> None:
    left_right = np.array([0.5, -0.5] * 1000)  # interleaved: cancels out when averaged

    audio = decode_wav(wav_bytes(left_right, channels=2))

    assert audio.size == 1000
    assert np.allclose(audio, 0, atol=1e-4)


@pytest.mark.parametrize(
    "data",
    [b"not audio", wav_bytes(np.zeros(0)), wav_bytes(np.zeros(100), width=1)],
    ids=["garbage", "empty", "8-bit"],
)
def test_decode_wav_rejects_unusable_audio(data: bytes) -> None:
    with pytest.raises(AudioError):
        decode_wav(data)


@pytest.mark.parametrize(
    ("text", "request_text"),
    [
        ("VARA, what time is it?", "what time is it?"),
        ("Vara what time is it", "what time is it"),
        ("Hey VARA, turn it up.", "turn it up."),
        ("Okay, Vera. What's the weather?", "What's the weather?"),
        ("V.A.R.A. remind me later", "remind me later"),
        ("Varah, hello", "hello"),
        ("VARA's there?", "there?"),
        ("Vara.", ""),
    ],
)
def test_wake_word_variations_are_recognised(text: str, request_text: str) -> None:
    assert split_wake_word(text) == (True, request_text)


@pytest.mark.parametrize(
    "text",
    [
        "Sarah, what time is it?",
        "I was talking to Farah",
        "What time is it, VARA?",  # the wake word must come first
        "",
    ],
)
def test_other_speech_is_not_a_wake_word(text: str) -> None:
    assert split_wake_word(text)[0] is False


class FakeTranscriber:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, audio: np.ndarray) -> Transcript:
        self.calls += 1
        return Transcript(self.text, "en", round(audio.size / 16_000, 2), 0.01)


def voice_client(text: str) -> tuple[TestClient, FakeTranscriber]:
    fake = FakeTranscriber(text)
    return TestClient(create_app(Settings(), transcriber=fake)), fake


def test_transcribe_returns_text_and_the_request_after_the_wake_word() -> None:
    client, _ = voice_client("Hey VARA, what's on today?")

    response = client.post(
        "/v1/voice/transcribe",
        content=wav_bytes(np.zeros(32_000)),
        headers={"Content-Type": "audio/wav"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Hey VARA, what's on today?",
        "wake_word": True,
        "request": "what's on today?",
        "language": "en",
        "audio_seconds": 2.0,
        "processing_seconds": 0.01,
    }


def test_transcribe_rejects_bad_audio_without_running_the_model() -> None:
    client, fake = voice_client("unused")

    response = client.post("/v1/voice/transcribe", content=b"nope")

    assert response.status_code == 400
    assert fake.calls == 0


def test_transcribe_rejects_oversized_audio() -> None:
    client, fake = voice_client("unused")

    response = client.post("/v1/voice/transcribe", content=bytes(11 * 1024 * 1024))

    assert response.status_code == 413
    assert fake.calls == 0
