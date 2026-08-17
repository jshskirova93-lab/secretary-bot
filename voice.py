"""
Распознавание голосовых сообщений.

Используем faster-whisper — он работает локально, поэтому не нужен отдельный
платный сервис распознавания речи.

ВАЖНО ПРО ПАМЯТЬ (главное в этом файле).
Первая попытка чинить перерасход памяти была такой: загрузить модель, распознать,
удалить объект модели и вызвать gc.collect(). Замеры показали, что это НЕ работает:
объект удаляется, но операционная система не забирает освобождённую память обратно
у процесса. Бот как занимал ~0,64 ГБ после первого голосового, так и продолжал
занимать — а хостинг берёт деньги именно за занятую память.

Работающее решение: распознавать в ОТДЕЛЬНОМ процессе (voice_worker.py) и сразу
его завершать. Когда процесс завершается, система забирает всю его память
целиком — тут уже без вариантов. Основной процесс бота остаётся на ~0,17 ГБ
всегда, независимо от количества голосовых.
"""
import logging
import os
import subprocess
import sys
import tempfile

logger = logging.getLogger("secretary_bot.voice")

# Путь к рабочему процессу — рядом с этим файлом, откуда бы бот ни запускался
_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_worker.py")

# Предохранитель: если распознавание зависнет, процесс будет убит через это время
_TIMEOUT_SECONDS = int(os.getenv("WHISPER_TIMEOUT", "300"))


def transcribe_ogg(ogg_bytes: bytes) -> str:
    """Принимает байты голосового сообщения Telegram (формат .ogg) и возвращает текст."""
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(ogg_bytes)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [sys.executable, _WORKER, tmp_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TIMEOUT_SECONDS,
        )

        if result.returncode != 0:
            # stderr рабочего процесса пишем в лог — там будет видна причина
            logger.error(
                "Распознавание не удалось (код %s): %s",
                result.returncode,
                (result.stderr or "").strip()[:2000],
            )
            raise RuntimeError("Не удалось распознать голосовое сообщение")

        return (result.stdout or "").strip()

    except subprocess.TimeoutExpired:
        logger.error("Распознавание превысило %s секунд и было прервано", _TIMEOUT_SECONDS)
        raise RuntimeError("Распознавание заняло слишком много времени")

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            logger.warning("Не удалось удалить временный файл %s", tmp_path)
