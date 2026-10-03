"""Проверка: «→ Перенести» в вечернем чек-листе (вторая кнопка в строке) раскрывает выбор дня.

Запуск: .venv\\Scripts\\python.exe scripts\\test_postpone_keyboard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_TOKEN", "test")
import logging
logging.disable(logging.CRITICAL)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import bot

date_str, row_num = "03.10.2026", 7
markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Сделано", callback_data=f"evdone_{date_str}_{row_num}"),
     InlineKeyboardButton("→ Перенести", callback_data=f"evpost_{date_str}_{row_num}")],
    [InlineKeyboardButton("✅ Сделано", callback_data=f"evdone_{date_str}_8"),
     InlineKeyboardButton("→ Перенести", callback_data=f"evpost_{date_str}_8")],
])
new = bot.postpone_choice_keyboard(markup, f"evpost_{date_str}_{row_num}", date_str, row_num)
datas = [[b.callback_data for b in row] for row in new.inline_keyboard]
assert datas[0] == [f"evtmrw_{date_str}_{row_num}", f"evcust_{date_str}_{row_num}"], datas
assert datas[1] == [f"evdone_{date_str}_8", f"evpost_{date_str}_8"], datas
# одиночная кнопка (после записи задачи без даты) — тоже раскрывается
single = InlineKeyboardMarkup([[InlineKeyboardButton("📅 Не сегодня, перенести", callback_data=f"evpost_{date_str}_{row_num}")]])
datas = [[b.callback_data for b in row] for row in bot.postpone_choice_keyboard(single, f"evpost_{date_str}_{row_num}", date_str, row_num).inline_keyboard]
assert datas == [[f"evtmrw_{date_str}_{row_num}", f"evcust_{date_str}_{row_num}"]], datas
print("OK")
