"""
Рабочий процесс распознавания речи. Запускается из voice.py как отдельный процесс:

    python voice_worker.py /путь/к/файлу.ogg

Печатает распознанный текст в стандартный вывод и завершается. Смысл именно в
завершении: вся память, которую занял faster-whisper (модель ~145 МБ плюс
внутренние буферы библиотеки), возвращается операционной системе целиком.
Внутри одного долгоживущего процесса так сделать нельзя — проверено замерами.

Размер модели задаётся переменной окружения WHISPER_MODEL:
  tiny (~75 МБ)  — быстро и дёшево, точность ниже
  base (~145 МБ) — значение по умолчанию, разумный компромисс
  small (~500 МБ) — точнее, но именно из-за неё и был перерасход
"""
import os
import sys


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("использование: python voice_worker.py <путь к аудиофайлу>\n")
        return 2

    audio_path = sys.argv[1]
    if not os.path.exists(audio_path):
        sys.stderr.write(f"файл не найден: {audio_path}\n")
        return 3

    # Импорт внутри функции: библиотека тяжёлая, грузим только когда точно нужна
    from faster_whisper import WhisperModel

    model_size = os.getenv("WHISPER_MODEL", "base")
    # compute_type="int8" — работает на обычном процессоре без видеокарты
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    segments, _info = model.transcribe(audio_path, language="ru")
    text = " ".join(segment.text.strip() for segment in segments).strip()

    # Пишем в stdout напрямую, без print — чтобы ничего лишнего не попало в текст
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
