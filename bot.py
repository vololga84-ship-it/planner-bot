"""
Личный Планнер — Telegram Bot
Голос → Groq Whisper API → Groq LLaMA API → Google Sheets
Без библиотеки groq — прямые HTTP запросы
"""

import os, logging, json, tempfile, requests
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
ALLOWED_USERS   = set(os.getenv("ALLOWED_USERS", "").split(","))
SPREADSHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"

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

def parse_task(text, today):
    """Понять задачу через Groq LLaMA."""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
    prompt = f"""Сегодня {today}. Пользователь сказал: "{text}"

Ответь ТОЛЬКО валидным JSON объектом, без пояснений, без markdown:
{{"type":"task","category":"личное","task":"{text}","date":null,"time":null,"habit_name":null,"habit_value":null,"missing":[]}}

Заполни поля правильно:
- type: "task" (обычная задача), "habit" (привычка: сон/витамины/прогулка/вода/встала/легла), "note" (заметка)
- category: "работа", "личное" или "дом". Если не указано явно — угадай по контексту (парикмахер/врач/магазин = личное, уборка/готовка = дом, встреча/звонок коллеге = работа)
- task: краткое описание задачи
- date: "{today}" если сегодня, "{tomorrow}" если завтра, иначе null
- time: время в формате ЧЧ:ММ если упомянуто, иначе null
- habit_name: для привычки выбери одно точное название: "🌙 Легла спать", "☀️ Встала", "😴 Сон (часов)", "💊 Витамины утром", "💊 Витамины вечер", "🚶 Прогулка", "💧 Вода", "📖 Чтение", "😊 Настроение" или "⚡ Энергия"
- habit_value: значение привычки без пояснений, например "23:30", "✅", "45", "8"
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
HABIT_ROWS = {
    "🌙 Легла спать": 26, "☀️ Встала": 27, "😴 Сон (часов)": 28,
    "💊 Витамины утром": 29, "💊 Витамины вечер": 30, "🚶 Прогулка": 31,
    "💧 Вода": 32, "📖 Чтение": 33, "😊 Настроение": 34, "⚡ Энергия": 35,
}

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

def week_sheet_name(date_str):
    monday = week_start(date_str)
    sunday = monday + timedelta(days=6)
    return f"📅 Неделя {monday:%d.%m}–{sunday:%d.%m}"

def get_week_sheet(date_str, create=False):
    sheet = get_sheet()
    title = week_sheet_name(date_str)
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
        return ws

def day_column(date_str):
    return chr(ord("B") + datetime.strptime(date_str, "%d.%m.%Y").weekday())

def add_task_to_sheet(category, task, time_str, date_str, row_type="task"):
    ws = get_week_sheet(date_str, create=True)
    column = day_column(date_str)
    if row_type == "habit":
        row = HABIT_ROWS.get(task)
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

def get_tasks_for_day(date_str):
    try:
        ws = get_week_sheet(date_str, create=False)
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

def get_habits_for_day(date_str):
    ws = get_week_sheet(date_str, create=False)
    if not ws:
        return []
    column = day_column(date_str)
    values = ws.get(f"A26:{column}35")
    day_index = ord(column) - ord("A")
    return [(26 + i, ["⏰ ПРИВЫЧКА", row[0], row[day_index], "", "habit"])
            for i, row in enumerate(values) if len(row) > day_index and row[day_index]]

def mark_task_done(date_str, row_num):
    ws = get_week_sheet(date_str, create=False)
    if not ws:
        return
    column = day_column(date_str)
    value = ws.acell(f"{column}{row_num}").value or ""
    ws.update(f"{column}{row_num}", [["✅ " + value.lstrip("☐✅ ").strip()]])

# ── State ──────────────────────────────────────────────────────
user_states = {}

def is_allowed(update):
    return str(update.effective_user.id) in ALLOWED_USERS

# ── Save task ──────────────────────────────────────────────────
async def save_task(update_or_query, parsed, default_date):
    date_str  = parsed.get("date") or default_date
    category  = parsed.get("category", "личное")
    task      = parsed.get("task", "")
    time_str  = parsed.get("time") or ""
    cat_emoji = {"работа": "💼", "личное": "👤", "дом": "🏠"}.get(category, "📌")
    try:
        add_task_to_sheet(category, task, time_str, date_str)
    except Exception:
        logger.exception("Could not save task to weekly planner")
        await update_or_query.message.reply_text(
            "❌ Не удалось записать задачу в недельный планнер. Попробуй ещё раз.")
        return
    time_info = f" в {time_str}" if time_str else ""
    text = (f"✅ Записала!\n\n{cat_emoji} *{category.capitalize()}*\n"
            f"📌 {task}{time_info}\n📅 {date_str}")
    if hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(text, parse_mode="Markdown")
    else:
        await update_or_query.message.reply_text(text, parse_mode="Markdown")

# ── Process text/voice ─────────────────────────────────────────
async def process_text(update, text):
    user_id = str(update.effective_user.id)
    today   = datetime.now().strftime("%d.%m.%Y")
    try:
        parsed = parse_task(text, today)
    except Exception:
        logger.exception("Task parsing failed")
        await update.message.reply_text(
            "❌ Не удалось обработать задачу из-за ошибки сервиса. "
            "Попробуй ещё раз через минуту.\n"
            "Например: «Позвонить врачу, личное, завтра в 10:00»")
        return

    if parsed.get("type") == "habit":
        date_str = parsed.get("date") or today
        try:
            add_task_to_sheet("", parsed.get("habit_name", ""),
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
        await save_task(update, parsed, today)
        return

    await update.message.reply_text(
        "🎤 Отправь голосовое или напиши задачу.\n"
        "Например: «Купить продукты, дом, завтра»")

# ── Handlers ───────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Доступ закрыт.")
        return
    await update.message.reply_text(
        "👋 Привет! Я твой личный планнер.\n\n"
        "🎤 Голосовое — запишу задачу\n"
        "✍️ Текст — тоже пойму\n\n"
        "📋 /today — задачи на сегодня\n"
        "✅ /done — отметить выполненное\n"
        "📊 /habits — привычки за день\n"
        "🗓 /table — открыть таблицу\n"
        "📋 /menu — главное меню\n\n"
        "Просто говори — я пойму! 😊",
        reply_markup=MAIN_KEYBOARD)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
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
        await process_text(update, text)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Не удалось расшифровать. Попробуй ещё раз.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    user_id = str(update.effective_user.id)
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
        await save_task(update, parsed, today)
        return
    await process_text(update, text)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    data    = query.data
    today   = datetime.now().strftime("%d.%m.%Y")
    if data.startswith("cat_"):
        category = data.replace("cat_", "")
        if user_id in user_states:
            user_states[user_id]["category"] = category
            parsed = user_states.pop(user_id)
            await save_task(query, parsed, today)
    elif data == "menu_today":
        await today_tasks(update, context)
    elif data == "menu_done":
        await done_command(update, context)
    elif data == "menu_habits":
        await habits_command(update, context)
    elif data.startswith("done_"):
        parts    = data.split("_")
        date_str = parts[1]
        row_num  = int(parts[2])
        mark_task_done(date_str, row_num)
        await query.edit_message_text(query.message.text + "\n\n✅ Готово!")

async def today_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    today = datetime.now().strftime("%d.%m.%Y")
    tasks = get_tasks_for_day(today)
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
    today = datetime.now().strftime("%d.%m.%Y")
    tasks = [(rn, row) for rn, row in get_tasks_for_day(today)
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
    today  = datetime.now().strftime("%d.%m.%Y")
    habits = get_habits_for_day(today)
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
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
