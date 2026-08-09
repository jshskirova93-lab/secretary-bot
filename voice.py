"""
Распознавание голосовых сообщений.

Используем faster-whisper — он работает локально (модель скачивается один раз
при первом запуске), поэтому не нужен отдельный платный API для распознавания речи.
Модель "small" — компромисс между точностью и скоростью на обычном сервере без GPU.
"""
import os
import tempfile

from faster_whisper import WhisperModel

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        # compute_type="int8" — работает на CPU без видеокарты, приемлемая скорость
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    return _model


def transcribe_ogg(ogg_bytes: bytes) -> str:
    """Принимает байты голосового сообщения Telegram (формат .ogg) и возвращает текст."""
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(ogg_bytes)
        tmp_path = tmp.name

    try:
        model = _get_model()
        segments, _info = model.transcribe(tmp_path, language="ru")
        return " ".join(segment.text.strip() for segment in segments).strip()
    finally:
        os.remove(tmp_path)
