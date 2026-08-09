"""
Работа с базой данных задач.
Используем SQLite — файл на диске, не нужен отдельный сервер.

Почему так: для одного пользователя (или небольшой команды) SQLite полностью
достаточно, а поднимать Postgres ради личного секретаря — избыточно.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

import config


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def _today_local() -> date:
    """Сегодня по времени пользователя. Сервер живёт по UTC, поэтому просто
    date.today() дало бы неверный день вечером или ранним утром."""
    return datetime.now(_tz()).date()


def _now_local_iso() -> str:
    return datetime.now(_tz()).isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'обычная',   -- высокая / обычная / низкая
    due_date TEXT,                              -- YYYY-MM-DD, если известна дата
    due_time TEXT,                              -- HH:MM, если известно время
    status TEXT NOT NULL DEFAULT 'open',        -- open / done
    source TEXT NOT NULL DEFAULT 'chat',        -- chat / voice / calendar
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    topic TEXT NOT NULL,                        -- о чём факт: имя человека, проект, привычка
    fact TEXT NOT NULL,                         -- сам факт
    category TEXT NOT NULL DEFAULT 'прочее',    -- люди / проекты / привычки / прочее
    created_at TEXT NOT NULL,
    UNIQUE(user_id, topic)
);
"""

EXPENSES_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    category TEXT NOT NULL DEFAULT 'другое',
    description TEXT,
    source TEXT NOT NULL DEFAULT 'chat',        -- chat / voice
    created_at TEXT NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(SCHEMA)
        conn.execute(EXPENSES_SCHEMA)
        conn.execute(MEMORY_SCHEMA)


def add_task(
    user_id: int,
    text: str,
    priority: str = "обычная",
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    source: str = "chat",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (user_id, text, priority, due_date, due_time, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, text, priority, due_date, due_time, source, _now_local_iso()),
        )
        return cur.lastrowid


def list_open_tasks(user_id: int, due_date: Optional[str] = None) -> list[sqlite3.Row]:
    """Все незакрытые задачи. Если указана дата — только на неё (плюс задачи без даты)."""
    with get_conn() as conn:
        if due_date:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE user_id = ? AND status = 'open'
                   AND (due_date = ? OR due_date IS NULL) ORDER BY due_time IS NULL, due_time""",
                (user_id, due_date),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND status = 'open' ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return rows


def list_open_tasks_range(user_id: int, date_from: str, date_to: str) -> list[sqlite3.Row]:
    """Незакрытые задачи с датой в указанном периоде (включительно).
    Задачи без даты сюда не попадают — они показываются в плане каждый день,
    а в обзоре на неделю/месяц только загромождали бы список."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks WHERE user_id = ? AND status = 'open'
               AND due_date IS NOT NULL AND due_date >= ? AND due_date <= ?
               ORDER BY due_date, due_time IS NULL, due_time""",
            (user_id, date_from, date_to),
        ).fetchall()


def list_undated_open_tasks(user_id: int) -> list[sqlite3.Row]:
    """Незакрытые задачи без конкретной даты."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks WHERE user_id = ? AND status = 'open' AND due_date IS NULL
               ORDER BY created_at""",
            (user_id,),
        ).fetchall()


def list_tasks_created_between(user_id: int, day: str) -> list[sqlite3.Row]:
    """Задачи, у которых due_date == day, независимо от статуса — для вечернего отчёта."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND (due_date = ? OR due_date IS NULL) ORDER BY status",
            (user_id, day),
        ).fetchall()


def complete_task(task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ? AND status = 'open'",
            (_now_local_iso(), task_id),
        )
        return cur.rowcount > 0


def delete_task(task_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        return cur.rowcount > 0


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def today_str() -> str:
    return _today_local().isoformat()


# --- Расходы ---

def add_expense(
    user_id: int,
    amount: float,
    currency: str = "RUB",
    category: str = "другое",
    description: str = "",
    source: str = "chat",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO expenses (user_id, amount, currency, category, description, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, amount, currency, category, description, source, _now_local_iso()),
        )
        return cur.lastrowid


def list_expenses_since(user_id: int, since_date: str) -> list[sqlite3.Row]:
    """Все траты, начиная с указанной даты (включительно), сегодняшним днём заканчивая."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM expenses WHERE user_id = ? AND substr(created_at, 1, 10) >= ?
               ORDER BY created_at DESC""",
            (user_id, since_date),
        ).fetchall()


def tasks_load_by_day(user_id: int, date_from: str, date_to: str) -> dict[str, int]:
    """Сколько незакрытых задач на каждый день периода — чтобы ИИ видел перекос нагрузки."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT due_date, COUNT(*) AS cnt FROM tasks
               WHERE user_id = ? AND status = 'open' AND due_date IS NOT NULL
               AND due_date >= ? AND due_date <= ?
               GROUP BY due_date ORDER BY due_date""",
            (user_id, date_from, date_to),
        ).fetchall()
        return {r["due_date"]: r["cnt"] for r in rows}


# --- Память бота (факты о пользователе) ---

def remember_fact(user_id: int, topic: str, fact: str, category: str = "прочее") -> None:
    """Сохраняет факт. Если факт по этой теме уже есть — обновляет его."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO memory (user_id, topic, fact, category, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, topic) DO UPDATE SET fact = excluded.fact,
                                                          category = excluded.category""",
            (user_id, topic, fact, category, _now_local_iso()),
        )


def list_facts(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM memory WHERE user_id = ? ORDER BY category, topic", (user_id,)
        ).fetchall()


def forget_fact(user_id: int, fact_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM memory WHERE id = ? AND user_id = ?", (fact_id, user_id))
        return cur.rowcount > 0


def expenses_summary(user_id: int, since_date: str) -> dict:
    """Считаем суммы в Python, а не через Claude — с деньгами точность важнее красноречия."""
    rows = list_expenses_since(user_id, since_date)
    total_by_currency: dict[str, float] = {}
    by_category: dict[str, float] = {}
    for r in rows:
        total_by_currency[r["currency"]] = total_by_currency.get(r["currency"], 0.0) + r["amount"]
        by_category[r["category"]] = by_category.get(r["category"], 0.0) + r["amount"]
    return {"total_by_currency": total_by_currency, "by_category": by_category, "count": len(rows)}
