"""
Чтение событий из Google Calendar на сегодня.

Права запрашиваются только на чтение (calendar.readonly) — бот не может
создавать или менять события в вашем календаре.

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
import datetime as dt

import config

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _get_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        return None

    # Способ 1: через переменные окружения (для сервера, без браузера)
    if config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET and config.GOOGLE_OAUTH_REFRESH_TOKEN:
        creds = Credentials(
            token=None,
            refresh_token=config.GOOGLE_OAUTH_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=config.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return build("calendar", "v3", credentials=creds)

    # Способ 2: локальный запуск с файлами credentials.json/token.json
    if not os.path.exists(config.GOOGLE_CREDENTIALS_PATH):
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


def get_events_range(date_from: dt.date, date_to: dt.date) -> list[dict]:
    """События календаря за период (включительно): [{title, start, end, date}, ...].
    Пустой список, если Google Calendar не настроен или произошла ошибка."""
    service = _get_service()
    if service is None:
        return []

    try:
        time_min = dt.datetime.combine(date_from, dt.time.min).isoformat() + "Z"
        time_max = dt.datetime.combine(date_to, dt.time.max).isoformat() + "Z"

        events_result = (
            service.events()
            .list(
                calendarId=config.GOOGLE_CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
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
        return result
    except Exception:
        # Не роняем бота из-за проблем с календарём — просто работаем без него
        return []


def get_today_events() -> list[dict]:
    """События на сегодня."""
    today = dt.date.today()
    return get_events_range(today, today)


def get_tomorrow_events() -> list[dict]:
    """События на завтра — чтобы утром предупредить о важном заранее."""
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    return get_events_range(tomorrow, tomorrow)
