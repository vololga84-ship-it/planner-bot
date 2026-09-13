"""
Личный Планнер — Telegram Bot
Голос → Groq Whisper API → Groq LLaMA API → Google Sheets
Без библиотеки groq — прямые HTTP запросы
"""

import os, re, time, logging, json, tempfile, base64, asyncio, requests
from datetime import datetime, timedelta, timezone, time as dt_time
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from aiohttp import web

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
SPREADSHEET_ID  = os.getenv("SPREADSHEET_ID")
SPREADSHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"

def _parse_owner_map(raw):
    """Общий разбор строк вида "Оля:значение,Мама:значение" — используется
    и для DASHBOARD_URLS (значение — ссылка), и для CALENDAR_IDS
    (значение — email календаря), у каждого владельца своё."""
    result = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        owner, value = part.split(":", 1)
        owner, value = owner.strip(), value.strip()
        if owner and value:
            result[owner] = value
    return result

DASHBOARD_URLS = _parse_owner_map(os.getenv("DASHBOARD_URLS"))
# CALENDAR_IDS=Оля:oly.gmail@gmail.com,Мама:lena.gmail@gmail.com — календарь
# каждого владельца, куда бот пишет задачи с указанным временем. Чтобы
# заработало, владелец должен один раз расшарить свой Google Календарь
# сервисному аккаунту бота (email из GOOGLE_CREDENTIALS_JSON, поле
# client_email) с правом "Изменение мероприятий" — так же, как расшарена
# таблица.
CALENDAR_IDS = _parse_owner_map(os.getenv("CALENDAR_IDS"))
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "Asia/Yekaterinburg")

def _parse_users(raw):
    """USERS=182778711:Оля,555555555:Мама:Лена — Telegram ID -> (имя
    владельца в планере, обращение к ней самой). Третье поле необязательно
    — например, для семейного бота дочери зовут её "Мама" (так называется
    её лист/привычки/цели, так её ищут в "peek"), а бот при обращении К
    НЕЙ самой говорит "Лена". Без третьего поля обращение = имя владельца."""
    users = {}
    display_names = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        fields = [f.strip() for f in part.split(":")]
        uid  = fields[0]
        name = fields[1] if len(fields) > 1 else ""
        display = fields[2] if len(fields) > 2 and fields[2] else name
        if uid and name:
            users[uid] = name
            display_names[uid] = display
    return users, display_names

USER_NAMES, DISPLAY_NAMES = _parse_users(os.getenv("USERS"))
ALLOWED_USERS = set(USER_NAMES.keys())
ALL_OWNERS    = list(dict.fromkeys(USER_NAMES.values()))  # без дублей, сохраняя порядок

def display_name_for(update):
    """Как обращаться к самому пользователю (может отличаться от имени
    владельца в планере — см. _parse_users)."""
    return DISPLAY_NAMES.get(str(update.effective_user.id)) or owner_for(update)

def owner_for(update):
    return USER_NAMES.get(str(update.effective_user.id), "Гость")

def _words(text):
    return set(re.findall(r"\w+", text.lower(), re.UNICODE))

def _ru_stem(word):
    """Грубый стемминг: у русских имён на -а/-я падежные окончания почти
    всегда меняют только последнюю букву (мама/маму/мамы/маме, оля/олю/оле)."""
    return word[:-1] if len(word) > 2 else word

def resolve_owner_name(spoken):
    """Сопоставить произнесённое имя (в любом падеже) с одним из известных
    участников."""
    if not spoken:
        return None
    spoken_words = _words(spoken)
    for name in ALL_OWNERS:
        if _words(name) & spoken_words:
            return name
    # запасной вариант — сравнить по основе слова (без падежного окончания)
    for name in ALL_OWNERS:
        name_stem = _ru_stem(name.lower())
        if any(_ru_stem(w) == name_stem for w in spoken_words):
            return name
    return None

# Кто ведёт блог — только этим владельцам показываем кнопку "Идеи для
# постов" (у остальных участников семейного бота такой функции нет).
BLOG_OWNERS = set(o.strip() for o in os.getenv("BLOG_OWNERS", "Оля").split(",") if o.strip())

def main_keyboard_for(owner):
    row2 = ([KeyboardButton("💡 Идеи для постов")] if owner in BLOG_OWNERS else []) + [KeyboardButton("📝 Заметки")]
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📋 Меню"), KeyboardButton("📊 Дашборд"), KeyboardButton("📅 Сегодня")], row2],
        resize_keyboard=True,
        is_persistent=True,
    )

CAPTURE_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("⬅️ Выйти")]],
    resize_keyboard=True,
    is_persistent=True,
)

# ── Groq API (прямые запросы) ──────────────────────────────────
GROQ_HEADERS = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
}

def transcribe_voice(file_path):
    """Расшифровка голоса через Groq Whisper."""
    with open(file_path, "rb") as f:
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=GROQ_HEADERS,
            files={"file": (os.path.basename(file_path), f, "audio/ogg")},
            data={"model": "whisper-large-v3", "response_format": "text"},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.text.strip()

def parse_task(text, today, habit_names, owner_names):
    """Понять задачу через Groq LLaMA."""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
    habit_list_str = ", ".join(f'"{name}"' for name in habit_names)
    owners_list_str = ", ".join(f'"{name}"' for name in owner_names) or "нет других участников"
    prompt = f"""Сегодня {today}. Пользователь сказал: "{text}"

Ответь ТОЛЬКО валидным JSON объектом, без пояснений, без markdown:
{{"type":"task","category":"личное","task":"{text}","date":null,"time":null,"habit_name":null,"habit_value":null,"period":null,"target_user":null,"missing":[]}}

Заполни поля правильно:
- type: "task" (обычная разовая задача), "habit" (привычка: сон/витамины/прогулка/вода/встала/легла), "note" (заметка), "goal" (цель на месяц или на год, а не разовая задача — например «цель на месяц выучить 50 слов» или «добавь годовую цель — накопить на отпуск»), "delete" (просьба удалить/убрать/стереть/отменить уже существующую запись, например «удали запись про парикмахера»), "peek" (спрашивают о делах/привычках/целях ДРУГОГО участника, а не о своих — например «что у Мамы сегодня?», «какие у неё цели?»), "question" (вопрос, комментарий или рассуждение вслух, НЕ задача для записи)
- category: "работа", "личное" или "дом". Если не указано явно — угадай по контексту (парикмахер/врач/магазин = личное, уборка/готовка = дом, встреча/звонок коллеге = работа)
- task: краткое описание задачи или цели. Для type="delete" — только ключевые слова для поиска записи, без слов «удали»/«убери»/«сотри»
- date: если упомянута ЛЮБАЯ дата — "сегодня", "завтра", "послезавтра", конкретное число («15 сентября», «20.09»), день недели («в понедельник», ближайший предстоящий) или «через N дней» — вычисли её от сегодняшней даты ({today}) и верни в формате ДД.ММ.ГГГГ. Если дата вообще не упомянута — null
- time: время в формате ЧЧ:ММ если упомянуто, иначе null
- habit_name: для привычки выбери одно точное название из списка: {habit_list_str}
- habit_value: значение привычки без пояснений, например "23:30", "✅", "45", "8"
- period: для type="goal" — "месяц" или "год" (если не сказано явно, поставь "месяц"), иначе null
- target_user: для type="peek" — имя участника, о ком спрашивают, одно из: {owners_list_str}. Иначе null
- missing: [] (всегда пустой — угадывай категорию сам)"""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={**GROQ_HEADERS, "Content-Type": "application/json"},
        json={
            "model": "qwen/qwen3.6-27b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "none",
            "reasoning_format": "hidden",
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    # Clean up any markdown
    raw = raw.replace("```json", "").replace("```", "").strip()
    # Extract JSON if wrapped in text
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]
    return json.loads(raw)

def extract_screenshot_data(image_path):
    """Разбирает произвольный скриншот через Groq vision (та же модель,
    что разбирает текст задач — умеет и в картинки): если это экран
    приложения здоровья — извлекает данные сна/шагов; если что-то другое
    (билет, переписка, документ) — переписывает весь видимый текст, чтобы
    сохранить его как заметку. Поля, которых нет на скриншоте, null."""
    with open(image_path, "rb") as f:
        b64_image = base64.b64encode(f.read()).decode("utf-8")
    prompt = (
        "Определи тип этого скриншота и извлеки данные. "
        "Ответь ТОЛЬКО валидным JSON объектом, без пояснений, без markdown:\n"
        '{"vstala":null,"legla":null,"son_dlitelnost":null,"shagi":null,"km":null,'
        '"minuty":null,"data_na_ekrane":null,"tekst":null}\n\n'
        'Если это скриншот приложения "Здоровье" (сон, шаги, активность) — заполни '
        'ниже описанные поля сна/шагов, "tekst" оставь null.\n'
        'Если это НЕ скриншот здоровья (билет, переписка, документ, любой другой '
        'экран с текстом) — поля здоровья оставь null, а в "tekst" перепиши ВЕСЬ '
        "видимый на экране текст как можно точнее и по порядку, не теряя важные "
        "детали (даты, время, места, имена, суммы).\n\n"
        "Поля здоровья (заполняй только то, что реально видно на экране, не выдумывай):\n"
        "- vstala: время подъёма/пробуждения, формат ЧЧ:ММ\n"
        "- legla: время отхода ко сну, формат ЧЧ:ММ\n"
        '- son_dlitelnost: длительность сна как есть на экране (например "7ч 18м" или "7:18")\n'
        "- shagi: число шагов (только цифры)\n"
        "- km: пройденное расстояние в км (число, точка как разделитель)\n"
        "- minuty: минуты ходьбы/активности (только цифры)\n"
        '- data_na_ekrane: дата или день, к которому относятся эти данные, ТОЧНО как '
        'написано на экране (например "13 сентября", "Сб", "Сегодня", "Вчера", "13.09") — '
        "если на экране вообще нет никакого указания на дату/день, оставь null. "
        "Не путай это с временем (ЧЧ:ММ) — здесь нужна именно дата/день."
    )
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={**GROQ_HEADERS, "Content-Type": "application/json"},
        json={
            "model": "qwen/qwen3.6-27b",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                ],
            }],
            "max_tokens": 1200,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "none",
            "reasoning_format": "hidden",
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]
    return json.loads(raw)

# ── Google Sheets: недельный шаблон ─────────────────────────────
TEMPLATE_SHEET = "📅 Неделя"
CATEGORY_ROWS = {
    "работа": (5, 10, "💼 РАБОТА"),
    "личное": (12, 17, "👤 ЛИЧНОЕ"),
    "дом": (19, 24, "🏠 ДОМ"),
}
HABITS_SHEET     = "⚙️ Мои привычки"
HABIT_START_ROW  = 26
HABIT_MAX_ROWS   = 10  # столько строк отведено под привычки в шаблоне (26-35)
HABIT_CACHE_TTL  = 300  # секунд
DEFAULT_HABITS = [
    "🌙 Легла спать", "☀️ Встала", "😴 Сон (часов)",
    "💊 Витамины утром", "💊 Витамины вечер", "🚶 Прогулка",
    "💧 Вода", "📖 Чтение", "😊 Настроение", "⚡ Энергия",
]
_habit_cache = {"names": None, "ts": 0}

def get_habit_list():
    """Список привычек из листа настроек (в порядке строк), не больше
    HABIT_MAX_ROWS штук — столько строк физически есть в шаблоне недели.
    Кэшируется на HABIT_CACHE_TTL секунд, чтобы не дёргать Sheets на каждое
    сообщение. Список общий для всех участников."""
    now = time.time()
    if _habit_cache["names"] is not None and now - _habit_cache["ts"] < HABIT_CACHE_TTL:
        return _habit_cache["names"]
    try:
        sheet = get_sheet()
        ws = sheet.worksheet(HABITS_SHEET)
        values = ws.get(f"A3:A{2 + HABIT_MAX_ROWS + 10}")
        names = []
        for row in values:
            name = (row[0] if row else "").strip()
            if not name or name.startswith("("):
                continue
            names.append(name)
            if len(names) >= HABIT_MAX_ROWS:
                break
        if not names:
            names = list(DEFAULT_HABITS)
    except Exception:
        logger.exception("Could not read habits settings sheet")
        names = _habit_cache["names"] or list(DEFAULT_HABITS)
    _habit_cache["names"] = names
    _habit_cache["ts"] = now
    return names

def habit_row_map():
    return {name: HABIT_START_ROW + i for i, name in enumerate(get_habit_list())}

def find_habit_name(keyword):
    """Ищет привычку по ключевому слову в её названии (список настраиваемый
    пользователем в листе "⚙️ Мои привычки", поэтому не завязываемся на
    точный текст/эмодзи). None, если такой привычки сейчас нет."""
    for name in get_habit_list():
        if keyword.lower() in name.lower():
            return name
    return None

def get_sheet():
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

# ── Google Calendar: задачи с указанным временем ─────────────────
_calendar_creds = None

def get_calendar_access_token():
    """Токен того же сервисного аккаунта, что и для Sheets, но с
    отдельным scope на календарь (кэшируется, google-auth сам обновляет
    протухший токен при .refresh())."""
    global _calendar_creds
    if _calendar_creds is None:
        creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
        _calendar_creds = Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/calendar.events"])
    if not _calendar_creds.valid:
        _calendar_creds.refresh(GoogleAuthRequest())
    return _calendar_creds.token

def add_calendar_event(owner, date_str, time_str, summary, category):
    """Пишет задачу с указанным временем в личный Google Календарь
    владельца (час по умолчанию длительность). Тихо ничего не делает,
    если для владельца не настроен CALENDAR_IDS или календарь ещё не
    расшарен сервисному аккаунту — задача при этом всё равно остаётся
    записанной в планере, календарь тут просто дополнительная копия."""
    calendar_id = CALENDAR_IDS.get(owner)
    if not calendar_id or not time_str:
        return
    try:
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
        end_dt = start_dt + timedelta(hours=1)
        token = get_calendar_access_token()
        body = {
            "summary": summary,
            "description": f"Из «Мой планер» ({category})" if category else "Из «Мой планер»",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": CALENDAR_TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": CALENDAR_TIMEZONE},
        }
        resp = requests.post(
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body, timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception(f"Не удалось записать событие в календарь {owner}")

def week_start(date_str):
    day = datetime.strptime(date_str, "%d.%m.%Y")
    return day - timedelta(days=day.weekday())

def week_sheet_name(date_str, owner):
    monday = week_start(date_str)
    sunday = monday + timedelta(days=6)
    return f"📅 {owner} {monday:%d.%m}–{sunday:%d.%m}"

def get_week_sheet(date_str, owner, create=False):
    sheet = get_sheet()
    title = week_sheet_name(date_str, owner)
    try:
        return sheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        if not create:
            return None
        template = sheet.worksheet(TEMPLATE_SHEET)
        sheet.batch_update({"requests": [{"duplicateSheet": {
            "sourceSheetId": template.id, "newSheetName": title
        }}]})
        ws = sheet.worksheet(title)
        monday = week_start(date_str)
        ws.update("B3:H3", [[(monday + timedelta(days=i)).strftime("%d.%m") for i in range(7)]])
        ws.batch_clear(["B5:H10", "B12:H17", "B19:H24", "B26:H35"])
        habit_names = get_habit_list()
        labels = [[name] for name in habit_names]
        labels += [[""]] * (HABIT_MAX_ROWS - len(labels))
        ws.update(f"A{HABIT_START_ROW}:A{HABIT_START_ROW + HABIT_MAX_ROWS - 1}", labels)
        return ws

def day_column(date_str):
    return chr(ord("B") + datetime.strptime(date_str, "%d.%m.%Y").weekday())

def add_task_to_sheet(owner, category, task, time_str, date_str, row_type="task"):
    ws = get_week_sheet(date_str, owner, create=True)
    column = day_column(date_str)
    if row_type == "habit":
        row = habit_row_map().get(task)
        if not row:
            raise ValueError(f"Неизвестная привычка: {task}")
        ws.update(f"{column}{row}", [[time_str]])
        return row

    start_row, end_row, _ = CATEGORY_ROWS.get(category, CATEGORY_ROWS["личное"])
    values = ws.get(f"{column}{start_row}:{column}{end_row}")
    for row_num in range(start_row, end_row + 1):
        offset = row_num - start_row
        if offset >= len(values) or not values[offset] or not values[offset][0]:
            task_text = f"☐ {task}" + (f" — {time_str}" if time_str else "")
            ws.update(f"{column}{row_num}", [[task_text]])
            return row_num
    raise ValueError("В этой категории на день уже шесть задач.")

def get_tasks_for_day(date_str, owner):
    try:
        ws = get_week_sheet(date_str, owner, create=False)
        if not ws:
            return []
        column = day_column(date_str)
        tasks = []
        for category, (start_row, end_row, label) in CATEGORY_ROWS.items():
            values = ws.get(f"{column}{start_row}:{column}{end_row}")
            for row_num in range(start_row, end_row + 1):
                offset = row_num - start_row
                value = values[offset][0] if offset < len(values) and values[offset] else ""
                if value:
                    status = "✅" if value.startswith("✅") else "☐"
                    tasks.append((row_num, [label, value.lstrip("☐✅ ").strip(), "", status, "task"]))
        return tasks
    except Exception:
        logger.exception("Could not read weekly tasks")
        return []

def get_habits_for_day(date_str, owner):
    ws = get_week_sheet(date_str, owner, create=False)
    if not ws:
        return []
    column = day_column(date_str)
    values = ws.get(f"A26:{column}35")
    day_index = ord(column) - ord("A")
    return [(26 + i, ["⏰ ПРИВЫЧКА", row[0], row[day_index], "", "habit"])
            for i, row in enumerate(values) if len(row) > day_index and row[day_index]]

def mark_task_done(date_str, owner, row_num):
    ws = get_week_sheet(date_str, owner, create=False)
    if not ws:
        return
    column = day_column(date_str)
    value = ws.acell(f"{column}{row_num}").value or ""
    ws.update(f"{column}{row_num}", [["✅ " + value.lstrip("☐✅ ").strip()]])

def delete_entry_from_sheet(date_str, owner, row_num):
    ws = get_week_sheet(date_str, owner, create=False)
    if not ws:
        return
    column = day_column(date_str)
    ws.update(f"{column}{row_num}", [[""]])

def category_for_row(row_num):
    for category, (start_row, end_row, _label) in CATEGORY_ROWS.items():
        if start_row <= row_num <= end_row:
            return category
    return "личное"

def move_task_to_date(owner, date_str, row_num, new_date_str):
    """Переносит задачу из (date_str, row_num) на new_date_str, сохраняя
    категорию и время. Возвращает текст перенесённой задачи (без
    чекбокса/времени) или None, если ячейка была уже пуста (например,
    перенесли дважды)."""
    ws = get_week_sheet(date_str, owner, create=False)
    if not ws:
        return None
    column = day_column(date_str)
    value = (ws.acell(f"{column}{row_num}").value or "").strip()
    if not value:
        return None
    task_text, time_str = split_task_time(value.lstrip("☐✅ ").strip())
    category = category_for_row(row_num)
    add_task_to_sheet(owner, category, task_text, time_str or "", new_date_str)
    delete_entry_from_sheet(date_str, owner, row_num)
    if time_str:
        add_calendar_event(owner, new_date_str, time_str, task_text, category)
    return task_text

def find_matching_entries(date_str, owner, query):
    """Найти задачи/привычки за день и цели ВЛАДЕЛЬЦА owner, похожие на
    query, по совпадению слов (без учёта эмодзи/пунктуации). Возвращает
    список записей с наибольшим числом общих слов:
    (kind, row_num, label, text_для_показа).
    kind — "day" (задача/привычка за конкретный день) или "goal"."""
    candidates = []
    for row_num, row in get_tasks_for_day(date_str, owner):
        candidates.append(("day", row_num, row[0], row[1]))
    for row_num, row in get_habits_for_day(date_str, owner):
        display = f"{row[1]} — {row[2]}" if row[2] else row[1]
        candidates.append(("day", row_num, row[0], display))
    for row_num, period, category, goal_text, _, _ in get_goals(owner):
        candidates.append(("goal", row_num, f"Цель на {period}", goal_text))

    query_words = _words(query or "")
    if not query_words:
        return []
    scored = [(len(query_words & _words(c[3])), c) for c in candidates]
    best = max((s for s, _ in scored), default=0)
    if best == 0:
        return []
    return [c for s, c in scored if s == best]

# ── Google Sheets: цели (месяц/год), отдельный лист на участника ─
GOALS_TEMPLATE = "🎯 Цели"
GOAL_ROWS = {
    "месяц": {"работа": (5, 8), "личное": (10, 13), "дом": (15, 18)},
    "год":   {"работа": (23, 26), "личное": (28, 31), "дом": (33, 36)},
}

def goals_sheet_name(owner):
    return f"🎯 Цели {owner}"

def get_goals_sheet(owner, create=False):
    sheet = get_sheet()
    title = goals_sheet_name(owner)
    try:
        return sheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        if not create:
            return None
        template = sheet.worksheet(GOALS_TEMPLATE)
        sheet.batch_update({"requests": [{"duplicateSheet": {
            "sourceSheetId": template.id, "newSheetName": title
        }}]})
        return sheet.worksheet(title)

def add_goal(owner, period, category, goal_text, deadline=""):
    ws = get_goals_sheet(owner, create=True)
    start_row, end_row = GOAL_ROWS[period][category]
    values = ws.get(f"B{start_row}:B{end_row}")
    for row_num in range(start_row, end_row + 1):
        offset = row_num - start_row
        cell = values[offset][0] if offset < len(values) and values[offset] else ""
        if not cell.strip():
            ws.update(f"B{row_num}:E{row_num}", [[goal_text, "", deadline, "☐"]])
            return row_num
    raise ValueError(f"Все слоты целей на {period} ({category}) заняты.")

def get_goals(owner):
    """Все непустые цели владельца. Возвращает список
    (row_num, period, category, goal_text, deadline, status)."""
    ws = get_goals_sheet(owner, create=False)
    if not ws:
        return []
    goals = []
    for period, categories in GOAL_ROWS.items():
        for category, (start_row, end_row) in categories.items():
            values = ws.get(f"B{start_row}:E{end_row}")
            for i, row_num in enumerate(range(start_row, end_row + 1)):
                row = values[i] if i < len(values) else []
                goal_text = row[0] if len(row) > 0 else ""
                if not goal_text.strip():
                    continue
                deadline = row[2] if len(row) > 2 else ""
                status   = row[3] if len(row) > 3 else "☐"
                goals.append((row_num, period, category, goal_text, deadline, status))
    return goals

def mark_goal_done(owner, row_num):
    ws = get_goals_sheet(owner, create=False)
    if not ws:
        return
    ws.update(f"E{row_num}", [["✅"]])

def delete_goal(owner, row_num):
    ws = get_goals_sheet(owner, create=False)
    if not ws:
        return
    ws.update(f"B{row_num}:E{row_num}", [["", "", "", ""]])

# ── Идеи для постов + общие заметки ───────────────────────────────
IDEAS_SHEET = "💡 Идеи для постов"
NOTES_SHEET = "📝 Заметки"

def _get_or_create_log_sheet(title, create):
    sheet = get_sheet()
    try:
        return sheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        if not create:
            return None
        ws = sheet.add_worksheet(title=title, rows=200, cols=5)
        ws.append_row(["Дата", "Время", "Владелец", "Текст", "Статус"])
        return ws

def _append_log_row(title, owner, text):
    ws = _get_or_create_log_sheet(title, create=True)
    now = datetime.now()
    ws.append_row([now.strftime("%d.%m.%Y"), now.strftime("%H:%M"), owner, text, "новая"])

def get_ideas_sheet(create=False):
    return _get_or_create_log_sheet(IDEAS_SHEET, create)

def add_idea_to_sheet(owner, text):
    _append_log_row(IDEAS_SHEET, owner, text)

def get_notes_sheet(create=False):
    return _get_or_create_log_sheet(NOTES_SHEET, create)

def add_note_to_sheet(owner, text):
    _append_log_row(NOTES_SHEET, owner, text)

def get_new_notes_by_owner():
    """Новые (статус "новая") заметки листа "Заметки", по владельцам,
    отсортированные по дате и времени (старые первыми)."""
    ws = get_notes_sheet(create=False)
    if not ws:
        return {}
    rows = ws.get_all_values()
    by_owner = {}
    for i, row in enumerate(rows[1:], start=2):  # строка 1 — заголовок
        date, time_str, owner, text, status = (row + [""] * 5)[:5]
        if status.strip() != "новая" or not text.strip():
            continue
        try:
            sort_key = datetime.strptime(f"{date} {time_str}", "%d.%m.%Y %H:%M")
        except ValueError:
            sort_key = datetime.min
        by_owner.setdefault(owner, []).append((sort_key, i, text))
    for owner in by_owner:
        by_owner[owner].sort(key=lambda entry: entry[0])
    return by_owner

def mark_notes_processed(rows):
    ws = get_notes_sheet(create=False)
    if not ws:
        return
    for row in rows:
        ws.update(f"E{row}", [["обработана"]])

# ── State ──────────────────────────────────────────────────────
user_states = {}
post_idea_mode_users = set()
general_notes_mode_users = set()
pending_health = {}  # user_id -> данные со скриншота "Здоровья", ждём дату
pending_postpone = {}  # user_id -> {"date_str", "row_num"}, ждём дату переноса

RU_MONTHS_GEN = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

def parse_flexible_date(text, today_dt):
    """Понимает "сегодня"/"вчера", "13.09"/"13.09.2026", "13 сентября" и
    похожее. Возвращает строку ДД.ММ.ГГГГ или None, если не поняла."""
    if not text:
        return None
    t = text.strip().lower()
    if "сегодня" in t:
        return today_dt.strftime("%d.%m.%Y")
    if "вчера" in t:
        return (today_dt - timedelta(days=1)).strftime("%d.%m.%Y")

    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", t)
    if m:
        day, month, year = (int(x) for x in m.groups())
        try:
            return datetime(year, month, day).strftime("%d.%m.%Y")
        except ValueError:
            return None

    m = re.search(r"(\d{1,2})\s+(" + "|".join(RU_MONTHS_GEN) + r")", t)
    if m:
        day = int(m.group(1))
        month = RU_MONTHS_GEN[m.group(2)]
        try:
            return datetime(today_dt.year, month, day).strftime("%d.%m.%Y")
        except ValueError:
            return None

    m = re.search(r"(\d{1,2})\.(\d{1,2})(?!\.\d)", t)
    if m:
        day, month = (int(x) for x in m.groups())
        try:
            return datetime(today_dt.year, month, day).strftime("%d.%m.%Y")
        except ValueError:
            return None
    return None

def is_allowed(update):
    return str(update.effective_user.id) in ALLOWED_USERS

# ── Save task ──────────────────────────────────────────────────
async def save_task(update_or_query, parsed, default_date, owner):
    date_given = bool(parsed.get("date"))
    date_str   = parsed.get("date") or default_date
    category   = parsed.get("category", "личное")
    task       = parsed.get("task", "")
    time_str   = parsed.get("time") or ""
    cat_emoji  = {"работа": "💼", "личное": "👤", "дом": "🏠"}.get(category, "📌")
    try:
        row_num = add_task_to_sheet(owner, category, task, time_str, date_str)
    except Exception:
        logger.exception("Could not save task to weekly planner")
        await update_or_query.message.reply_text(
            "❌ Не удалось записать задачу в недельный планнер. Попробуй ещё раз.")
        return
    if time_str:
        add_calendar_event(owner, date_str, time_str, task, category)
    time_info = f" в {time_str}" if time_str else ""
    text = (f"✅ Записала!\n\n{cat_emoji} *{category.capitalize()}*\n"
            f"📌 {task}{time_info}\n📅 {date_str}")
    # Дату никто не называл — записала на сегодня по умолчанию, но
    # вдруг имелся в виду другой день: даём лёгкий способ поправить,
    # не заставляя отвечать на вопрос при каждой обычной задаче.
    keyboard = None
    if not date_given:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📅 Не сегодня, перенести", callback_data=f"evpost_{date_str}_{row_num}")]])
    await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

# ── Просмотр чужого расписания ("peek") ─────────────────────────
async def send_peek(update, target_owner, date_str):
    tasks  = [(rn, row) for rn, row in get_tasks_for_day(date_str, target_owner)
              if not (len(row) >= 5 and row[4] == "habit")]
    habits = get_habits_for_day(date_str, target_owner)
    goals  = [g for g in get_goals(target_owner) if g[5] != "✅"]
    cat_emoji = {"работа": "💼", "личное": "👤", "дом": "🏠"}

    text = f"👀 *У {target_owner} на {date_str}:*\n\n"
    if tasks:
        for _, row in tasks:
            status = row[3] if len(row) > 3 else "☐"
            text += f"{status} {row[1]}\n   {row[0]}\n"
    else:
        text += "Задач нет.\n"
    if habits:
        text += "\n📊 *Привычки:*\n"
        for _, row in habits:
            text += f"• {row[1]}: {row[2]}\n"
    if goals:
        text += "\n🎯 *Активные цели:*\n"
        for _, period, category, goal_text, _, _ in goals[:6]:
            text += f"• {cat_emoji.get(category, '📌')} {goal_text} ({period})\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Process text/voice ─────────────────────────────────────────
async def process_text(update, text, owner):
    user_id = str(update.effective_user.id)
    today   = datetime.now().strftime("%d.%m.%Y")
    try:
        parsed = parse_task(text, today, get_habit_list(), ALL_OWNERS)
    except Exception:
        logger.exception("Task parsing failed")
        await update.message.reply_text(
            "❌ Не удалось обработать задачу из-за ошибки сервиса. "
            "Попробуй ещё раз через минуту.\n"
            "Например: «Позвонить врачу, личное, завтра в 10:00»")
        return

    if parsed.get("type") == "peek":
        target = resolve_owner_name(parsed.get("target_user"))
        if not target:
            await update.message.reply_text(
                "🤔 Не поняла, о ком из участников спрашиваешь.")
            return
        date_str = parsed.get("date") or today
        await send_peek(update, target, date_str)
        return

    if parsed.get("type") == "question":
        await update.message.reply_text(
            "🤔 Похоже, это вопрос, а не задача — ничего не записала.\n"
            "Если хочешь добавить задачу, скажи её ещё раз попроще.")
        return

    if parsed.get("type") == "habit":
        date_str = parsed.get("date") or today
        try:
            add_task_to_sheet(owner, "", parsed.get("habit_name", ""),
                              parsed.get("habit_value", ""), date_str, "habit")
        except Exception:
            logger.exception("Could not save habit to weekly planner")
            await update.message.reply_text(
                "❌ Не смогла сопоставить привычку с планнером. Попробуй назвать её иначе.")
            return
        await update.message.reply_text(
            f"✅ Привычка: *{parsed.get('habit_name','')}* — {parsed.get('habit_value','')}",
            parse_mode="Markdown")
        return

    if parsed.get("type") == "delete":
        date_str = parsed.get("date") or today
        query    = parsed.get("task", "")
        matches  = find_matching_entries(date_str, owner, query)
        if not matches:
            await update.message.reply_text(
                f"🤔 Не нашла запись «{query}», чтобы удалить.")
            return
        if len(matches) == 1:
            kind, row_num, _, entry_text = matches[0]
            if kind == "goal":
                delete_goal(owner, row_num)
            else:
                delete_entry_from_sheet(date_str, owner, row_num)
            await update.message.reply_text(f"🗑 Удалила: {entry_text}")
            return
        keyboard = [[InlineKeyboardButton(f"🗑 {entry_text[:45]}",
                     callback_data=f"delpick_{kind}_{date_str}_{row_num}")]
                    for kind, row_num, _, entry_text in matches[:8]]
        await update.message.reply_text(
            "🤔 Нашла несколько похожих записей. Какую удалить?",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if parsed.get("type") == "goal":
        category  = parsed.get("category", "личное")
        period    = parsed.get("period") or "месяц"
        goal_text = parsed.get("task", "")
        if period not in GOAL_ROWS:
            period = "месяц"
        if category not in GOAL_ROWS[period]:
            category = "личное"
        try:
            add_goal(owner, period, category, goal_text)
        except Exception:
            logger.exception("Could not save goal")
            await update.message.reply_text(
                f"❌ Не удалось записать цель — похоже, все слоты на {period} в этой категории заняты.")
            return
        cat_emoji = {"работа": "💼", "личное": "👤", "дом": "🏠"}.get(category, "📌")
        await update.message.reply_text(
            f"🎯 Цель на {period} записана!\n\n{cat_emoji} *{category.capitalize()}*\n📌 {goal_text}",
            parse_mode="Markdown")
        return

    if parsed.get("type") in ("task", "note"):
        if "category" in parsed.get("missing", []):
            user_states[user_id] = parsed
            keyboard = [[
                InlineKeyboardButton("💼 Работа", callback_data="cat_работа"),
                InlineKeyboardButton("👤 Личное", callback_data="cat_личное"),
                InlineKeyboardButton("🏠 Дом",    callback_data="cat_дом"),
            ]]
            await update.message.reply_text(
                f"📌 *{parsed.get('task','')}*\n\nКакая категория?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown")
            return
        await save_task(update, parsed, today, owner)
        return

    await update.message.reply_text(
        "🎤 Отправь голосовое или напиши задачу.\n"
        "Например: «Купить продукты, дом, завтра»")

# ── Handlers ───────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text(
            f"⛔ Доступ закрыт.\n\nТвой Telegram ID: `{update.effective_user.id}`\n"
            "Перешли его тому, кто настраивает бота, чтобы получить доступ.",
            parse_mode="Markdown")
        return
    owner   = owner_for(update)
    user_id = str(update.effective_user.id)
    # /start — ещё и способ выбраться, если бот завис в каком-то режиме
    # записи (кнопки "⬅️ Выйти" не видно) — сбрасываем всё для этого
    # пользователя и показываем обычную клавиатуру заново.
    post_idea_mode_users.discard(user_id)
    general_notes_mode_users.discard(user_id)
    user_states.pop(user_id, None)
    pending_health.pop(user_id, None)
    pending_postpone.pop(user_id, None)
    await update.message.reply_text(
        f"👋 Привет, {display_name_for(update)}! Я твой личный планнер.\n\n"
        "Говори или пиши — разберу, что это: задача, привычка, цель или заметка. "
        "Для остального — кнопки внизу.\n\n"
        "♻️ Если что-то зависнет или пойдёт не так — пришли /start ещё раз, всё сброшу.",
        reply_markup=main_keyboard_for(owner))

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    owner   = owner_for(update)
    user_id = str(update.effective_user.id)
    voice = update.message.voice
    file  = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name
    await update.message.reply_text("🎤 Расшифровываю...")
    try:
        text = transcribe_voice(tmp_path)
        os.unlink(tmp_path)
        await update.message.reply_text(f"📝 Услышала: _{text}_", parse_mode="Markdown")
        if user_id in post_idea_mode_users:
            add_idea_to_sheet(owner, text)
            await update.message.reply_text("💡 Записала как идею для поста.")
            return
        if user_id in general_notes_mode_users:
            add_note_to_sheet(owner, text)
            await update.message.reply_text("📝 Записала заметку.")
            return
        await process_text(update, text, owner)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Не удалось расшифровать. Попробуй ещё раз.")

def save_health_data(owner, date_str, data):
    """Пишет распознанные со скриншота значения в привычки за date_str.
    Возвращает список строк с тем, что реально записалось (что не
    распозналось на конкретном скрине или привычка была переименована/
    удалена в настройках — просто пропускается)."""
    def save_habit(keyword, value):
        habit_name = find_habit_name(keyword)
        if not habit_name:
            return None
        add_task_to_sheet(owner, "", habit_name, value, date_str, "habit")
        return f"{habit_name}: {value}"

    saved = []
    if data.get("vstala"):
        line = save_habit("Встала", data["vstala"])
        if line: saved.append(line)
    if data.get("legla"):
        line = save_habit("Легла", data["legla"])
        if line: saved.append(line)
    if data.get("son_dlitelnost"):
        line = save_habit("сна", data["son_dlitelnost"])
        if line: saved.append(line)

    walk_parts = []
    if data.get("shagi"):
        walk_parts.append(f"{data['shagi']} шагов")
    if data.get("km"):
        walk_parts.append(f"{data['km']} км")
    if data.get("minuty"):
        walk_parts.append(f"{data['minuty']} мин")
    if walk_parts:
        line = save_habit("Прогулка", ", ".join(walk_parts))
        if line: saved.append(line)
    return saved

async def handle_health_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Любой присланный скриншот: если это "Здоровье" (сон/шаги) — данные
    уходят в привычки, дата берётся с самого скриншота (если её там видно,
    иначе бот спрашивает — иначе данные за прошлый день улетают не в тот
    столбец). Если скриншот не про здоровье (билет, переписка, документ) —
    весь видимый текст сохраняется как обычная заметка в лист "📝 Заметки",
    чтобы потом её можно было причесать в задачу/план через notes-structurer."""
    if not is_allowed(update): return
    owner   = owner_for(update)
    user_id = str(update.effective_user.id)
    photo   = update.message.photo[-1]
    file    = await context.bot.get_file(photo.file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name
    await update.message.reply_text("📸 Разбираю скриншот...")
    try:
        data = extract_screenshot_data(tmp_path)
    except Exception:
        logger.exception("Screenshot parsing failed")
        await update.message.reply_text("❌ Не удалось распознать скриншот. Попробуй ещё раз.")
        return
    finally:
        os.unlink(tmp_path)

    has_health_data = any(data.get(k) for k in ("vstala", "legla", "son_dlitelnost", "shagi", "km", "minuty"))

    if not has_health_data:
        text = (data.get("tekst") or "").strip()
        if not text:
            await update.message.reply_text("🤔 Не смогла разобрать ничего полезного на этом скриншоте.")
            return
        try:
            add_note_to_sheet(owner, text)
        except Exception:
            logger.exception("Could not save screenshot text to notes")
            await update.message.reply_text("❌ Распознала текст, но не смогла записать в заметки.")
            return
        await update.message.reply_text(f"📝 Записала текст со скриншота в заметки:\n\n{text}")
        return

    date_str = parse_flexible_date(data.get("data_na_ekrane"), datetime.now())
    if not date_str:
        pending_health[user_id] = data
        await update.message.reply_text(
            "📅 Не поняла, за какой день этот скриншот — на нём не видно даты. "
            "Напиши, например «сегодня», «вчера» или 13.09.")
        return

    try:
        saved = save_health_data(owner, date_str, data)
    except Exception:
        logger.exception("Could not save health data to weekly planner")
        await update.message.reply_text("❌ Распознала скриншот, но не смогла записать в планнер.")
        return
    await update.message.reply_text(f"✅ Записала на {date_str} со скриншота:\n\n" + "\n".join(saved))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    user_id = str(update.effective_user.id)
    owner   = owner_for(update)
    text    = (update.message.text or "").strip()
    today   = datetime.now().strftime("%d.%m.%Y")

    # Ждём уточнение даты для скриншота "Здоровья" — этот ответ не должен
    # попасть ни в режимы заметок, ни в разбор задачи.
    if user_id in pending_health:
        date_str = parse_flexible_date(text, datetime.now())
        if not date_str:
            await update.message.reply_text(
                "🤔 Не поняла дату. Напиши, например «сегодня», «вчера» или 13.09.")
            return
        data = pending_health.pop(user_id)
        try:
            saved = save_health_data(owner, date_str, data)
        except Exception:
            logger.exception("Could not save health data to weekly planner")
            await update.message.reply_text("❌ Не смогла записать в планнер.")
            return
        await update.message.reply_text(f"✅ Записала на {date_str} со скриншота:\n\n" + "\n".join(saved))
        return

    # Ждём дату для переноса задачи из вечернего чек-листа.
    if user_id in pending_postpone:
        date_str = parse_flexible_date(text, datetime.now())
        if not date_str:
            await update.message.reply_text(
                "🤔 Не поняла дату. Напиши, например «20.09» или «завтра».")
            return
        entry = pending_postpone.pop(user_id)
        try:
            moved = move_task_to_date(owner, entry["date_str"], entry["row_num"], date_str)
        except Exception:
            logger.exception("Could not move task to new date")
            await update.message.reply_text("❌ Не удалось перенести задачу.")
            return
        if moved:
            await update.message.reply_text(f"→ Перенесла «{moved}» на {date_str}.")
        else:
            await update.message.reply_text("🤔 Не нашла эту задачу — может, уже перенесена.")
        return

    # Уже в одном из режимов записи — выходим только по явной кнопке
    # «Выйти», а любой другой текст сохраняем как есть (даже если он
    # случайно содержит слово "меню" или название другой кнопки).
    if user_id in post_idea_mode_users or user_id in general_notes_mode_users:
        if "Выйти" in text:
            post_idea_mode_users.discard(user_id)
            general_notes_mode_users.discard(user_id)
            await update.message.reply_text("Вышли из режима записи.", reply_markup=main_keyboard_for(owner))
            return
        if user_id in post_idea_mode_users:
            add_idea_to_sheet(owner, text)
            await update.message.reply_text(f"💡 Записала как идею для поста: _{text}_", parse_mode="Markdown")
        else:
            add_note_to_sheet(owner, text)
            await update.message.reply_text(f"📝 Записала: _{text}_", parse_mode="Markdown")
        return

    if "Идеи для постов" in text and owner in BLOG_OWNERS:
        post_idea_mode_users.add(user_id)
        await update.message.reply_text(
            "💡 Режим «Идеи для постов» включён. Говори или пиши — сохраню как материал для постов.\n"
            "Чтобы выйти, нажми «⬅️ Выйти».",
            reply_markup=CAPTURE_KEYBOARD,
        )
        return

    if "Заметки" in text:
        general_notes_mode_users.add(user_id)
        await update.message.reply_text(
            "📝 Режим «Заметки» включён. Накидывай сюда любой сумбур — план, черновик "
            "ответа на письмо, что угодно. Потом попросишь причесать в нужную форму.\n"
            "Чтобы выйти, нажми «⬅️ Выйти».",
            reply_markup=CAPTURE_KEYBOARD,
        )
        return

    # Handle persistent keyboard buttons (match by keyword, since some
    # Telegram clients add/drop emoji variation selectors on the label)
    if "Меню" in text:
        await menu_command(update, context)
        return
    if "Дашборд" in text or "Таблица" in text:
        await table_command(update, context)
        return
    if "Сегодня" in text:
        await today_tasks(update, context)
        return

    if user_id in user_states and user_states[user_id].get("awaiting_date"):
        date_str = parse_flexible_date(text, datetime.now())
        if not date_str:
            await update.message.reply_text(
                "🤔 Не поняла дату. Напиши, например «20.09» или «завтра».")
            return
        user_states[user_id]["date"] = date_str
        user_states[user_id].pop("awaiting_date")
        parsed = user_states.pop(user_id)
        await save_task(update, parsed, today, owner)
        return
    await process_text(update, text, owner)

async def _remove_checklist_row(query, date_str, row_num):
    """Убирает из клавиатуры вечернего чек-листа строку кнопок,
    относящуюся к этой задаче (её уже отметили сделанной или перенесли),
    оставляя кнопки остальных задач как есть."""
    suffix = f"_{date_str}_{row_num}"
    kb = query.message.reply_markup
    if not kb:
        return
    new_rows = [row for row in kb.inline_keyboard
                if not (row and row[0].callback_data and row[0].callback_data.endswith(suffix))]
    await query.edit_message_reply_markup(InlineKeyboardMarkup(new_rows) if new_rows else None)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    owner   = owner_for(update)
    data    = query.data
    today   = datetime.now().strftime("%d.%m.%Y")
    if data.startswith("cat_"):
        category = data.replace("cat_", "")
        if user_id in user_states:
            user_states[user_id]["category"] = category
            parsed = user_states.pop(user_id)
            await save_task(query, parsed, today, owner)
    elif data == "menu_today":
        await today_tasks(update, context)
    elif data == "menu_done":
        await done_command(update, context)
    elif data == "menu_habits":
        await habits_command(update, context)
    elif data == "menu_goals":
        await goals_command(update, context)
    elif data.startswith("done_"):
        parts    = data.split("_")
        date_str = parts[1]
        row_num  = int(parts[2])
        mark_task_done(date_str, owner, row_num)
        await query.edit_message_text(query.message.text + "\n\n✅ Готово!")
    elif data.startswith("goaldone_"):
        row_num = int(data.split("_")[1])
        mark_goal_done(owner, row_num)
        await query.edit_message_text(query.message.text + "\n\n✅ Цель выполнена!")
    elif data.startswith("delpick_"):
        parts    = data.split("_")
        kind     = parts[1]
        date_str = parts[2]
        row_num  = int(parts[3])
        if kind == "goal":
            delete_goal(owner, row_num)
        else:
            delete_entry_from_sheet(date_str, owner, row_num)
        await query.edit_message_text("🗑 Запись удалена.")
    elif data.startswith("evdone_"):
        _, date_str, row_str = data.split("_", 2)
        row_num = int(row_str)
        mark_task_done(date_str, owner, row_num)
        await _remove_checklist_row(query, date_str, row_num)
        await query.message.reply_text("✅ Отметила как сделано.")
    elif data.startswith("evpost_"):
        _, date_str, row_str = data.split("_", 2)
        row_num = int(row_str)
        new_keyboard = []
        for row in query.message.reply_markup.inline_keyboard:
            if row and row[0].callback_data == data:
                new_keyboard.append([
                    InlineKeyboardButton("📅 Завтра", callback_data=f"evtmrw_{date_str}_{row_num}"),
                    InlineKeyboardButton("✏️ Другой день", callback_data=f"evcust_{date_str}_{row_num}"),
                ])
            else:
                new_keyboard.append(row)
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_keyboard))
    elif data.startswith("evtmrw_"):
        _, date_str, row_str = data.split("_", 2)
        row_num  = int(row_str)
        tomorrow = (datetime.strptime(date_str, "%d.%m.%Y") + timedelta(days=1)).strftime("%d.%m.%Y")
        moved = move_task_to_date(owner, date_str, row_num, tomorrow)
        await _remove_checklist_row(query, date_str, row_num)
        if moved:
            await query.message.reply_text(f"→ Перенесла «{moved}» на {tomorrow}.")
        else:
            await query.message.reply_text("🤔 Не нашла эту задачу — может, уже перенесена.")
    elif data.startswith("evcust_"):
        _, date_str, row_str = data.split("_", 2)
        row_num = int(row_str)
        pending_postpone[user_id] = {"date_str": date_str, "row_num": row_num}
        await _remove_checklist_row(query, date_str, row_num)
        await query.message.reply_text("📅 На какой день перенести? Напиши, например «20.09» или «завтра».")

async def today_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    owner = owner_for(update)
    today = datetime.now().strftime("%d.%m.%Y")
    tasks = get_tasks_for_day(today, owner)
    if not tasks:
        await update.effective_message.reply_text(f"📅 На {today} задач нет.\n\nНаговори что-нибудь! 🎤")
        return
    text = f"📅 *Задачи на {today}:*\n\n"
    for _, row in tasks:
        if len(row) >= 5 and row[4] == "habit": continue
        status    = row[3] if len(row) > 3 else "☐"
        t         = row[2] if len(row) > 2 and row[2] else ""
        time_info = f" _{t}_" if t else ""
        text += f"{status} {row[1]}{time_info}\n   {row[0]}\n\n"
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    owner = owner_for(update)
    today = datetime.now().strftime("%d.%m.%Y")
    tasks = [(rn, row) for rn, row in get_tasks_for_day(today, owner)
             if (len(row) < 5 or row[4] != "habit") and
                (len(row) < 4 or row[3] != "✅")]
    if not tasks:
        await update.effective_message.reply_text("🎉 Все задачи выполнены!")
        return
    keyboard = [[InlineKeyboardButton(f"☐ {row[1][:45]}",
                 callback_data=f"done_{today}_{rn}")] for rn, row in tasks if row[1]]
    await update.effective_message.reply_text(
        "✅ *Что выполнено?*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown")

async def habits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    owner  = owner_for(update)
    today  = datetime.now().strftime("%d.%m.%Y")
    habits = get_habits_for_day(today, owner)
    if not habits:
        await update.effective_message.reply_text(
            f"📊 *Привычки на {today}*\n\nНичего не записано.\n\n"
            "Скажи голосом: «Встала в 7:30» или «Витамины утром приняла» 🎤",
            parse_mode="Markdown")
        return
    text = f"📊 *Привычки на {today}:*\n\n"
    for _, row in habits:
        text += f"• {row[1]}: *{row[2]}*\n"
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def goals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    owner = owner_for(update)
    goals = get_goals(owner)
    if not goals:
        await update.effective_message.reply_text(
            "🎯 Целей пока нет.\n\n"
            "Скажи: «Цель на месяц — выучить английский, личное»")
        return
    cat_emoji = {"работа": "💼", "личное": "👤", "дом": "🏠"}
    text = "🎯 *Твои цели:*\n\n"
    for period in ("месяц", "год"):
        period_goals = [g for g in goals if g[1] == period]
        if not period_goals:
            continue
        text += f"— *На {period}* —\n"
        for row_num, _, category, goal_text, deadline, status in period_goals:
            deadline_info = f" _(до {deadline})_" if deadline else ""
            text += f"{status} {cat_emoji.get(category, '📌')} {goal_text}{deadline_info}\n"
        text += "\n"
    active = [g for g in goals if g[5] != "✅"]
    keyboard = [[InlineKeyboardButton(f"✅ {g[3][:35]}", callback_data=f"goaldone_{g[0]}")]
                for g in active[:8]]
    await update.effective_message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)

async def table_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    owner = owner_for(update)
    keyboard = []
    dashboard_url = DASHBOARD_URLS.get(owner)
    if dashboard_url:
        keyboard.append([InlineKeyboardButton("📊 Дашборд (неделя)", url=dashboard_url)])
    keyboard.append([InlineKeyboardButton("🗓 Открыть таблицу", url=SPREADSHEET_URL)])
    await update.message.reply_text(
        "📊 *Твой планнер:*\n\nДашборд — наглядный снимок недели (только твои задачи). "
        "Таблица — все данные как есть.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    owner = owner_for(update)
    keyboard = [
        [InlineKeyboardButton("📅 Задачи на сегодня", callback_data="menu_today")],
        [InlineKeyboardButton("✅ Отметить выполненное", callback_data="menu_done")],
        [InlineKeyboardButton("📊 Привычки", callback_data="menu_habits")],
        [InlineKeyboardButton("🎯 Цели", callback_data="menu_goals")],
    ]
    dashboard_url = DASHBOARD_URLS.get(owner)
    if dashboard_url:
        keyboard.append([InlineKeyboardButton("📊 Дашборд (неделя)", url=dashboard_url)])
    keyboard.append([InlineKeyboardButton("🗓 Открыть таблицу", url=SPREADSHEET_URL)])
    await update.message.reply_text(
        "📋 *Главное меню:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown")

# ── Напоминания о задачах с указанным временем ──────────────────
REMINDER_CHECK_INTERVAL = 300  # секунд между проверками
REMINDER_WINDOW_MINUTES = 5    # ширина окна срабатывания (под интервал проверки)
REMINDER_RULES = (
    # (на сколько дней вперёд смотреть, за сколько минут напомнить, метка, текст)
    (0, 60,        "1h", "⏰ Через час"),
    (1, 24 * 60,   "1d", "📅 Завтра"),
)
sent_reminders = set()  # (owner, date_str, row_num, метка) — чтобы не слать повторно

def split_task_time(text):
    """Отделяет время от текста задачи вида "Позвонить врачу — 14:00".
    Возвращает (текст_без_времени, "14:00") или (text, None), если
    времени нет."""
    m = re.search(r"\s—\s(\d{1,2}:\d{2})$", text)
    if not m:
        return text, None
    return text[:m.start()].strip(), m.group(1)

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Каждые REMINDER_CHECK_INTERVAL секунд смотрит задачи с указанным
    временем на сегодня и на завтра и шлёт напоминание за час и за сутки
    до момента задачи (владельцу этой задачи, не всем)."""
    now = datetime.now()
    owner_to_id = {}
    for telegram_id, name in USER_NAMES.items():
        owner_to_id.setdefault(name, telegram_id)

    for owner in ALL_OWNERS:
        chat_id = owner_to_id.get(owner)
        if not chat_id:
            continue
        for days_ahead, minutes_before, kind, prefix in REMINDER_RULES:
            date_str = (now + timedelta(days=days_ahead)).strftime("%d.%m.%Y")
            try:
                tasks = get_tasks_for_day(date_str, owner)
            except Exception:
                logger.exception(f"Reminder check failed for {owner} {date_str}")
                continue
            for row_num, row in tasks:
                if len(row) >= 5 and row[4] == "habit":
                    continue
                if (row[3] if len(row) > 3 else "☐") == "✅":
                    continue
                task_text, time_str = split_task_time(row[1])
                if not time_str:
                    continue
                try:
                    target_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
                except ValueError:
                    continue
                delta_minutes = (target_dt - now).total_seconds() / 60
                if not (minutes_before - REMINDER_WINDOW_MINUTES <= delta_minutes <= minutes_before):
                    continue
                key = (owner, date_str, row_num, kind)
                if key in sent_reminders:
                    continue
                sent_reminders.add(key)
                try:
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"{prefix}: {task_text} в {time_str} ({date_str})")
                except Exception:
                    logger.exception(f"Could not send reminder to {owner}")

# ── Ночной агент: причёсывает "Заметки" в результат каждый вечер ─
# Сейчас — через Groq (нет платного ключа Anthropic API, см. AGENTS.md).
# Вся работа с моделью — в этой одной функции: когда появится ключ,
# меняется реализация только structure_notes_for_owner, остальное — нет.
NIGHT_AGENT_MODEL = "openai/gpt-oss-120b"
MAX_NOTES_PER_OWNER_PER_NIGHT = 40
TELEGRAM_MSG_LIMIT = 4096

def structure_notes_for_owner(owner, notes):
    """Причёсывает сумбурные заметки владельца в результат нужной формы.
    Форму (план/письмо/список/что угодно) выбирает сама модель по смыслу.
    Возвращает готовый текст или None, если модель ничего не вернула."""
    notes_block = "\n".join(f"{i}. {text}" for i, text in enumerate(notes, 1))
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={**GROQ_HEADERS, "Content-Type": "application/json"},
        json={
            "model": NIGHT_AGENT_MODEL,
            "messages": [
                {"role": "system", "content": (
                    "Ты помощник, который каждый вечер причёсывает сумбурные "
                    "голосовые заметки в аккуратный результат. Сам выбери форму "
                    "по смыслу заметок — план, черновик письма/сообщения, список "
                    "дел или что-то ещё — не используй один и тот же шаблон "
                    "всегда. Не выдумывай факты, которых нет в заметках. Если "
                    "что-то похоже на плохо расслышанное слово (например, "
                    "странное название) — не утверждай уверенно, отдельной "
                    "строкой напиши, что тут не уверена и какое исходное слово "
                    "могло иметься в виду. Пиши по-русски, обращайся на «ты»."
                )},
                {"role": "user", "content": f"Заметки {owner} за сегодня:\n{notes_block}"},
            ],
            "max_tokens": 3000,
            "temperature": 0.3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    return content or None

def split_into_telegram_chunks(text, limit=TELEGRAM_MSG_LIMIT):
    """Режет текст на сообщения по границам абзацев; абзац длиннее лимита
    режется жёстко по символам."""
    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
        else:
            for start in range(0, len(paragraph), limit):
                chunks.append(paragraph[start:start + limit])
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]

async def run_nightly_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежевечернее задание: у каждого владельца свои новые заметки
    причёсываются в результат и уходят ему в Telegram."""
    by_owner = get_new_notes_by_owner()
    if not by_owner:
        return
    owner_to_id = {}
    for telegram_id, name in USER_NAMES.items():
        owner_to_id.setdefault(name, telegram_id)

    for owner, entries in by_owner.items():
        taken = entries[:MAX_NOTES_PER_OWNER_PER_NIGHT]
        texts = [text for (_, _, text) in taken]
        rows  = [row for (_, row, _) in taken]
        chat_id = owner_to_id.get(owner)
        if not chat_id:
            logger.warning(f"Ночной агент: не найден Telegram ID для {owner}")
            continue
        try:
            result = structure_notes_for_owner(owner, texts)
        except Exception:
            logger.exception(f"Ночной агент: сбой Groq для {owner}")
            continue
        if not result:
            logger.warning(f"Ночной агент: пустой результат для {owner}")
            continue
        try:
            for chunk in split_into_telegram_chunks(f"🌙 Вечерний разбор заметок:\n\n{result}"):
                await context.bot.send_message(chat_id=int(chat_id), text=chunk)
        except Exception:
            logger.exception(f"Ночной агент: не удалось отправить результат {owner}")
            continue
        mark_notes_processed(rows)

async def run_evening_checklist(context: ContextTypes.DEFAULT_TYPE):
    """Вечерний чек-лист за сегодня: задачи (с кнопками "Сделано" /
    "Перенести" у невыполненных) и привычки (только информационно —
    привычку на завтра не перенесёшь, не отмечена — значит не сделана)."""
    today = datetime.now().strftime("%d.%m.%Y")
    owner_to_id = {}
    for telegram_id, name in USER_NAMES.items():
        owner_to_id.setdefault(name, telegram_id)

    habit_list = get_habit_list()
    for owner in ALL_OWNERS:
        chat_id = owner_to_id.get(owner)
        if not chat_id:
            continue
        try:
            tasks = [(rn, row) for rn, row in get_tasks_for_day(today, owner)
                     if len(row) < 5 or row[4] != "habit"]
        except Exception:
            logger.exception(f"Вечерний чек-лист: не удалось прочитать задачи {owner}")
            tasks = []
        try:
            habits_today = dict((row[1], row[2]) for _, row in get_habits_for_day(today, owner))
        except Exception:
            logger.exception(f"Вечерний чек-лист: не удалось прочитать привычки {owner}")
            habits_today = {}

        if not tasks and not habit_list:
            continue

        lines = [f"🌙 *Вечерний чек-лист, {today}:*", "", "📋 *Задачи:*"]
        keyboard = []
        if not tasks:
            lines.append("_Задач нет._")
        for row_num, row in tasks:
            status = row[3] if len(row) > 3 else "☐"
            lines.append(f"{status} {row[1]}")
            if status != "✅":
                keyboard.append([
                    InlineKeyboardButton("✅ Сделано", callback_data=f"evdone_{today}_{row_num}"),
                    InlineKeyboardButton("→ Перенести", callback_data=f"evpost_{today}_{row_num}"),
                ])
        if habit_list:
            lines.append("")
            lines.append("😴 *Привычки:*")
            for name in habit_list:
                if name in habits_today:
                    lines.append(f"✅ {name} — {habits_today[name]}")
                else:
                    lines.append(f"➖ {name} — нет данных")

        try:
            await context.bot.send_message(
                chat_id=int(chat_id), text="\n".join(lines), parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
        except Exception:
            logger.exception(f"Вечерний чек-лист: не удалось отправить {owner}")

# ── Веб-дашборд (свой сервер на Railway, не claude.ai Artifact) ──
# Раньше дашборд публиковался как Artifact на claude.ai — оказалось,
# что для просмотра нужен логин в аккаунт Claude, который иногда
# уходит на модерацию ("account on hold"), плюс сама страница
# обновлялась только по запросу в чате. Здесь то же самое, но отдаётся
# прямо с сервера бота: свежие данные, без логина, без claude.ai.
OWNER_DASHBOARD_STYLE = {
    "Оля": {"accent": "#1F7A6C", "accent_bg": "#E4F1EE", "accent_dark": "#4FBBA4", "accent_bg_dark": "#1D3530"},
    "Мама": {"accent": "#B8722E", "accent_bg": "#F6ECDD", "accent_dark": "#E0A45E", "accent_bg_dark": "#3A2E1C"},
}
DEFAULT_DASHBOARD_STYLE = {"accent": "#3B6EA8", "accent_bg": "#E4EBF6", "accent_dark": "#6FA0DE", "accent_bg_dark": "#1E2C40"}

# token в конце ссылки DASHBOARD_URLS -> владелец (никакой отдельной
# переменной не нужно: ссылка вида .../d/<token> уже содержит его)
DASHBOARD_TOKEN_TO_OWNER = {url.rstrip("/").rsplit("/", 1)[-1]: owner for owner, url in DASHBOARD_URLS.items()}
DASHBOARD_CACHE = {}  # owner -> готовый HTML

def display_name_for_owner(owner):
    for uid, name in USER_NAMES.items():
        if name == owner:
            return DISPLAY_NAMES.get(uid, owner)
    return owner

def build_dashboard_data(owner):
    """Синхронно (gspread) собирает те же данные, что раньше собирались
    вручную для Artifact: задачи на неделю, привычки, цели, очередь
    заметок/идей. Вызывать через asyncio.to_thread — блокирующий I/O."""
    today_dt   = datetime.now()
    today_str  = today_dt.strftime("%d.%m.%Y")
    monday     = week_start(today_str)
    week_dates = [(monday + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(7)]
    weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    sheet      = get_sheet()
    habit_list = get_habit_list()
    week   = {d: [] for d in week_dates}
    habits = {name: {} for name in habit_list}
    goals  = []

    try:
        ws = sheet.worksheet(week_sheet_name(today_str, owner))
        for d in week_dates:
            column = day_column(d)
            values = ws.get(f"{column}5:{column}24")
            day_tasks = []
            for category, (start_row, end_row, label) in CATEGORY_ROWS.items():
                for row_num in range(start_row, end_row + 1):
                    offset = row_num - 5
                    value = values[offset][0] if offset < len(values) and values[offset] else ""
                    if value:
                        status = "✅" if value.startswith("✅") else "☐"
                        day_tasks.append({"category": label, "text": value.lstrip("☐✅ ").strip(), "status": status})
            week[d] = day_tasks
        habit_values = ws.get(f"A{HABIT_START_ROW}:H{HABIT_START_ROW + HABIT_MAX_ROWS - 1}")
        for i, name in enumerate(habit_list):
            row = habit_values[i] if i < len(habit_values) else []
            for j, d in enumerate(week_dates):
                val = row[1 + j] if 1 + j < len(row) else ""
                if val:
                    habits[name][d] = val
    except gspread.exceptions.WorksheetNotFound:
        pass

    for row_num, period, category, goal_text, deadline, status in get_goals(owner):
        goals.append({"period": period, "category": category, "text": goal_text, "deadline": deadline, "status": status})

    def read_log(sheet_title):
        out = []
        try:
            ws2 = sheet.worksheet(sheet_title)
        except gspread.exceptions.WorksheetNotFound:
            return out
        for row in ws2.get_all_values()[1:]:
            date, time_str, row_owner, text, status = (row + [""] * 5)[:5]
            if row_owner == owner and status.strip() == "новая" and text.strip():
                out.append({"date": date, "time": time_str, "text": text})
        return out

    return {
        "today": today_str, "week_dates": week_dates, "weekday_names": weekday_names,
        "snapshot_at": today_dt.strftime("%d.%m.%Y, %H:%M"),
        "week": week, "habits": habits, "goals": goals,
        "notes": read_log(NOTES_SHEET), "ideas": read_log(IDEAS_SHEET),
    }

DASHBOARD_PAGE_TEMPLATE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
  :root {
    --bg: #EEF0F2; --surface: #FFFFFF; --ink: #1B2430; --ink-muted: #626B79; --ink-faint: #9AA2AE;
    --line: #DBDFE4; --accent: @@ACCENT@@; --accent-bg: @@ACCENT_BG@@; --today: #C1502E; --done: #9AA2AE;
    --shadow: 0 1px 2px rgba(27,36,48,.06), 0 8px 24px rgba(27,36,48,.05);
    --font-display: 'Fraunces', Georgia, serif;
    --font-body: 'IBM Plex Sans', system-ui, -apple-system, sans-serif;
    --font-mono: 'IBM Plex Mono', ui-monospace, 'SFMono-Regular', monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #14181F; --surface: #1B212B; --ink: #EDEFF2; --ink-muted: #9AA4B2; --ink-faint: #6B7482;
      --line: #2A313C; --accent: @@ACCENT_DARK@@; --accent-bg: @@ACCENT_BG_DARK@@; --today: #E07A54; --done: #6B7482;
      --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.35);
    }
  }
  :root[data-theme="dark"] {
    --bg: #14181F; --surface: #1B212B; --ink: #EDEFF2; --ink-muted: #9AA4B2; --ink-faint: #6B7482;
    --line: #2A313C; --accent: @@ACCENT_DARK@@; --accent-bg: @@ACCENT_BG_DARK@@; --today: #E07A54; --done: #6B7482;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.35);
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--font-body); padding: 28px 20px 48px; }
  .wrap { max-width: 640px; margin: 0 auto; display: flex; flex-direction: column; gap: 22px; }
  .eyebrow { font-family: var(--font-mono); font-size: 11px; letter-spacing: .12em; color: var(--ink-faint); text-transform: uppercase; }
  h1 { font-family: var(--font-display); font-weight: 600; font-size: clamp(26px, 5vw, 34px); margin: 4px 0 6px; text-wrap: balance; }
  .meta { font-size: 13px; color: var(--ink-muted); margin: 0; }
  .meta a { color: var(--accent); text-decoration: none; font-weight: 500; }
  .meta a:hover { text-decoration: underline; }
  .card { background: var(--surface); border-radius: 14px; box-shadow: var(--shadow); position: relative; padding: 22px 22px 26px 30px; overflow: hidden; }
  .card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 14px; background-image: radial-gradient(circle, var(--bg) 2.5px, transparent 2.6px); background-size: 14px 22px; background-position: 0 6px; border-right: 1px dashed var(--line); }
  .block-title { font-family: var(--font-display); font-weight: 600; font-size: 18px; margin: 0 0 14px; display: flex; align-items: baseline; gap: 8px; }
  .block-title .hint { font-family: var(--font-body); font-weight: 400; font-size: 12px; color: var(--ink-faint); }
  nav.week-strip { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 4px; margin-bottom: 20px; border-bottom: 1px solid var(--line); }
  .day-tab { font-family: var(--font-body); background: none; border: none; border-bottom: 2px solid transparent; border-radius: 8px 8px 0 0; padding: 8px 10px 10px; min-width: 52px; cursor: pointer; display: flex; flex-direction: column; align-items: center; gap: 3px; color: var(--ink-muted); }
  .day-tab:hover { background: var(--bg); }
  .day-tab .dow { font-family: var(--font-mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
  .day-tab .dnum { font-family: var(--font-display); font-weight: 600; font-size: 18px; color: var(--ink); font-variant-numeric: tabular-nums; }
  .day-tab .count { font-family: var(--font-mono); font-size: 10px; color: var(--ink-faint); min-height: 12px; }
  .day-tab[aria-selected="true"] { border-bottom-color: var(--today); background: var(--bg); }
  .day-tab[aria-selected="true"] .dnum { color: var(--today); }
  .day-tab.is-today .dow { color: var(--today); }
  .group { margin-bottom: 16px; }
  .group:last-child { margin-bottom: 0; }
  .group-label { font-family: var(--font-mono); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--accent); background: var(--accent-bg); display: inline-block; padding: 2px 8px; border-radius: 5px; margin-bottom: 8px; }
  ul.tasks { list-style: none; margin: 0; padding: 0; }
  ul.tasks li { display: flex; gap: 8px; padding: 7px 0; border-top: 1px solid var(--line); font-size: 14.5px; line-height: 1.45; overflow-wrap: anywhere; }
  ul.tasks li:first-child { border-top: none; }
  ul.tasks li .box { font-family: var(--font-mono); flex-shrink: 0; color: var(--ink-faint); }
  ul.tasks li.done { color: var(--done); text-decoration: line-through; }
  ul.tasks li.done .box { color: var(--done); }
  .empty { color: var(--ink-faint); font-size: 13.5px; font-style: italic; padding: 6px 0; }
  .habit-scroll { overflow-x: auto; }
  table.habit-table { border-collapse: collapse; width: 100%; min-width: 480px; font-size: 13px; }
  table.habit-table th, table.habit-table td { text-align: center; padding: 7px 6px; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
  table.habit-table th { font-family: var(--font-mono); font-weight: 500; color: var(--ink-faint); font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase; }
  table.habit-table td:first-child, table.habit-table th:first-child { text-align: left; font-family: var(--font-body); color: var(--ink); white-space: nowrap; padding-right: 14px; }
  table.habit-table td.today-col, table.habit-table th.today-col { background: var(--accent-bg); border-radius: 4px; }
  table.habit-table td.val { color: var(--ink-muted); font-family: var(--font-mono); font-size: 12px; }
  table.habit-table td.val.filled { color: var(--ink); }
  table.habit-table td.dash { color: var(--ink-faint); }
  .goal-group { margin-bottom: 14px; }
  .goal-group:last-child { margin-bottom: 0; }
  .goal-period { font-family: var(--font-mono); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-faint); margin: 18px 0 8px; }
  .goal-period:first-child { margin-top: 0; }
  ul.goals { list-style: none; margin: 0 0 10px; padding: 0; }
  ul.goals li { display: flex; gap: 8px; padding: 6px 0; border-top: 1px solid var(--line); font-size: 14.5px; line-height: 1.4; }
  ul.goals li:first-child { border-top: none; }
  ul.goals li .box { font-family: var(--font-mono); flex-shrink: 0; color: var(--ink-faint); }
  ul.goals li .deadline { color: var(--ink-faint); font-size: 12px; white-space: nowrap; }
  ul.goals li.done { color: var(--done); text-decoration: line-through; }
  .queue-item { padding: 10px 0; border-top: 1px solid var(--line); }
  .queue-item:first-child { border-top: none; }
  .queue-item .qmeta { font-family: var(--font-mono); font-size: 11px; color: var(--ink-faint); margin-bottom: 3px; }
  .queue-item .qtext { font-size: 14px; line-height: 1.45; color: var(--ink); }
  footer { font-size: 12px; color: var(--ink-faint); text-align: center; }
</style></head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">@@EYEBROW@@</div>
    <h1 id="page-title">Неделя</h1>
    <p class="meta" id="snapshot-meta"></p>
  </header>
  <div class="card">
    <h2 class="block-title">📋 Задачи</h2>
    <nav class="week-strip" id="week-strip" role="tablist" aria-label="Дни недели"></nav>
    <div id="content"></div>
  </div>
  <div class="card" id="habits-card">
    <h2 class="block-title">😴 Привычки недели</h2>
    <div class="habit-scroll"><table class="habit-table" id="habit-table"></table></div>
  </div>
  <div class="card" id="goals-card">
    <h2 class="block-title">🎯 Цели</h2>
    <div id="goals-content"></div>
  </div>
  <div class="card" id="queue-card" hidden>
    <h2 class="block-title">📥 В очереди на обработку <span class="hint">заметки и идеи для постов</span></h2>
    <div id="queue-content"></div>
  </div>
  <footer>Только твои данные, обновляется сама каждые несколько минут — <a href="?refresh=1">🔄 обновить сейчас</a></footer>
</div>
<script type="application/json" id="planner-data">@@DATA_JSON@@</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById('planner-data').textContent);
  var CATEGORY_ORDER = ["💼 РАБОТА", "👤 ЛИЧНОЕ", "🏠 ДОМ"];
  var monday = data.week_dates[0].slice(0, 5);
  var sunday = data.week_dates[6].slice(0, 5);
  document.getElementById('page-title').textContent = 'Неделя, ' + monday + '–' + sunday;
  document.getElementById('snapshot-meta').textContent = 'Обновлено: ' + (data.snapshot_at || data.today);

  function escapeHtml(s) {
    return String(s).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; });
  }

  var strip = document.getElementById('week-strip');
  data.week_dates.forEach(function (dateStr, i) {
    var total = (data.week[dateStr] || []).length;
    var btn = document.createElement('button');
    btn.className = 'day-tab' + (dateStr === data.today ? ' is-today' : '');
    btn.setAttribute('role', 'tab');
    btn.setAttribute('data-date', dateStr);
    btn.innerHTML = '<span class="dow">' + data.weekday_names[i] + '</span>' +
      '<span class="dnum">' + dateStr.slice(0, 2) + '</span>' +
      '<span class="count">' + (total ? total + ' дел' : '') + '</span>';
    btn.addEventListener('click', function () { selectDate(dateStr); });
    strip.appendChild(btn);
  });

  var content = document.getElementById('content');
  function selectDate(dateStr) {
    Array.prototype.forEach.call(strip.children, function (btn) {
      btn.setAttribute('aria-selected', btn.getAttribute('data-date') === dateStr ? 'true' : 'false');
    });
    var tasks = data.week[dateStr] || [];
    content.innerHTML = '';
    if (!tasks.length) {
      var empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = 'Задач не записано.';
      content.appendChild(empty);
      return;
    }
    CATEGORY_ORDER.forEach(function (cat) {
      var inCat = tasks.filter(function (t) { return t.category === cat; });
      if (!inCat.length) return;
      var group = document.createElement('div');
      group.className = 'group';
      var label = document.createElement('div');
      label.className = 'group-label';
      label.textContent = cat;
      group.appendChild(label);
      var ul = document.createElement('ul');
      ul.className = 'tasks';
      inCat.forEach(function (t) {
        var li = document.createElement('li');
        var done = t.status === '✅';
        if (done) li.className = 'done';
        li.innerHTML = '<span class="box">' + (done ? '✅' : '☐') + '</span><span>' + escapeHtml(t.text) + '</span>';
        ul.appendChild(li);
      });
      group.appendChild(ul);
      content.appendChild(group);
    });
  }
  selectDate(data.today);

  var habitNames = Object.keys(data.habits || {});
  var trackedHabits = habitNames.filter(function (name) { return Object.keys(data.habits[name]).length > 0; });
  var habitsCard = document.getElementById('habits-card');
  if (!trackedHabits.length) {
    habitsCard.querySelector('.habit-scroll').innerHTML = '<p class="empty">За эту неделю привычки ещё не записаны.</p>';
  } else {
    var table = document.getElementById('habit-table');
    var thead = document.createElement('thead');
    var headRow = document.createElement('tr');
    headRow.innerHTML = '<th></th>' + data.weekday_names.map(function (dow, i) {
      return '<th class="' + (data.week_dates[i] === data.today ? 'today-col' : '') + '">' + dow + '</th>';
    }).join('');
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement('tbody');
    trackedHabits.forEach(function (name) {
      var tr = document.createElement('tr');
      var cells = '<td>' + escapeHtml(name) + '</td>';
      data.week_dates.forEach(function (d) {
        var val = data.habits[name][d];
        var todayCls = d === data.today ? ' today-col' : '';
        cells += val ? ('<td class="val filled' + todayCls + '">' + escapeHtml(val) + '</td>')
                      : ('<td class="val dash' + todayCls + '">–</td>');
      });
      tr.innerHTML = cells;
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }

  var goalsContent = document.getElementById('goals-content');
  var goals = data.goals || [];
  if (!goals.length) {
    goalsContent.innerHTML = '<p class="empty">Целей пока нет.</p>';
  } else {
    ['месяц', 'год'].forEach(function (period) {
      var periodGoals = goals.filter(function (g) { return g.period === period; });
      if (!periodGoals.length) return;
      var wrap = document.createElement('div');
      wrap.className = 'goal-group';
      var periodLabel = document.createElement('div');
      periodLabel.className = 'goal-period';
      periodLabel.textContent = 'На ' + period;
      wrap.appendChild(periodLabel);
      CATEGORY_ORDER.forEach(function (catLabel, idx) {
        var catKey = ['работа', 'личное', 'дом'][idx];
        var inCat = periodGoals.filter(function (g) { return g.category === catKey; });
        if (!inCat.length) return;
        var group = document.createElement('div');
        group.className = 'group';
        var label = document.createElement('div');
        label.className = 'group-label';
        label.textContent = catLabel;
        group.appendChild(label);
        var ul = document.createElement('ul');
        ul.className = 'goals';
        inCat.forEach(function (g) {
          var li = document.createElement('li');
          var done = g.status === '✅';
          if (done) li.className = 'done';
          var deadline = g.deadline ? '<span class="deadline">до ' + escapeHtml(g.deadline) + '</span>' : '';
          li.innerHTML = '<span class="box">' + (done ? '✅' : '☐') + '</span><span style="flex:1">' + escapeHtml(g.text) + '</span>' + deadline;
          ul.appendChild(li);
        });
        group.appendChild(ul);
        wrap.appendChild(group);
      });
      goalsContent.appendChild(wrap);
    });
  }

  var queue = (data.notes || []).map(function (n) { return Object.assign({ kind: '📝 заметка' }, n); })
    .concat((data.ideas || []).map(function (n) { return Object.assign({ kind: '💡 идея для поста' }, n); }));
  if (queue.length) {
    queue.sort(function (a, b) { return (a.date + a.time).localeCompare(b.date + b.time); });
    var queueCard = document.getElementById('queue-card');
    queueCard.hidden = false;
    var qc = document.getElementById('queue-content');
    queue.forEach(function (item) {
      var div = document.createElement('div');
      div.className = 'queue-item';
      div.innerHTML = '<div class="qmeta">' + item.kind + ' · ' + escapeHtml(item.date) + ' ' + escapeHtml(item.time) + '</div>' +
        '<div class="qtext">' + escapeHtml(item.text) + '</div>';
      qc.appendChild(div);
    });
  }
})();
</script>
</body></html>"""

def render_dashboard_page(owner, data):
    style = OWNER_DASHBOARD_STYLE.get(owner, DEFAULT_DASHBOARD_STYLE)
    display = display_name_for_owner(owner)
    html = DASHBOARD_PAGE_TEMPLATE
    html = html.replace("@@TITLE@@", f"Мой планер — {display}")
    html = html.replace("@@EYEBROW@@", f"Мой планер · {display}")
    html = html.replace("@@ACCENT_DARK@@", style["accent_dark"])
    html = html.replace("@@ACCENT_BG_DARK@@", style["accent_bg_dark"])
    html = html.replace("@@ACCENT_BG@@", style["accent_bg"])
    html = html.replace("@@ACCENT@@", style["accent"])
    html = html.replace("@@DATA_JSON@@", json.dumps(data, ensure_ascii=False))
    return html

async def refresh_dashboard_cache(owner):
    if owner not in DASHBOARD_URLS:
        return
    try:
        data = await asyncio.to_thread(build_dashboard_data, owner)
        DASHBOARD_CACHE[owner] = render_dashboard_page(owner, data)
    except Exception:
        logger.exception(f"Не удалось обновить дашборд для {owner}")

def schedule_dashboard_refresh(owner):
    """Фоновое обновление кэша дашборда — вызывается после действий,
    которые могли что-то поменять (не блокирует ответ пользователю)."""
    if owner not in DASHBOARD_URLS:
        return
    try:
        asyncio.get_running_loop().create_task(refresh_dashboard_cache(owner))
    except RuntimeError:
        pass

async def refresh_all_dashboards(context: ContextTypes.DEFAULT_TYPE):
    for owner in DASHBOARD_URLS:
        await refresh_dashboard_cache(owner)

async def dashboard_http_handler(request):
    token = request.match_info["token"]
    owner = DASHBOARD_TOKEN_TO_OWNER.get(token)
    if not owner:
        return web.Response(status=404, text="Страница не найдена.")
    if request.query.get("refresh") == "1" or owner not in DASHBOARD_CACHE:
        await refresh_dashboard_cache(owner)
    html = DASHBOARD_CACHE.get(owner)
    if not html:
        return web.Response(status=503, text="Не удалось собрать данные, попробуй обновить через минуту.")
    return web.Response(text=html, content_type="text/html", charset="utf-8")

async def start_dashboard_server():
    web_app = web.Application()
    web_app.router.add_get("/d/{token}", dashboard_http_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Дашборд-сервер слушает на порту {port}")
    return runner

async def refresh_dashboard_after_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Регистрируется отдельным обработчиком с group=1 — срабатывает
    после любого текста/голоса/фото/нажатия кнопки, независимо от того,
    какая именно ветка внутри сработала (проще, чем расставлять вызов
    по всем return в handle_text/handle_voice/handle_callback/
    handle_health_photo)."""
    if not is_allowed(update):
        return
    schedule_dashboard_refresh(owner_for(update))

# ── Main ───────────────────────────────────────────────────────
async def main_async():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("menu",   menu_command))
    app.add_handler(CommandHandler("table",  table_command))
    app.add_handler(CommandHandler("today",  today_tasks))
    app.add_handler(CommandHandler("done",   done_command))
    app.add_handler(CommandHandler("habits", habits_command))
    app.add_handler(CommandHandler("goals",  goals_command))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_health_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    if DASHBOARD_URLS:
        app.add_handler(MessageHandler(filters.VOICE, refresh_dashboard_after_update), group=1)
        app.add_handler(MessageHandler(filters.PHOTO, refresh_dashboard_after_update), group=1)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, refresh_dashboard_after_update), group=1)
        app.add_handler(CallbackQueryHandler(refresh_dashboard_after_update), group=1)
    app.job_queue.run_daily(run_nightly_job, time=dt_time(hour=16, minute=0, tzinfo=timezone.utc))
    app.job_queue.run_daily(run_evening_checklist, time=dt_time(hour=16, minute=5, tzinfo=timezone.utc))
    app.job_queue.run_repeating(check_reminders, interval=REMINDER_CHECK_INTERVAL, first=10)
    if DASHBOARD_URLS:
        app.job_queue.run_repeating(refresh_all_dashboards, interval=300, first=15)

    dashboard_runner = await start_dashboard_server() if DASHBOARD_URLS else None
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("🤖 Бот запущен!")
    try:
        await asyncio.Event().wait()
    finally:
        if dashboard_runner:
            await dashboard_runner.cleanup()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
