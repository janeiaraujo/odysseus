"""Speech engines, with the model itself replaced: what Jarvis asks of them is what matters."""
import wave
from types import SimpleNamespace

from services.stt.stt_service import STTService
from services.tts.tts_service import TTSService


def test_transcription_filters_noise_and_knows_the_assistant_name(monkeypatch, tmp_path):
    """Without vad_filter Whisper turns silence into "Tchau.", which would be sent to the chat."""
    seen = {}

    class FakeWhisper:
        def transcribe(self, path, **kwargs):
            seen.update(kwargs)
            return [SimpleNamespace(text=" Jarvis, bom dia ")], SimpleNamespace(language="pt", language_probability=1.0)
    service = STTService.__new__(STTService)
    monkeypatch.setattr(service, "_get_whisper", lambda *a, **k: FakeWhisper(), raising=False)
    assert service._transcribe_local(b"audio", "pt") == "Jarvis, bom dia"
    assert seen == {"hotwords": "Jarvis", "vad_filter": True, "language": "pt"}


def test_piper_output_is_a_playable_wav(monkeypatch):
    class FakeVoice:
        def synthesize_wav(self, text, wav):
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\0\0" * 2205)
    service = TTSService.__new__(TTSService)
    monkeypatch.setattr(service, "_get_piper", lambda voice="": FakeVoice(), raising=False)
    audio = service._synthesize_piper("olá")
    assert audio[:4] == b"RIFF"
    import io
    with wave.open(io.BytesIO(audio)) as result:
        assert result.getnframes() == 2205


def test_piper_failure_is_reported_as_no_audio_instead_of_raising(monkeypatch):
    service = TTSService.__new__(TTSService)

    def broken(voice=""):
        raise OSError("sem rede para baixar a voz")
    monkeypatch.setattr(service, "_get_piper", broken, raising=False)
    assert service._synthesize_piper("olá") is None
