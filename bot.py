"""
Личный секретарь — точка входа бота.

Команды:
  /start    — приветствие и подсказка как пользоваться
  /today    — список задач на сегодня с кнопками "готово"
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
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database
import ai
import voice
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


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    await message.answer(
        "Привет! Я твой личный секретарь.\n\n"
        "Просто пиши мне задачи текстом или голосом — я их запомню.\n"
        f"Каждое утро в {config.MORNING_TIME} пришлю план на день, "
        f"а вечером в {config.EVENING_TIME} — итог по незакрытым делам.\n\n"
        "Команды:\n"
        "/today — задачи на сегодня\n"
        "/plan — прислать план прямо сейчас\n"
        "/report — прислать вечерний отчёт прямо сейчас"
    )


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


async def main() -> None:
    database.init_db()
    scheduler_module.setup_scheduler(bot)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
