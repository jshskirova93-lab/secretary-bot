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
import uuid
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

# Предложенные ИИ разбивки больших задач ждут здесь подтверждения пользователя.
# Держим в памяти процесса: если бот перезапустится, старые предложения просто
# станут неактуальны — не страшно, пользователь повторит запрос.
_pending_big_tasks: dict[str, dict] = {}

# То же самое для операций с календарём: бот никогда не пишет в календарь,
# пока пользователь не нажмёт кнопку подтверждения.
_pending_calendar: dict[str, dict] = {}


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
    "• «дописать отчёт до пятницы» → задача\n"
    "• «встреча с Ивановым в четверг в 14:00» → событие в календаре\n"
    "• «перенеси встречу с Ивановым на пятницу» → перенесу в календаре\n"
    "• «купил кофе за 300 рублей» → трата\n"
    "• «подготовить презентацию для клиента» → предложу разбить на шаги\n"
    "• «Марина — моя помощница» → запомню и буду учитывать\n"
    "• «что у меня сегодня важного?» → отвечу по твоим делам\n\n"
    "*Команды* (или кнопка ☰ слева от поля ввода):\n"
    "/today — задачи на сегодня, с кнопками «готово»\n"
    "/week — план на 7 дней вперёд\n"
    "/month — план на 30 дней вперёд\n"
    "/memory — что я о тебе запомнил\n"
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


def _human_date(date_str: str) -> str:
    """2026-08-12 → «12 августа (среда)», с пометкой сегодня/завтра."""
    try:
        d = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return date_str
    return _format_day_header(d)


def _format_day_header(d: date) -> str:
    today = calendar_integration.today_local()
    if d == today:
        prefix = "Сегодня, "
    elif d == today + timedelta(days=1):
        prefix = "Завтра, "
    else:
        prefix = ""
    return f"{prefix}{d.day} {MONTHS_RU[d.month - 1]} ({WEEKDAYS_RU[d.weekday()]})"


async def _send_overview(message: Message, days: int, title: str) -> None:
    """Общий обзор задач и событий календаря на N дней вперёд, по дням."""
    today = calendar_integration.today_local()
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

    today = calendar_integration.today_local()
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


@dp.message(Command("caltest"))
async def cmd_caltest(message: Message) -> None:
    """Диагностика Google Календаря — показывает, что именно не так, если события не видны."""
    if not _owner_only(message.from_user.id):
        return
    await message.answer("Проверяю подключение к календарю...")
    report = calendar_integration.diagnose()
    await message.answer(report, parse_mode="Markdown")


@dp.message(Command("memory"))
async def cmd_memory(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    facts = database.list_facts(config.OWNER_CHAT_ID)
    if not facts:
        await message.answer(
            "Пока ничего не запомнил.\n\n"
            "Просто расскажи о себе или работе — например «Марина моя помощница» "
            "или «по средам я не работаю» — и я это учту в планах."
        )
        return

    lines = ["🧠 *Что я о тебе знаю:*", ""]
    current_cat = None
    for f in facts:
        if f["category"] != current_cat:
            current_cat = f["category"]
            lines.append(f"*{current_cat.capitalize()}*")
        lines.append(f"  • {f['fact']}  `/forget_{f['id']}`")
    lines.append("")
    lines.append("_Чтобы удалить факт — нажми на команду рядом с ним._")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(F.text.regexp(r"^/forget_(\d+)$"))
async def cmd_forget(message: Message) -> None:
    if not _owner_only(message.from_user.id):
        return
    fact_id = int(message.text.split("_")[1])
    if database.forget_fact(config.OWNER_CHAT_ID, fact_id):
        await message.answer("Забыл ✅")
    else:
        await message.answer("Такого факта не нашёл.")


@dp.callback_query(F.data.startswith("cal_add:"))
async def on_cal_add(callback: CallbackQuery) -> None:
    """Создаёт событие в календаре — только после явного согласия пользователя."""
    if not _owner_only(callback.from_user.id):
        return
    token = callback.data.split(":")[1]
    parsed = _pending_calendar.pop(token, None)
    if not parsed:
        await callback.answer("Это предложение устарело, повтори запрос")
        return

    result = calendar_integration.create_event(
        title=parsed["title"],
        date_str=parsed["date"],
        time_str=parsed.get("time"),
        duration_minutes=parsed.get("duration_minutes") or 60,
        description=parsed.get("description", ""),
        location=parsed.get("location", ""),
    )
    if result["ok"]:
        await callback.answer("Добавлено в календарь ✅")
        when = _human_date(parsed["date"])
        tp = f" в {parsed['time']}" if parsed.get("time") else ""
        await callback.message.edit_text(f"📅 В календаре: «{parsed['title']}» — {when}{tp}")
    else:
        await callback.answer("Не получилось")
        await callback.message.edit_text(
            f"❌ Не удалось добавить событие.\n\n{result['error']}\n\n"
            "Проверь командой /caltest — возможно, у бота пока нет права записи."
        )


@dp.callback_query(F.data.startswith("cal_do:"))
async def on_cal_do(callback: CallbackQuery) -> None:
    """Переносит, переименовывает или удаляет событие — после подтверждения."""
    if not _owner_only(callback.from_user.id):
        return
    token = callback.data.split(":")[1]
    payload = _pending_calendar.pop(token, None)
    if not payload:
        await callback.answer("Это предложение устарело, повтори запрос")
        return

    change, event = payload["change"], payload["event"]
    action = change["action"]

    if action == "отменить":
        result = calendar_integration.delete_event(event["id"])
        done_text = f"🗑 Удалено из календаря: «{event['title']}»"
    elif action == "переименовать":
        result = calendar_integration.update_event(event["id"], title=change.get("new_title"))
        done_text = f"✏️ Переименовано: «{change.get('new_title')}»"
    else:
        result = calendar_integration.update_event(
            event["id"],
            date_str=change.get("new_date"),
            time_str=change.get("new_time"),
        )
        when = _human_date(change["new_date"]) if change.get("new_date") else ""
        tp = f" в {change['new_time']}" if change.get("new_time") else ""
        done_text = f"📅 Перенесено: «{event['title']}» — {when}{tp}"

    if result["ok"]:
        await callback.answer("Готово ✅")
        await callback.message.edit_text(done_text)
    else:
        await callback.answer("Не получилось")
        await callback.message.edit_text(f"❌ Не удалось изменить событие.\n\n{result['error']}")


@dp.callback_query(F.data.startswith("cal_cancel:"))
async def on_cal_cancel(callback: CallbackQuery) -> None:
    if not _owner_only(callback.from_user.id):
        return
    token = callback.data.split(":")[1]
    _pending_calendar.pop(token, None)
    await callback.answer("Отменено")
    await callback.message.edit_text("Хорошо, ничего не меняю в календаре.")


@dp.callback_query(F.data.startswith("big_yes:"))
async def on_big_yes(callback: CallbackQuery) -> None:
    if not _owner_only(callback.from_user.id):
        return
    token = callback.data.split(":")[1]
    parsed = _pending_big_tasks.pop(token, None)
    if not parsed:
        await callback.answer("Это предложение устарело, повтори запрос")
        return
    for s in parsed["steps"]:
        database.add_task(
            user_id=config.OWNER_CHAT_ID,
            text=s["text"],
            priority=s.get("priority", "обычная"),
            due_date=s.get("due_date"),
            source="chat",
        )
    await callback.answer("Сохранил шаги ✅")
    await callback.message.edit_text(
        f"✅ «{parsed['title']}» разбита на {len(parsed['steps'])} шагов и добавлена в задачи."
    )


@dp.callback_query(F.data.startswith("big_no:"))
async def on_big_no(callback: CallbackQuery) -> None:
    if not _owner_only(callback.from_user.id):
        return
    token = callback.data.split(":")[1]
    parsed = _pending_big_tasks.pop(token, None)
    if not parsed:
        await callback.answer("Это предложение устарело, повтори запрос")
        return
    database.add_task(
        user_id=config.OWNER_CHAT_ID, text=parsed["title"], priority="обычная", source="chat"
    )
    await callback.answer("Сохранил одной задачей ✅")
    await callback.message.edit_text(f"✅ Записал одной задачей: «{parsed['title']}»")


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
    """Понимаем, что прислал пользователь: задачу, трату, факт для памяти,
    крупную задачу для разбивки на шаги или просто реплику."""
    facts = [dict(f) for f in database.list_facts(config.OWNER_CHAT_ID)]
    parsed = ai.parse_message(text, facts)
    kind = parsed.get("kind")
    source = "voice" if message.voice else "chat"

    if kind == "fact":
        database.remember_fact(
            user_id=config.OWNER_CHAT_ID,
            topic=parsed["topic"],
            fact=parsed["fact"],
            category=parsed.get("category", "прочее"),
        )
        await message.answer(f"Запомнил: {parsed['fact']} 🧠")
        return

    if kind == "cal_event":
        # В календарь ничего не пишем без явного подтверждения кнопкой
        token = str(uuid.uuid4())[:8]
        _pending_calendar[token] = parsed

        when = _human_date(parsed["date"])
        time_part = f" в {parsed['time']}" if parsed.get("time") else " (весь день)"
        lines = ["📅 Добавить в календарь?", "", f"*{parsed['title']}*", f"{when}{time_part}"]
        if parsed.get("duration_minutes") and parsed.get("time"):
            lines.append(f"Длительность: {parsed['duration_minutes']} мин")
        if parsed.get("location"):
            lines.append(f"Место: {parsed['location']}")
        if parsed.get("description"):
            lines.append(f"Детали: {parsed['description']}")

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Добавить", callback_data=f"cal_add:{token}"),
                    InlineKeyboardButton(text="✖️ Отмена", callback_data=f"cal_cancel:{token}"),
                ]
            ]
        )
        await message.answer("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
        return

    if kind == "cal_change":
        found = calendar_integration.find_events_by_title(parsed["search_query"])
        if not found:
            await message.answer(
                f"Не нашёл в календаре событие по запросу «{parsed['search_query']}». "
                "Попробуй назвать его точнее."
            )
            return
        if len(found) > 1:
            lines = ["Нашёл несколько событий — уточни, какое именно:", ""]
            for e in found[:5]:
                lines.append(f"• {_human_date(e['date'])} — {e['title']}")
            await message.answer("\n".join(lines))
            return

        event = found[0]
        token = str(uuid.uuid4())[:8]
        _pending_calendar[token] = {"change": parsed, "event": event}
        action = parsed["action"]

        if action == "отменить":
            text = f"Удалить из календаря событие «{event['title']}» ({_human_date(event['date'])})?"
            btn = "🗑 Удалить"
        elif action == "переименовать":
            text = f"Переименовать «{event['title']}» → «{parsed.get('new_title')}»?"
            btn = "✅ Переименовать"
        else:
            when = _human_date(parsed["new_date"]) if parsed.get("new_date") else "?"
            tp = f" в {parsed['new_time']}" if parsed.get("new_time") else ""
            text = f"Перенести «{event['title']}» на {when}{tp}?"
            btn = "✅ Перенести"

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=btn, callback_data=f"cal_do:{token}"),
                    InlineKeyboardButton(text="✖️ Отмена", callback_data=f"cal_cancel:{token}"),
                ]
            ]
        )
        await message.answer(text, reply_markup=kb)
        return

    if kind == "big_task":
        steps = parsed.get("steps", [])
        if not steps:
            return
        # Кладём шаги во временное хранилище — сохраним, только если пользователь согласится
        token = str(uuid.uuid4())[:8]
        _pending_big_tasks[token] = parsed

        lines = [f"Задача «{parsed['title']}» большая. Предлагаю разбить так:", ""]
        for i, s in enumerate(steps, 1):
            when = f" — {s['due_date']}" if s.get("due_date") else ""
            lines.append(f"{i}. {s['text']}{when}")
        lines.append("")
        lines.append("Сохранить эти шаги как отдельные задачи?")

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да, сохранить", callback_data=f"big_yes:{token}"),
                    InlineKeyboardButton(text="Одной задачей", callback_data=f"big_no:{token}"),
                ]
            ]
        )
        await message.answer("\n".join(lines), reply_markup=kb)
        return

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
    today = calendar_integration.today_local()
    load = database.tasks_load_by_day(
        config.OWNER_CHAT_ID, today.isoformat(), (today + timedelta(days=30)).isoformat()
    )
    # Даём ИИ и события календаря — иначе на вопрос «что у меня завтра?»
    # он видел бы только задачи и отвечал «ничего нет»
    events = calendar_integration.get_events_range(today, today + timedelta(days=30))
    reply = ai.chat_reply(text, open_tasks, facts, load, events, today.isoformat())
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
            BotCommand(command="memory", description="🧠 Что я о тебе помню"),
            BotCommand(command="caltest", description="🔍 Проверить Google Календарь"),
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
