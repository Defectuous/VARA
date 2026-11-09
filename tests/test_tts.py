import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from vara.config import Settings
from vara.main import create_app
from vara.tts import KokoroSynthesizer, Speech, SpeechUnavailable, UnknownVoice, speakable


class FakeSynthesizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def voices(self) -> list[str]:
        return ["af_heart", "bf_emma", "bf_lily"]

    def synthesize(self, text: str, voice: str, speed: float) -> Speech:
        if voice not in self.voices():
            raise UnknownVoice(voice)
        self.calls.append((text, voice, speed))
        return Speech(np.linspace(-1, 1, 2400, dtype=np.float32), 24000)


def client_with(fake, **settings) -> TestClient:
    return TestClient(create_app(Settings(**settings), synthesizer=fake))


def test_speak_returns_wav_in_the_default_voice() -> None:
    fake = FakeSynthesizer()

    response = client_with(fake).post("/v1/voice/speak", json={"text": "Hello there."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (
            24000,
            1,
            2,
            2400,
        )
    assert fake.calls == [("Hello there.", "bf_lily", 1.0)]


def test_speak_uses_the_configured_voice_and_request_overrides() -> None:
    fake = FakeSynthesizer()
    client = client_with(fake, tts_voice="bf_emma", tts_speed=1.1)

    client.post("/v1/voice/speak", json={"text": "One."})
    client.post("/v1/voice/speak", json={"text": "Two.", "voice": "af_heart", "speed": 0.9})

    assert fake.calls == [("One.", "bf_emma", 1.1), ("Two.", "af_heart", 0.9)]


def test_unknown_voice_is_a_400() -> None:
    response = client_with(FakeSynthesizer()).post(
        "/v1/voice/speak", json={"text": "Hi.", "voice": "zz_nobody"}
    )

    assert response.status_code == 400


def test_voices_lists_installed_voices_and_the_default() -> None:
    response = client_with(FakeSynthesizer()).get("/v1/voice/voices")

    assert response.json() == {"default": "bf_lily", "voices": ["af_heart", "bf_emma", "bf_lily"]}


@pytest.mark.parametrize(
    "payload", [{"text": ""}, {"text": "x" * 1001}, {"text": "hi", "speed": 5}]
)
def test_speak_rejects_bad_requests(payload: dict) -> None:
    assert client_with(FakeSynthesizer()).post("/v1/voice/speak", json=payload).status_code == 422


def test_missing_model_files_are_a_clear_503(tmp_path) -> None:
    synthesizer = KokoroSynthesizer(tmp_path / "missing.onnx", tmp_path / "missing.bin")

    with pytest.raises(SpeechUnavailable):
        synthesizer.synthesize("Hi.", "af_heart", 1.0)
    response = client_with(synthesizer).post("/v1/voice/speak", json={"text": "Hi."})
    assert response.status_code == 503
    assert "missing" in response.json()["detail"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I'm V.A.R.A., your assistant.", "I'm Vara, your assistant."),
        ("Ask V. A. R. A. anything", "Ask Vara anything"),
        ("VARA is here.", "VARA is here."),
    ],
)
def test_the_name_is_said_as_a_word_not_spelled_out(text: str, expected: str) -> None:
    assert speakable(text) == expected
