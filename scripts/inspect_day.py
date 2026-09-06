"""
Показать задачи, привычки и цели участника за день (без записи в чат).
Запуск: python scripts/inspect_day.py <Имя> [дд.мм.гггг]
Пример: python scripts/inspect_day.py Оля
        python scripts/inspect_day.py Мама 06.09.2026
"""
import sys, os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
import bot

if len(sys.argv) < 2:
    print("Использование: python scripts/inspect_day.py <Имя> [дд.мм.гггг]")
    sys.exit(1)

owner = sys.argv[1]
date_str = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%d.%m.%Y")

print(f"=== {owner} — {date_str} ===\n")

print("Задачи:")
for row_num, row in bot.get_tasks_for_day(date_str, owner):
    print(" ", row_num, row)

print("\nПривычки:")
for row_num, row in bot.get_habits_for_day(date_str, owner):
    print(" ", row_num, row)

print("\nЦели:")
for goal in bot.get_goals(owner):
    print(" ", goal)
