"""
Показать все листы в Google Таблице планнера.
Запуск: python scripts/list_sheets.py
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
import bot

sheet = bot.get_sheet()
for ws in sheet.worksheets():
    print(ws.title, f"({ws.row_count}x{ws.col_count})")
