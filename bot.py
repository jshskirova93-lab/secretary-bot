"""
Личный секретарь — точка входа бота.

Команды:
  /start    — приветствие и подсказка как пользоваться
  /today    — список задач на сегодня с кнопками "готово"
  /week     — задачи и события календаря на 7 дней вперёд
  /month    — то же на 30 дней вперёд
  /plan     — принудительно прислать утренний план прямо сейчас
  /report   — принудительно прислать вечерний отчёт прямо сейчас
  /spending — траты за день/неделю/месяц (например /spending неделя)

Обычные сообщения (текст или голос) бот пытается понять сам: это задача,
трата денег или просто вопрос/реплика — определяет ИИ и сохраняет куда нужно.

Бот рассчитан на одного владельца (OWNER_CHAT_ID) — это личный секретарь,
а не публичный бот, поэтому все чужие сообщения игнорируются.
"""
import asyncio
import logging
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)

import config
import database
import ai
import voice
import calendar_integration
import scheduler as scheduler_module

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("secretary_bot")

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


def _owner_only(user_id: int) -> bool:
    return user_id == config.OWNER_CHAT_ID


def _tasks_keyboard(rows) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    buttons = [
        [InlineKeyboardButton(text=f"✅ {row['text'][:40]}", callback_data=f"done:{row['id']}")]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


HELP_TEXT = (
    "*Как пользоваться*\n\n"
    "Просто пиши или наговаривай — я сам разберусь, что это:\n"
    "• «завтра в 15:00 позвонить врачу, важно» → задача\n"
    "• «купил кофе за 300 рублей» → трата\n"
    "• «что у меня сегодня важного?» → отвечу по твоим делам\n\n"
    "*Команды* (или кнопка ☰ слева от поля ввода):\n"
    "/today — задачи на сегодня, с кнопками «готово»\n"
    "/week — план на 7 дней вперёд\n"
    "/month — план на 30 дней вперёд\n"
    "/spending — траты за сегодня\n"
    "   `/spending неделя` — за неделю\n"
    "   `/spending месяц` — за месяц\n"
    "/plan — прислать план прямо сейчас\n"
    "/report — прислать итоги дня прямо сейчас\n"
    "/help — эта подсказка"
)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await message.answer(
        "Привет! Я твой личный секретарь 👋\n\n"
        f"Каждое утро в {config.MORNING_TIME} пришлю план на день, "
        f"вечером в {config.EVENING_TIME} — что осталось незакрытым и сколько потрачено.\n\n"
        + HELP_TEXT,
        parse_mode="Markdown",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@dp.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    rows = database.list_open_tasks(config.OWNER_CHAT_ID, due_date=database.today_str())
    if not rows:
        await message.answer("На сегодня открытых задач нет 🎉")
        return
    lines = []
    for r in rows:
        time_part = f" ({r['due_time']})" if r["due_time"] else ""
        lines.append(f"• [{r['priority']}] {r['text']}{time_part}")
    await message.answer("\n".join(lines), reply_markup=_tasks_keyboard(rows))


@dp.message(Command("plan"))
async def cmd_plan(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await scheduler_module.send_morning_plan(bot)


@dp.message(Command("report"))
async def cmd_report(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await scheduler_module.send_evening_report(bot)


MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _format_day_header(d: date) -> str:
    today = date.today()
    if d == today:
        prefix = "Сегодня, "
    elif d == today + timedelta(days=1):
        prefix = "Завтра, "
    else:
        prefix = ""
    return f"{prefix}{d.day} {MONTHS_RU[d.month - 1]} ({WEEKDAYS_RU[d.weekday()]})"


async def _send_overview(message: Message, days: int, title: str) -> None:
    """Общий обзор задач и событий календаря на N дней вперёд, по дням."""
    today = date.today()
    last_day = today + timedelta(days=days - 1)

    tasks = database.list_open_tasks_range(
        config.OWNER_CHAT_ID, today.isoformat(), last_day.isoformat()
    )
    events = calendar_integration.get_events_range(today, last_day)

    # Складываем задачи и события в один словарь по датам
    by_day: dict[str, list[str]] = {}
    for t in tasks:
        time_part = f"{t['due_time']} — " if t["due_time"] else ""
        mark = "❗ " if t["priority"] == "высокая" else ""
        by_day.setdefault(t["due_date"], []).append(f"  • {mark}{time_part}{t['text']}")
    for e in events:
        time_part = ""
        if "T" in e["start"]:
            time_part = e["start"][11:16] + " — "
        by_day.setdefault(e["date"], []).append(f"  📅 {time_part}{e['title']}")

    lines = [title, ""]
    if by_day:
        for day_str in sorted(by_day.keys()):
            d = date.fromisoformat(day_str)
            lines.append(f"*{_format_day_header(d)}*")
            lines.extend(sorted(by_day[day_str]))
            lines.append("")
    else:
        lines.append("На этот период ничего не запланировано.")
        lines.append("")

    undated = database.list_undated_open_tasks(config.OWNER_CHAT_ID)
    if undated:
        lines.append("*Без конкретной даты:*")
        for t in undated:
            mark = "❗ " if t["priority"] == "высокая" else ""
            lines.append(f"  • {mark}{t['text']}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("week"))
async def cmd_week(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await _send_overview(message, days=7, title="🗓 Ближайшие 7 дней:")


@dp.message(Command("month"))
async def cmd_month(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await _send_overview(message, days=30, title="🗓 Ближайшие 30 дней:")


@dp.message(Command("spending"))
async def cmd_spending(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else "день"

    today = date.today()
    if arg in ("неделя", "week"):
        since = today - timedelta(days=today.weekday())
        label = "эту неделю"
    elif arg in ("месяц", "month"):
        since = today.replace(day=1)
        label = "этот месяц"
    else:
        since = today
        label = "сегодня"

    summary = database.expenses_summary(config.OWNER_CHAT_ID, since.isoformat())
    if summary["count"] == 0:
        await message.answer(f"Трат за {label} не найдено.")
        return

    lines = [f"💰 Расходы за {label}:", ""]
    for currency, total in summary["total_by_currency"].items():
        lines.append(f"Итого: {total:.0f} {currency}")
    lines.append("")
    lines.append("По категориям:")
    for cat, total in sorted(summary["by_category"].items(), key=lambda x: -x[1]):
        lines.append(f"• {cat}: {total:.0f}")
    await message.answer("\n".join(lines))


@dp.callback_query(F.data.startswith("done:"))
async def on_done(callback: CallbackQuery) -> None:
    if not _owner_only(callback.from_user.id):
        return
    task_id = int(callback.data.split(":")[1])
    ok = database.complete_task(task_id)
    if ok:
        await callback.answer("Отмечено как выполнено ✅")
        await callback.message.edit_text(f"~~{callback.message.text}~~\n(выполнено)")
    else:
        await callback.answer("Задача уже закрыта или не найдена")


async def _handle_incoming_text(message: Message, text: str) -> None:
    """Понимаем, что прислал пользователь: задачу, трату денег или просто реплику."""
    parsed = ai.parse_message(text)
    kind = parsed.get("kind")
    source = "voice" if message.voice else "chat"

    if kind == "task":
        database.add_task(
            user_id=config.OWNER_CHAT_ID,
            text=parsed["text"],
            priority=parsed.get("priority", "обычная"),
            due_date=parsed.get("due_date"),
            due_time=parsed.get("due_time"),
            source=source,
        )
        when = ""
        if parsed.get("due_date"):
            when = f" на {parsed['due_date']}"
            if parsed.get("due_time"):
                when += f" в {parsed['due_time']}"
        await message.answer(f"Записал: «{parsed['text']}»{when}. Приоритет: {parsed['priority']}.")
        return

    if kind == "expense":
        database.add_expense(
            user_id=config.OWNER_CHAT_ID,
            amount=parsed["amount"],
            currency=parsed.get("currency", "RUB"),
            category=parsed.get("category", "другое"),
            description=parsed.get("description", ""),
            source=source,
        )
        await message.answer(
            f"Записал трату: {parsed['amount']:.0f} {parsed.get('currency', 'RUB')} — "
            f"{parsed.get('description', '')} ({parsed.get('category', 'другое')})"
        )
        return

    open_tasks = [dict(r) for r in database.list_open_tasks(config.OWNER_CHAT_ID)]
    reply = ai.chat_reply(text, open_tasks)
    await message.answer(reply)


@dp.message(F.text)
async def on_text(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await _handle_incoming_text(message, message.text)


@dp.message(F.voice)
async def on_voice(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await message.answer("Слушаю голосовое...")
    file = await bot.get_file(message.voice.file_id)
    file_bytes = await bot.download_file(file.file_path)
    text = voice.transcribe_ogg(file_bytes.read())
    if not text:
        await message.answer("Не удалось распознать голосовое сообщение, попробуй ещё раз или напиши текстом.")
        return
    await message.answer(f"Распознал: «{text}»")
    await _handle_incoming_text(message, text)


async def setup_bot_commands() -> None:
    """Меню команд — появляется по кнопке слева от поля ввода в Telegram.
    Так не нужно помнить команды наизусть: видно, что есть и что делает."""
    await bot.set_my_commands(
        [
            BotCommand(command="today", description="📋 Задачи на сегодня"),
            BotCommand(command="week", description="🗓 План на 7 дней"),
            BotCommand(command="month", description="📆 План на 30 дней"),
            BotCommand(command="spending", description="💰 Траты (день/неделя/месяц)"),
            BotCommand(command="plan", description="☀️ Прислать план сейчас"),
            BotCommand(command="report", description="🌙 Прислать итоги дня"),
            BotCommand(command="help", description="❓ Как пользоваться"),
        ]
    )


async def main() -> None:
    database.init_db()
    await setup_bot_commands()
    scheduler_module.setup_scheduler(bot)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
