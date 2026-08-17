"""
Рабочий процесс распознавания речи. Запускается отдельно, на одно голосовое.
 
Зачем отдельный процесс: библиотека faster-whisper выделяет под модель сотни
мегабайт, и после её выгрузки внутри Python операционная система эту память
обратно НЕ забирает — она остаётся числиться за процессом бота, и хостинг за
неё берёт деньги. А когда завершается отдельный процесс, система забирает всю
его память целиком и гарантированно. Поэтому распознаём здесь и сразу выходим.
 
Принимает: путь к .ogg файлу первым аргументом.
Возвращает: распознанный текст в стандартный вывод.
"""
import os
import sys
 
 
def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("Не передан путь к аудиофайлу\n")
        return 2
 
    audio_path = sys.argv[1]
 
    # Размер модели можно менять переменной окружения, не трогая код:
    #   tiny  — ~75 МБ,  быстрая, качество похуже
    #   base  — ~145 МБ, разумный компромисс (по умолчанию)
    #   small — ~500 МБ, точнее всех, но медленнее
    model_size = os.getenv("WHISPER_MODEL", "base")
 
    # Импорт внутри функции: тяжёлая библиотека грузится только здесь,
    # в основном процессе бота её вообще нет.
    from faster_whisper import WhisperModel
 
    # compute_type="int8" — работает на CPU без видеокарты, приемлемая скорость
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
 
    # transcribe() возвращает генератор — текст реально считывается только в join()
    segments, _info = model.transcribe(audio_path, language="ru")
    text = " ".join(segment.text.strip() for segment in segments).strip()
 
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
 

