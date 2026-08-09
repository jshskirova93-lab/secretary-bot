"""
Планировщик утреннего плана и вечернего отчёта.

Почему APScheduler: лёгкий, не требует отдельного сервиса (в отличие от cron
на уровне ОС), работает прямо внутри процесса бота с учётом часового пояса.
"""
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import database
import ai
import calendar_integration


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "text": row["text"],
        "priority": row["priority"],
        "due_time": row["due_time"],
    }


async def send_morning_plan(bot) -> None:
    today = calendar_integration.today_local()
    today_str = today.isoformat()
    tomorrow_str = (today + timedelta(days=1)).isoformat()

    tasks = [_row_to_dict(r) for r in database.list_open_tasks(config.OWNER_CHAT_ID, due_date=today_str)]
    events = calendar_integration.get_today_events()

    # Заглядываем на завтра, чтобы предупредить о важном заранее
    tomorrow_tasks = [
        _row_to_dict(r)
        for r in database.list_open_tasks_range(config.OWNER_CHAT_ID, tomorrow_str, tomorrow_str)
    ]
    tomorrow_events = calendar_integration.get_tomorrow_events()

    # Нагрузка на 2 недели вперёд — чтобы ИИ мог заметить перекос по дням
    load = database.tasks_load_by_day(
        config.OWNER_CHAT_ID, today_str, (today + timedelta(days=14)).isoformat()
    )
    facts = [dict(f) for f in database.list_facts(config.OWNER_CHAT_ID)]

    plan_text = ai.generate_morning_plan(
        tasks, events, tomorrow_tasks, tomorrow_events, load, facts
    )
    await bot.send_message(config.OWNER_CHAT_ID, f"☀️ Доброе утро! План на сегодня:\n\n{plan_text}")


async def send_evening_report(bot) -> None:
    today = database.today_str()
    rows = database.list_tasks_created_between(config.OWNER_CHAT_ID, today)
    unfinished = [_row_to_dict(r) for r in rows if r["status"] == "open"]
    done = [_row_to_dict(r) for r in rows if r["status"] == "done"]
    report_text = ai.generate_evening_report(unfinished, done)

    spending = database.expenses_summary(config.OWNER_CHAT_ID, today)
    if spending["count"] > 0:
        spend_lines = ["", "💰 Траты за сегодня:"]
        for currency, total in spending["total_by_currency"].items():
            spend_lines.append(f"Итого: {total:.0f} {currency}")
        report_text += "\n" + "\n".join(spend_lines)

    await bot.send_message(config.OWNER_CHAT_ID, f"🌙 Итоги дня:\n\n{report_text}")


def setup_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

    morning_hour, morning_minute = config.MORNING_TIME.split(":")
    evening_hour, evening_minute = config.EVENING_TIME.split(":")

    scheduler.add_job(
        send_morning_plan,
        CronTrigger(hour=morning_hour, minute=morning_minute),
        args=[bot],
        id="morning_plan",
        replace_existing=True,
    )
    scheduler.add_job(
        send_evening_report,
        CronTrigger(hour=evening_hour, minute=evening_minute),
        args=[bot],
        id="evening_report",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
