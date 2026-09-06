"""
Личный Планнер — Telegram Bot
Голос → Groq Whisper API → Groq LLaMA API → Google Sheets
Без библиотеки groq — прямые HTTP запросы
"""

import os, re, time, logging, json, tempfile, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
SPREADSHEET_ID  = os.getenv("SPREADSHEET_ID")
SPREADSHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"

def _parse_users(raw):
    """USERS=182778711:Оля,555555555:Мама — Telegram ID -> имя владельца."""
    users = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        uid, name = part.split(":", 1)
        uid, name = uid.strip(), name.strip()
        if uid and name:
            users[uid] = name
    return users

USER_NAMES    = _parse_users(os.getenv("USERS"))
ALLOWED_USERS = set(USER_NAMES.keys())
ALL_OWNERS    = list(dict.fromkeys(USER_NAMES.values()))  # без дублей, сохраняя порядок

def owner_for(update):
    return USER_NAMES.get(str(update.effective_user.id), "Гость")

def _words(text):
    return set(re.findall(r"\w+", text.lower(), re.UNICODE))

def resolve_owner_name(spoken):
    """Сопоставить произнесённое имя с одним из известных участников."""
    if not spoken:
        return None
    spoken_words = _words(spoken)
    for name in ALL_OWNERS:
        if _words(name) & spoken_words:
            return name
    return None

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("📋 Меню"), KeyboardButton("🗓 Таблица"), KeyboardButton("📅 Сегодня")]],
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
- date: "{today}" если сегодня, "{tomorrow}" если завтра, иначе null
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

def get_sheet():
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

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

# ── State ──────────────────────────────────────────────────────
user_states = {}

def is_allowed(update):
    return str(update.effective_user.id) in ALLOWED_USERS

# ── Save task ──────────────────────────────────────────────────
async def save_task(update_or_query, parsed, default_date, owner):
    date_str  = parsed.get("date") or default_date
    category  = parsed.get("category", "личное")
    task      = parsed.get("task", "")
    time_str  = parsed.get("time") or ""
    cat_emoji = {"работа": "💼", "личное": "👤", "дом": "🏠"}.get(category, "📌")
    try:
        add_task_to_sheet(owner, category, task, time_str, date_str)
    except Exception:
        logger.exception("Could not save task to weekly planner")
        await update_or_query.message.reply_text(
            "❌ Не удалось записать задачу в недельный планнер. Попробуй ещё раз.")
        return
    time_info = f" в {time_str}" if time_str else ""
    text = (f"✅ Записала!\n\n{cat_emoji} *{category.capitalize()}*\n"
            f"📌 {task}{time_info}\n📅 {date_str}")
    await update_or_query.message.reply_text(text, parse_mode="Markdown")

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
    owner = owner_for(update)
    await update.message.reply_text(
        f"👋 Привет, {owner}! Я твой личный планнер.\n\n"
        "🎤 Голосовое — запишу задачу\n"
        "✍️ Текст — тоже пойму\n"
        "🗑 «Удали запись про...» — сотру подходящую запись\n\n"
        "📋 /today — задачи на сегодня\n"
        "✅ /done — отметить выполненное\n"
        "📊 /habits — привычки за день\n"
        "🎯 /goals — цели на месяц/год\n"
        "🗓 /table — открыть таблицу\n"
        "📋 /menu — главное меню\n\n"
        "Просто говори — я пойму! 😊",
        reply_markup=MAIN_KEYBOARD)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    owner = owner_for(update)
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
        await process_text(update, text, owner)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Не удалось расшифровать. Попробуй ещё раз.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    user_id = str(update.effective_user.id)
    owner   = owner_for(update)
    text    = (update.message.text or "").strip()
    today   = datetime.now().strftime("%d.%m.%Y")

    # Handle persistent keyboard buttons (match by keyword, since some
    # Telegram clients add/drop emoji variation selectors on the label)
    if "Меню" in text:
        await menu_command(update, context)
        return
    if "Таблица" in text:
        await table_command(update, context)
        return
    if "Сегодня" in text:
        await today_tasks(update, context)
        return

    if user_id in user_states and user_states[user_id].get("awaiting_date"):
        user_states[user_id]["date"] = text
        user_states[user_id].pop("awaiting_date")
        parsed = user_states.pop(user_id)
        await save_task(update, parsed, today, owner)
        return
    await process_text(update, text, owner)

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
    keyboard = [[InlineKeyboardButton("📊 Открыть таблицу", url=SPREADSHEET_URL)]]
    await update.message.reply_text(
        "📊 *Твой планнер в Google Таблицах:*\n\nНажми кнопку ниже чтобы открыть:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    keyboard = [
        [InlineKeyboardButton("📅 Задачи на сегодня", callback_data="menu_today")],
        [InlineKeyboardButton("✅ Отметить выполненное", callback_data="menu_done")],
        [InlineKeyboardButton("📊 Привычки", callback_data="menu_habits")],
        [InlineKeyboardButton("🎯 Цели", callback_data="menu_goals")],
        [InlineKeyboardButton("🗓 Открыть таблицу", url=SPREADSHEET_URL)],
    ]
    await update.message.reply_text(
        "📋 *Главное меню:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown")

# ── Main ───────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("menu",   menu_command))
    app.add_handler(CommandHandler("table",  table_command))
    app.add_handler(CommandHandler("today",  today_tasks))
    app.add_handler(CommandHandler("done",   done_command))
    app.add_handler(CommandHandler("habits", habits_command))
    app.add_handler(CommandHandler("goals",  goals_command))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
