"""
Обёртка над Claude API.

Задачи ИИ в этом боте:
1. parse_message — понять свободный текст пользователя и определить, что это:
   задача, трата денег или просто реплика/вопрос — и вернуть структурированные данные.
2. generate_morning_plan — утром собрать все задачи+события и написать живой план на день.
3. generate_evening_report — вечером посмотреть, что не закрыто, и написать итог.
4. chat_reply — обычный диалог, если пользователь просто задал вопрос, а не поставил задачу.

Почему через JSON-схему (tool use): так надёжнее, чем парсить текст руками —
Claude сам достаёт дату/время/приоритет из фразы вида "напомни завтра в 15 позвонить врачу"
или сумму/категорию из фразы вида "купил кофе за 300 рублей".
"""
import json
from datetime import date

from anthropic import Anthropic

import config

client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

TASK_TOOL = {
    "name": "save_task",
    "description": "Сохранить задачу или мероприятие, извлечённые из сообщения пользователя. Вызывай, только если в сообщении есть дело/задача/мероприятие — НЕ трата денег.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Краткая формулировка задачи."},
            "priority": {
                "type": "string",
                "enum": ["высокая", "обычная", "низкая"],
                "description": "Приоритет. Высокая — если есть слова 'срочно', 'важно', дедлайн сегодня/завтра.",
            },
            "due_date": {
                "type": ["string", "null"],
                "description": "Дата в формате YYYY-MM-DD, если указана или следует из контекста (например 'завтра'). Иначе null.",
            },
            "due_time": {
                "type": ["string", "null"],
                "description": "Время в формате HH:MM, если указано. Иначе null.",
            },
        },
        "required": ["text", "priority", "due_date", "due_time"],
    },
}

EXPENSE_TOOL = {
    "name": "save_expense",
    "description": "Сохранить трату денег/покупку/оплату, упомянутую в сообщении пользователя. Вызывай, только если пользователь сообщает, что он что-то купил, оплатил или потратил деньги.",
    "input_schema": {
        "type": "object",
        "properties": {
            "amount": {"type": "number", "description": "Сумма расхода, число без валюты."},
            "currency": {
                "type": "string",
                "description": "Код валюты (RUB, USD, EUR и т.п.). Если явно не указана — RUB.",
            },
            "category": {
                "type": "string",
                "enum": ["еда", "транспорт", "покупки", "счета и услуги", "развлечения", "здоровье", "другое"],
                "description": "Категория расхода, определи по смыслу покупки.",
            },
            "description": {"type": "string", "description": "Краткое описание, что именно куплено/оплачено."},
        },
        "required": ["amount", "currency", "category", "description"],
    },
}


def parse_message(user_text: str) -> dict:
    """Определяет, что прислал пользователь, и возвращает словарь с полем 'kind':
    - {'kind': 'task', ...поля задачи}
    - {'kind': 'expense', ...поля расхода}
    - {'kind': 'chat'} — если это не задача и не трата, а обычная реплика/вопрос.
    """
    today = date.today().isoformat()
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=500,
        system=(
            f"Сегодня {today}. Ты помогаешь личному секретарю понимать сообщения пользователя "
            f"на русском языке. Если в сообщении есть задача/дело/мероприятие — вызови save_task. "
            f"Если пользователь сообщает о покупке, оплате или трате денег — вызови save_expense. "
            f"Если это просто вопрос или реплика без задачи и без траты — не вызывай ни один инструмент."
        ),
        tools=[TASK_TOOL, EXPENSE_TOOL],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": user_text}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "save_task":
            data = dict(block.input)
            data["kind"] = "task"
            return data
        if block.type == "tool_use" and block.name == "save_expense":
            data = dict(block.input)
            data["kind"] = "expense"
            if not data.get("currency"):
                data["currency"] = "RUB"
            return data
    return {"kind": "chat"}


def generate_morning_plan(
    tasks: list[dict],
    calendar_events: list[dict],
    tomorrow_tasks: list[dict] | None = None,
    tomorrow_events: list[dict] | None = None,
) -> str:
    """tasks/calendar_events — на сегодня. tomorrow_* — чтобы предупредить о завтрашнем."""
    payload = {
        "задачи_сегодня": tasks,
        "события_календаря_сегодня": calendar_events,
        "задачи_завтра": tomorrow_tasks or [],
        "события_календаря_завтра": tomorrow_events or [],
    }
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=900,
        system=(
            "Ты — личный секретарь. Пиши по-русски, тепло и по-деловому, без канцелярита. "
            "Составь утренний план на день: сначала самое важное и срочное, потом остальное. "
            "Учитывай время встреч из календаря — не создавай конфликтов по времени в плане. "
            "Если задач и событий на сегодня нет — напиши короткое доброе утреннее сообщение "
            "без выдуманных дел. "
            "В самом конце, если на завтра есть что-то важное (встреча с фиксированным временем, "
            "срочная задача или что-то требующее подготовки заранее), добавь одну короткую строку "
            "вида «На заметку: завтра...». Если на завтра ничего важного — эту строку не добавляй вовсе. "
            "Не используй лишние заголовки, пиши компактно, можно с эмодзи по одному на пункт."
        ),
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    return _extract_text(response)


def generate_evening_report(unfinished: list[dict], done: list[dict]) -> str:
    payload = {"не_закрыто": unfinished, "выполнено": done}
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=600,
        system=(
            "Ты — личный секретарь. Пиши по-русски, доброжелательно, без нравоучений. "
            "Составь вечерний итог дня: сначала похвали за выполненное (кратко), потом честно "
            "перечисли, что осталось незакрытым, и мягко спроси — перенести на завтра или снять с контроля. "
            "Если всё выполнено — искренне похвали и не выдумывай проблем."
        ),
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    return _extract_text(response)


def chat_reply(user_text: str, open_tasks: list[dict]) -> str:
    """Свободный диалог — например, вопрос 'что у меня сегодня важного?'."""
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=600,
        system=(
            "Ты — личный секретарь пользователя, общаешься по-русски, кратко и по делу, "
            "дружелюбно, без канцелярита. Вот список его текущих незакрытых задач в формате JSON — "
            "используй его, чтобы отвечать по существу:\n" + json.dumps(open_tasks, ensure_ascii=False)
        ),
        messages=[{"role": "user", "content": user_text}],
    )
    return _extract_text(response)


def _extract_text(response) -> str:
    parts = [b.text for b in response.content if b.type == "text"]
    return "\n".join(parts).strip()
