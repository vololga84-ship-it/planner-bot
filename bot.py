"""
Личный Планнер — Telegram Bot
Голос → Groq Whisper API → Groq LLaMA API → Google Sheets
Без библиотеки groq — прямые HTTP запросы
"""

import os, logging, json, tempfile, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# ── Google Sheets ──────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

def find_or_create_day_sheet(sheet, date_str):
    try:
        return sheet.worksheet(date_str)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=date_str, rows=100, cols=5)
        ws.update("A1:E1", [["Категория", "Задача", "Время", "Выполнено", "Тип"]])
        ws.format("A1:E1", {"textFormat": {"bold": True}})
        return ws

def add_task_to_sheet(category, task, time_str, date_str, row_type="task"):
    sheet = get_sheet()
    ws = find_or_create_day_sheet(sheet, date_str)
    next_row = len(ws.get_all_values()) + 1
    ws.update(f"A{next_row}:E{next_row}", [[category, task, time_str, "☐", row_type]])
    return next_row

def get_tasks_for_day(date_str):
    try:
        sheet = get_sheet()
        ws = sheet.worksheet(date_str)
        rows = ws.get_all_values()
        return [(i+2, row) for i, row in enumerate(rows[1:]) if len(row) >= 2 and row[1]]
    except Exception:
        return []

def mark_task_done(date_str, row_num):
    sheet = get_sheet()
    ws = sheet.worksheet(date_str)
    ws.update(f"D{row_num}", [["✅"]])

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
    add_task_to_sheet(f"{cat_emoji} {category.upper()}", task, time_str, date_str)
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
        add_task_to_sheet("⏰ ПРИВЫЧКА", parsed.get("habit_name",""),
                          parsed.get("habit_value",""), date_str, "habit")
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
        "📊 /habits — привычки за день\n\n"
        "Просто говори — я пойму! 😊")

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
    text    = update.message.text
    today   = datetime.now().strftime("%d.%m.%Y")
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
        await update.message.reply_text(f"📅 На {today} задач нет.\n\nНаговори что-нибудь! 🎤")
        return
    text = f"📅 *Задачи на {today}:*\n\n"
    for _, row in tasks:
        if len(row) >= 5 and row[4] == "habit": continue
        status    = row[3] if len(row) > 3 else "☐"
        t         = row[2] if len(row) > 2 and row[2] else ""
        time_info = f" _{t}_" if t else ""
        text += f"{status} {row[1]}{time_info}\n   {row[0]}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    today = datetime.now().strftime("%d.%m.%Y")
    tasks = [(rn, row) for rn, row in get_tasks_for_day(today)
             if (len(row) < 5 or row[4] != "habit") and
                (len(row) < 4 or row[3] != "✅")]
    if not tasks:
        await update.message.reply_text("🎉 Все задачи выполнены!")
        return
    keyboard = [[InlineKeyboardButton(f"☐ {row[1][:45]}",
                 callback_data=f"done_{today}_{rn}")] for rn, row in tasks if row[1]]
    await update.message.reply_text(
        "✅ *Что выполнено?*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown")

async def habits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    today  = datetime.now().strftime("%d.%m.%Y")
    tasks  = get_tasks_for_day(today)
    habits = [(rn, row) for rn, row in tasks if len(row) >= 5 and row[4] == "habit"]
    if not habits:
        await update.message.reply_text(
            f"📊 *Привычки на {today}*\n\nНичего не записано.\n\n"
            "Скажи голосом: «Встала в 7:30» или «Витамины утром приняла» 🎤",
            parse_mode="Markdown")
        return
    text = f"📊 *Привычки на {today}:*\n\n"
    for _, row in habits:
        text += f"• {row[1]}: *{row[2]}*\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ── Main ───────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
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
