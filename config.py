"""
Загрузка настроек из .env файла.
Все параметры бота собраны в одном месте, чтобы не искать их по коду.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. "
            f"Проверьте файл .env (скопируйте .env.example и заполните)."
        )
    return value


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
OWNER_CHAT_ID = int(_require("OWNER_CHAT_ID"))

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
MORNING_TIME = os.getenv("MORNING_TIME", "08:00")
EVENING_TIME = os.getenv("EVENING_TIME", "21:00")

DB_PATH = os.getenv("DB_PATH", "secretary.db")

GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
GOOGLE_TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Модель Claude для планирования и разбора задач
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
