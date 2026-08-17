
"""
Распознавание голосовых сообщений.
 
Используем faster-whisper — он работает локально (модель скачивается один раз
при первом запуске), поэтому не нужен отдельный платный API для распознавания речи.
 
ВАЖНО ПРО ПАМЯТЬ:
Раньше модель загружалась один раз и навсегда оставалась в оперативной памяти
(переменная _model). Модель "small" занимает около 500 МБ, и сервер платно
держал их 24 часа в сутки, хотя голосовые приходят пару раз в день.
 
Теперь модель загружается только на время распознавания и сразу выгружается.
Бот в покое занимает ~190 МБ вместо ~700 МБ. Плата за память падает примерно
в три с половиной раза. Расплата — первые 3-6 секунд на распознавание уходят
на загрузку модели с диска. Для личного бота это незаметно.
"""
import gc
import logging
import os
import tempfile
 
from faster_whisper import WhisperModel
 
logger = logging.getLogger("secretary_bot.voice")
 
# Размер модели можно менять переменной окружения, не трогая код:
#   tiny  — ~75 МБ,  быстрая, качество похуже
#   base  — ~145 МБ, разумный компромисс (по умолчанию)
#   small — ~500 МБ, точнее всех, но дороже по памяти и медленнее
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "base")
 
 
def transcribe_ogg(ogg_bytes: bytes) -> str:
    """Принимает байты голосового сообщения Telegram (формат .ogg) и возвращает текст."""
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(ogg_bytes)
        tmp_path = tmp.name
 
    model = None
    try:
        # compute_type="int8" — работает на CPU без видеокарты, приемлемая скорость
        model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
 
        # ВАЖНО: transcribe() возвращает генератор — текст реально считывается
        # только здесь, в join(). Поэтому выгружать модель можно лишь ПОСЛЕ
        # того, как строка собрана, иначе распознавание оборвётся на полуслове.
        segments, _info = model.transcribe(tmp_path, language="ru")
        return " ".join(segment.text.strip() for segment in segments).strip()
    finally:
        # Освобождаем память независимо от того, была ошибка или нет.
        model = None
        del model
        gc.collect()
 
        try:
            os.remove(tmp_path)
        except OSError:
            # Файл уже удалён или недоступен — не роняем бота из-за такой мелочи
            logger.warning("Не удалось удалить временный файл %s", tmp_path)
