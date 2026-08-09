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


MEMORY_TOOL = {
    "name": "remember_fact",
    "description": (
        "Запомнить факт о пользователе, его окружении или работе — то, что пригодится потом. "
        "Вызывай, когда пользователь сообщает что-то о себе, о людях вокруг, о своих проектах "
        "или привычках (например: «Марина — моя помощница», «проект Альфа — это ремонт офиса», "
        "«по средам я не работаю»). НЕ вызывай для разовых задач и трат."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Кратко, о чём факт — имя человека, название проекта, тема привычки. Служит ключом.",
            },
            "fact": {"type": "string", "description": "Сам факт одной фразой."},
            "category": {
                "type": "string",
                "enum": ["люди", "проекты", "привычки", "прочее"],
                "description": "К чему относится факт.",
            },
        },
        "required": ["topic", "fact", "category"],
    },
}

BIG_TASK_TOOL = {
    "name": "split_big_task",
    "description": (
        "Разбить крупную задачу на конкретные выполнимые шаги. Вызывай, когда задача явно "
        "большая или расплывчатая и в один присест её не сделать (например «подготовить "
        "презентацию для клиента», «организовать переезд офиса», «запустить сайт»). "
        "Для простых однодневных дел вызывай обычный save_task."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Название крупной задачи целиком."},
            "steps": {
                "type": "array",
                "description": "От 2 до 7 конкретных шагов в логическом порядке.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Что конкретно сделать на этом шаге."},
                        "due_date": {
                            "type": ["string", "null"],
                            "description": "Дата YYYY-MM-DD, если можно разумно распределить шаги по дням до дедлайна. Иначе null.",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["высокая", "обычная", "низкая"],
                        },
                    },
                    "required": ["text", "due_date", "priority"],
                },
            },
        },
        "required": ["title", "steps"],
    },
}


def _facts_block(facts: list[dict] | None) -> str:
    """Готовим факты из памяти для подстановки в системный промпт."""
    if not facts:
        return ""
    lines = [f"- {f['topic']}: {f['fact']}" for f in facts]
    return (
        "\n\nЧто ты уже знаешь о пользователе (используй, чтобы понимать его лучше, "
        "но не пересказывай без нужды):\n" + "\n".join(lines)
    )


def parse_message(user_text: str, facts: list[dict] | None = None) -> dict:
    """Определяет, что прислал пользователь, и возвращает словарь с полем 'kind':
    - {'kind': 'task', ...поля задачи}
    - {'kind': 'expense', ...поля расхода}
    - {'kind': 'fact', ...факт для памяти}
    - {'kind': 'big_task', title, steps}
    - {'kind': 'chat'} — обычная реплика/вопрос.
    """
    today = date.today().isoformat()
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1200,
        system=(
            f"Сегодня {today}. Ты помогаешь личному секретарю понимать сообщения пользователя "
            f"на русском языке.\n"
            f"- Обычная задача/дело/мероприятие → save_task\n"
            f"- Покупка, оплата, трата денег → save_expense\n"
            f"- Крупная задача, которую надо разбить на шаги → split_big_task\n"
            f"- Факт о пользователе, людях, проектах, привычках → remember_fact\n"
            f"- Просто вопрос или реплика → не вызывай инструментов\n"
            f"Вызывай ровно один инструмент." + _facts_block(facts)
        ),
        tools=[TASK_TOOL, EXPENSE_TOOL, BIG_TASK_TOOL, MEMORY_TOOL],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": user_text}],
    )
    for block in response.content:
        if block.type != "tool_use":
            continue
        data = dict(block.input)
        if block.name == "save_task":
            data["kind"] = "task"
            return data
        if block.name == "save_expense":
            data["kind"] = "expense"
            if not data.get("currency"):
                data["currency"] = "RUB"
            return data
        if block.name == "split_big_task":
            data["kind"] = "big_task"
            return data
        if block.name == "remember_fact":
            data["kind"] = "fact"
            return data
    return {"kind": "chat"}


def generate_morning_plan(
    tasks: list[dict],
    calendar_events: list[dict],
    tomorrow_tasks: list[dict] | None = None,
    tomorrow_events: list[dict] | None = None,
    load_by_day: dict[str, int] | None = None,
    facts: list[dict] | None = None,
) -> str:
    """tasks/calendar_events — на сегодня. tomorrow_* — предупредить о завтрашнем.
    load_by_day — сколько задач на каждый день недели, чтобы заметить перекос."""
    payload = {
        "задачи_сегодня": tasks,
        "события_календаря_сегодня": calendar_events,
        "задачи_завтра": tomorrow_tasks or [],
        "события_календаря_завтра": tomorrow_events or [],
        "нагрузка_по_дням_на_2_недели": load_by_day or {},
    }
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=900,
        system=(
            "Ты — личный секретарь. Пиши по-русски, тепло и по-деловому, без канцелярита. "
            "Составь утренний план на день: сначала самое важное и срочное, потом остальное. "
            "Учитывай время встреч из календаря — не создавай конфликтов по времени в плане. "
            "Если задач и событий на сегодня нет — напиши короткое доброе утреннее сообщение "
            "без выдуманных дел.\n"
            "В конце можешь добавить максимум ДВЕ короткие строки, только если есть что сказать:\n"
            "1) «На заметку: завтра...» — если завтра есть что-то важное или требующее подготовки.\n"
            "2) Совет по нагрузке — если по данным нагрузка_по_дням видно сильный перекос "
            "(в один день задач намного больше, чем в соседние), мягко предложи часть перенести "
            "на свободный день, назвав конкретные дни. Если перекоса нет — молчи об этом.\n"
            "Не выдумывай проблем и не морализируй. "
            "Не используй лишние заголовки, пиши компактно, можно с эмодзи по одному на пункт."
            + _facts_block(facts)
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


def chat_reply(
    user_text: str,
    open_tasks: list[dict],
    facts: list[dict] | None = None,
    load_by_day: dict[str, int] | None = None,
) -> str:
    """Свободный диалог — например, вопрос 'что у меня сегодня важного?'."""
    today = date.today().isoformat()
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=700,
        system=(
            f"Сегодня {today}. Ты — личный секретарь пользователя, общаешься по-русски, кратко "
            f"и по делу, дружелюбно, без канцелярита.\n"
            f"Его текущие незакрытые задачи (JSON):\n"
            + json.dumps(open_tasks, ensure_ascii=False)
            + "\nНагрузка по дням (сколько задач на каждый день):\n"
            + json.dumps(load_by_day or {}, ensure_ascii=False)
            + _facts_block(facts)
        ),
        messages=[{"role": "user", "content": user_text}],
    )
    return _extract_text(response)


def _extract_text(response) -> str:
    parts = [b.text for b in response.content if b.type == "text"]
    return "\n".join(parts).strip()
