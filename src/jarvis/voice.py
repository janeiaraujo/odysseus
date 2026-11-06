"""Voice turn pipeline, independent of the interface: audio in, text and audio out.

Reuses Odysseus's STT/TTS services (local Whisper or API). Audio stays in memory
and is discarded after transcription; only the text is kept, and it can be erased.
Wake word: in hands-free mode the client streams only speech segments found by its
voice-activity detection, and the server acts only if the transcript starts with
the name "Jarvis".
"""
from __future__ import annotations

import base64
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.jarvis import audit, config, intents, memory
from src.jarvis.db import session_scope
from src.jarvis.models import VoiceTurn

logger = logging.getLogger(__name__)
# Whisper often writes the name differently in Portuguese.
_WAKE = re.compile(r"^\W*(?:ei\s+|ok\s+|hey\s+|oi\s+)?(?:jarvis|jarves|jarvi|jarviz|djarvis|d'jarvis|charvis)\b[\s,.:!-]*", re.I)
_CANCEL = re.compile(r"^(cancelar?|cancele|pare|parar|chega|esquece|deixa pra la)\W*$", re.I)


class VoiceUnavailable(RuntimeError):
    pass


def status() -> Dict[str, Any]:
    out: Dict[str, Any] = {"stt": False, "tts": False, "store_audio": bool(config.get("voice.store_audio", False)), "wake_word": "transcricao"}
    try:
        from services.stt import get_stt_service
        out["stt"] = bool(get_stt_service().available)
    except Exception as exc:
        logger.debug("stt unavailable: %s", exc)
    try:
        from services.tts import get_tts_service
        out["tts"] = bool(get_tts_service().available)
    except Exception as exc:
        logger.debug("tts unavailable: %s", exc)
    return out


def warm_up() -> Dict[str, bool]:
    """Load the speech models ahead of time, so the first word isn't met with a long pause."""
    loaded = {"stt": False, "tts": False}
    try:
        from services.stt import get_stt_service
        service = get_stt_service()
        if service.available and service._load_settings().get("stt_provider") == "local":
            loaded["stt"] = service._get_whisper() is not None
    except Exception as exc:
        logger.warning("Whisper warm-up failed: %s", exc)
    try:
        from services.tts import get_tts_service
        service = get_tts_service()
        settings = service._load_settings()
        if service.available and settings.get("tts_provider") == "piper":
            loaded["tts"] = service._get_piper(settings.get("tts_voice", "")) is not None
    except Exception as exc:
        logger.warning("Piper warm-up failed: %s", exc)
    return loaded


def _speak(text: str) -> Optional[str]:
    try:
        from services.tts import get_tts_service
        service = get_tts_service()
        return service.synthesize_to_base64(text[:1500]) if service.available else None
    except Exception as exc:
        logger.info("tts failed: %s", exc)
        return None


def _transcribe(audio: bytes) -> str:
    from services.stt import get_stt_service
    service = get_stt_service()
    if not service.available:
        raise VoiceUnavailable("reconhecimento de voz não configurado")
    return (service.transcribe(audio) or "").strip()


async def _answer_with_llm(owner: str, text: str) -> str:
    from src.jarvis import persona
    from src.jarvis.llm import complete
    from src.prompt_security import untrusted_context_message

    messages: List[Dict[str, Any]] = [{"role": "system", "content": persona.system_preface() + "\nA resposta será falada: seja breve, sem markdown."}]
    context = memory.context_for_chat(text, owner=owner, limit=4)
    if context:
        messages.append(untrusted_context_message("jarvis memory", context))
    messages.append({"role": "user", "content": text})
    return await complete(owner, messages, purpose="voice")


async def turn(owner: Optional[str], *, audio: Optional[bytes] = None, text: Optional[str] = None, require_wake_word: bool = False, speak: bool = True,
               transcribe: Callable[[bytes], str] = _transcribe, llm: Callable[[str, str], Awaitable[str]] = _answer_with_llm,
               tts: Callable[[str], Optional[str]] = _speak) -> Dict[str, Any]:
    """One conversational turn. Deterministic commands are answered without a model."""
    owner = owner or ""
    transcript = text.strip() if text else (transcribe(audio) if audio else "")
    if not transcript:
        return {"transcript": "", "reply": "", "handled_by": "silence", "audio": None}
    if require_wake_word:
        match = _WAKE.match(transcript)
        if not match:
            return {"transcript": transcript, "reply": "", "handled_by": "no_wake_word", "audio": None}  # not addressed: nothing stored
    spoken_to = _WAKE.match(transcript)
    if spoken_to:  # the commands expect the name spelled correctly
        transcript = "Jarvis, " + transcript[spoken_to.end():]
    if _CANCEL.match(_WAKE.sub("", transcript).strip()):
        return {"transcript": transcript, "reply": "Cancelado.", "handled_by": "cancel", "audio": tts("Cancelado.") if speak else None, "cancel": True}
    direct = await intents.handle(transcript, owner)
    if direct:
        reply, handled_by = direct["text"], direct["intent"]
    else:
        reply, handled_by = await llm(owner, _WAKE.sub("", transcript).strip() or transcript), "llm"
        try:
            from src.jarvis.learning import observe_user_message
            observe_user_message(transcript, owner=owner, source_ref="voz")
        except Exception as exc:
            logger.info("voice learning skipped: %s", exc)
    with session_scope() as db:
        db.add(VoiceTurn(owner=owner, transcript=transcript, reply=reply, handled_by=handled_by))
    audit.record("voice.turn", owner=owner, handled_by=handled_by)
    return {"transcript": transcript, "reply": reply, "handled_by": handled_by, "audio": tts(reply) if speak else None}


def history(owner: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
    with session_scope() as db:
        rows = db.query(VoiceTurn).filter(VoiceTurn.owner == (owner or "")).order_by(VoiceTurn.created_at.desc()).limit(limit).all()
        return [{"id": r.id, "transcript": r.transcript, "reply": r.reply, "handled_by": r.handled_by, "created_at": r.created_at.isoformat() + "Z"} for r in rows]


def clear_history(owner: Optional[str]) -> int:
    with session_scope() as db:
        deleted = db.query(VoiceTurn).filter(VoiceTurn.owner == (owner or "")).delete()
    audit.record("voice.clear_history", owner=owner, risk="reversible", deleted=deleted)
    return deleted
