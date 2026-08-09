"""
Чтение событий из Google Calendar на сегодня.

Права: чтение и запись. Бот умеет создавать, переносить и удалять события,
но делает это только после подтверждения пользователем кнопкой в чате —
сам по себе он в календарь ничего не пишет.

Два способа авторизации:
1. Через переменные окружения (GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN) —
   рабочий вариант для сервера (Railway), где нет браузера. Токен получается
   один раз через Google OAuth Playground (см. README) и больше не истекает.
2. Через файлы credentials.json/token.json с открытием браузера — удобно
   для локального запуска бота на своём компьютере.

Если Google Calendar вообще не настроен — бот просто работает без событий
календаря, ничего не ломается.
"""
import os
import logging
import datetime as dt
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("secretary_bot.calendar")

# Полный доступ к календарю: чтение + создание/изменение/удаление событий.
# Бот всё равно ничего не пишет без явного подтверждения пользователя кнопкой.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Последняя ошибка календаря — чтобы показать её в диагностике (/caltest)
last_error: str | None = None


def _get_service():
    global last_error
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        last_error = f"Библиотеки Google не установлены: {e}"
        log.warning(last_error)
        return None

    # Способ 1: через переменные окружения (для сервера, без браузера)
    if config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET and config.GOOGLE_OAUTH_REFRESH_TOKEN:
        try:
            creds = Credentials(
                token=None,
                refresh_token=config.GOOGLE_OAUTH_REFRESH_TOKEN,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=config.GOOGLE_OAUTH_CLIENT_ID,
                client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET,
                scopes=SCOPES,
            )
            creds.refresh(Request())
            last_error = None
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            last_error = f"Не удалось обновить токен Google: {type(e).__name__}: {e}"
            log.warning(last_error)
            return None

    # Способ 2: локальный запуск с файлами credentials.json/token.json
    if not os.path.exists(config.GOOGLE_CREDENTIALS_PATH):
        last_error = "Google Calendar не настроен: нет ни переменных OAuth, ни credentials.json"
        log.warning(last_error)
        return None

    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(config.GOOGLE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(config.GOOGLE_TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(config.GOOGLE_CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(config.GOOGLE_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def _tz() -> ZoneInfo:
    """Часовой пояс пользователя. Важно: сервер живёт по UTC, а дни у пользователя
    начинаются по его местному времени — иначе события уезжают на соседний день."""
    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def today_local() -> dt.date:
    """Сегодняшняя дата по часовому поясу пользователя, а не по времени сервера."""
    return dt.datetime.now(_tz()).date()


def get_events_range(date_from: dt.date, date_to: dt.date) -> list[dict]:
    """События календаря за период (включительно): [{title, start, end, date}, ...].
    Пустой список, если Google Calendar не настроен или произошла ошибка."""
    global last_error
    service = _get_service()
    if service is None:
        return []

    try:
        tz = _tz()
        # Границы периода в местном времени пользователя
        time_min = dt.datetime.combine(date_from, dt.time.min, tzinfo=tz).isoformat()
        time_max = dt.datetime.combine(date_to, dt.time.max, tzinfo=tz).isoformat()

        events_result = (
            service.events()
            .list(
                calendarId=config.GOOGLE_CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                timeZone=config.TIMEZONE,
            )
            .execute()
        )
        events = events_result.get("items", [])
        result = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date"))
            end = e["end"].get("dateTime", e["end"].get("date"))
            result.append(
                {
                    "title": e.get("summary", "Без названия"),
                    "start": start,
                    "end": end,
                    # Дата в виде YYYY-MM-DD — удобно группировать события по дням
                    "date": start[:10],
                }
            )
        last_error = None
        log.info("Календарь: %s..%s — получено событий: %d", date_from, date_to, len(result))
        return result
    except Exception as e:
        last_error = f"Ошибка запроса к календарю: {type(e).__name__}: {e}"
        log.warning(last_error)
        # Не роняем бота из-за проблем с календарём — просто работаем без него
        return []


def get_today_events() -> list[dict]:
    """События на сегодня."""
    today = today_local()
    return get_events_range(today, today)


def get_tomorrow_events() -> list[dict]:
    """События на завтра — чтобы утром предупредить о важном заранее."""
    tomorrow = today_local() + dt.timedelta(days=1)
    return get_events_range(tomorrow, tomorrow)


def create_event(
    title: str,
    date_str: str,
    time_str: str | None = None,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
) -> dict:
    """Создаёт событие в календаре.
    date_str — YYYY-MM-DD, time_str — HH:MM (если None, событие на весь день).
    Возвращает {'ok': True, 'link': ..., 'id': ...} или {'ok': False, 'error': ...}."""
    global last_error
    service = _get_service()
    if service is None:
        return {"ok": False, "error": last_error or "Календарь не подключён"}

    try:
        tz = _tz()
        body: dict = {"summary": title}
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        if time_str:
            start = dt.datetime.combine(
                dt.date.fromisoformat(date_str),
                dt.time.fromisoformat(time_str),
                tzinfo=tz,
            )
            end = start + dt.timedelta(minutes=duration_minutes)
            body["start"] = {"dateTime": start.isoformat(), "timeZone": config.TIMEZONE}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": config.TIMEZONE}
        else:
            # Событие на весь день: в Google Calendar конец — следующий день
            d = dt.date.fromisoformat(date_str)
            body["start"] = {"date": d.isoformat()}
            body["end"] = {"date": (d + dt.timedelta(days=1)).isoformat()}

        created = (
            service.events()
            .insert(calendarId=config.GOOGLE_CALENDAR_ID, body=body)
            .execute()
        )
        last_error = None
        log.info("Создано событие: %s (%s %s)", title, date_str, time_str or "весь день")
        return {"ok": True, "id": created.get("id"), "link": created.get("htmlLink")}
    except Exception as e:
        last_error = f"Не удалось создать событие: {type(e).__name__}: {e}"
        log.warning(last_error)
        return {"ok": False, "error": last_error}


def find_events_by_title(query: str, days_ahead: int = 60) -> list[dict]:
    """Ищет события по названию на ближайшие дни — чтобы понять, что переносить/удалять."""
    global last_error
    service = _get_service()
    if service is None:
        return []

    try:
        tz = _tz()
        today = today_local()
        time_min = dt.datetime.combine(today, dt.time.min, tzinfo=tz).isoformat()
        time_max = dt.datetime.combine(
            today + dt.timedelta(days=days_ahead), dt.time.max, tzinfo=tz
        ).isoformat()

        res = (
            service.events()
            .list(
                calendarId=config.GOOGLE_CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                q=query,
                singleEvents=True,
                orderBy="startTime",
                maxResults=20,
                timeZone=config.TIMEZONE,
            )
            .execute()
        )
        result = []
        for e in res.get("items", []):
            start = e["start"].get("dateTime", e["start"].get("date"))
            result.append(
                {
                    "id": e["id"],
                    "title": e.get("summary", "Без названия"),
                    "start": start,
                    "date": start[:10],
                }
            )
        return result
    except Exception as e:
        last_error = f"Не удалось найти событие: {type(e).__name__}: {e}"
        log.warning(last_error)
        return []


def update_event(
    event_id: str,
    title: str | None = None,
    date_str: str | None = None,
    time_str: str | None = None,
    duration_minutes: int = 60,
) -> dict:
    """Изменяет существующее событие: название и/или дату-время."""
    global last_error
    service = _get_service()
    if service is None:
        return {"ok": False, "error": last_error or "Календарь не подключён"}

    try:
        event = (
            service.events()
            .get(calendarId=config.GOOGLE_CALENDAR_ID, eventId=event_id)
            .execute()
        )
        if title:
            event["summary"] = title

        if date_str:
            tz = _tz()
            if time_str:
                start = dt.datetime.combine(
                    dt.date.fromisoformat(date_str),
                    dt.time.fromisoformat(time_str),
                    tzinfo=tz,
                )
                end = start + dt.timedelta(minutes=duration_minutes)
                event["start"] = {"dateTime": start.isoformat(), "timeZone": config.TIMEZONE}
                event["end"] = {"dateTime": end.isoformat(), "timeZone": config.TIMEZONE}
            else:
                d = dt.date.fromisoformat(date_str)
                event["start"] = {"date": d.isoformat()}
                event["end"] = {"date": (d + dt.timedelta(days=1)).isoformat()}

        updated = (
            service.events()
            .update(calendarId=config.GOOGLE_CALENDAR_ID, eventId=event_id, body=event)
            .execute()
        )
        last_error = None
        log.info("Изменено событие %s", event_id)
        return {"ok": True, "id": updated.get("id"), "link": updated.get("htmlLink")}
    except Exception as e:
        last_error = f"Не удалось изменить событие: {type(e).__name__}: {e}"
        log.warning(last_error)
        return {"ok": False, "error": last_error}


def delete_event(event_id: str) -> dict:
    """Удаляет событие из календаря."""
    global last_error
    service = _get_service()
    if service is None:
        return {"ok": False, "error": last_error or "Календарь не подключён"}
    try:
        service.events().delete(
            calendarId=config.GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()
        last_error = None
        log.info("Удалено событие %s", event_id)
        return {"ok": True}
    except Exception as e:
        last_error = f"Не удалось удалить событие: {type(e).__name__}: {e}"
        log.warning(last_error)
        return {"ok": False, "error": last_error}


def has_write_access() -> bool:
    """Проверяет, выдан ли токен с правом записи (а не только чтения)."""
    service = _get_service()
    if service is None:
        return False
    try:
        cal = service.calendarList().get(calendarId=config.GOOGLE_CALENDAR_ID).execute()
        return cal.get("accessRole") in ("owner", "writer")
    except Exception:
        return False


def diagnose() -> str:
    """Понятный отчёт о состоянии подключения к календарю — для команды /caltest."""
    lines = ["🔍 *Проверка Google Календаря*", ""]

    has_env = bool(
        config.GOOGLE_OAUTH_CLIENT_ID
        and config.GOOGLE_OAUTH_CLIENT_SECRET
        and config.GOOGLE_OAUTH_REFRESH_TOKEN
    )
    lines.append(f"Ключи доступа заданы: {'да ✅' if has_env else 'нет ❌'}")
    lines.append(f"Календарь: `{config.GOOGLE_CALENDAR_ID}`")
    lines.append(f"Часовой пояс: {config.TIMEZONE}")
    lines.append(f"Сегодня по вашему времени: {today_local()}")
    lines.append("")

    service = _get_service()
    if service is None:
        lines.append("Подключение: не удалось ❌")
        lines.append(f"Причина: `{last_error or 'неизвестна'}`")
        return "\n".join(lines)

    lines.append("Подключение: успешно ✅")

    can_write = has_write_access()
    if can_write:
        lines.append("Права: чтение и запись ✅")
    else:
        lines.append("Права: только чтение ⚠️")
        lines.append("_Чтобы бот мог создавать события — нужен новый токен, см. README._")

    # Пробуем прочитать список календарей — так видно, к чему вообще есть доступ
    try:
        cal_list = service.calendarList().list(maxResults=20).execute().get("items", [])
        lines.append("")
        lines.append(f"*Доступные календари ({len(cal_list)}):*")
        for c in cal_list:
            mark = " ← основной" if c.get("primary") else ""
            lines.append(f"  • {c.get('summary', '?')}{mark}")
            lines.append(f"    `{c.get('id')}`")
    except Exception as e:
        lines.append(f"Не удалось получить список календарей: `{e}`")

    today = today_local()
    events_today = get_events_range(today, today)
    events_month = get_events_range(today, today + dt.timedelta(days=30))
    lines.append("")
    lines.append(f"Событий на сегодня: {len(events_today)}")
    lines.append(f"Событий на 30 дней вперёд: {len(events_month)}")
    if events_month:
        lines.append("")
        lines.append("*Ближайшие:*")
        for e in events_month[:5]:
            lines.append(f"  • {e['date']} — {e['title']}")
    if last_error:
        lines.append("")
        lines.append(f"Последняя ошибка: `{last_error}`")
    return "\n".join(lines)
