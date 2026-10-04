"""Проверка: «убрать из витаминов утром …» убирает из утреннего набора, а не из первого попавшегося;
кавычки-«ёлочки» не становятся частью названия.

Запуск: .venv\\Scripts\\python.exe scripts\\test_supplement_remove.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_TOKEN", "test")
import logging
logging.disable(logging.CRITICAL)
import bot

remembered = {
    ("Мама", "💊 Витамины вечером"): "магний-3, хондроитин-1, глутамин-1, метабиотик-1",
    ("Мама", "💊 Витамины утром"): "«магний-2, хондроитин-1, глутамин-1»",
    ("Оля", "💊 Витамины утром"): "омега-1",
}

# 04.10.2026, мама: «Витамины утром хондроитин, магний удалить» — убралось из вечернего
habit, hit, kept = bot.pick_supplement_removal(remembered, "Мама", "Витамины утром хондроитин, магний удалить")
assert habit == "💊 Витамины утром", habit
assert hit == ["магний-2", "хондроитин-1"], hit
assert kept == ["глутамин-1"], kept

habit, hit, kept = bot.pick_supplement_removal(remembered, "Мама", "Убрать из витамины утром глутамин")
assert habit == "💊 Витамины утром" and hit == ["глутамин-1"], (habit, hit)

habit, hit, kept = bot.pick_supplement_removal(remembered, "Мама", "убери метабиотик из вечерних витаминов")
assert habit == "💊 Витамины вечером" and hit == ["метабиотик-1"], (habit, hit)

# набор не назван — как раньше, ищем, где есть такой пункт
habit, hit, kept = bot.pick_supplement_removal(remembered, "Мама", "убери метабиотик")
assert habit == "💊 Витамины вечером", habit

# «витамин» в названии набора не должен совпадать с пунктом «витамин Д3»
r2 = {("Мама", "💊 Витамины утром"): "омега-1, витамин Д3-3, витамин С-1"}
habit, hit, kept = bot.pick_supplement_removal(r2, "Мама", "убрать из витамины утром омегу")
assert hit == ["омега-1"], hit

# другие формы слова в названии набора
r3 = {("Мама", "Вечерние витамины"): "магний-3, глутамин-1", ("Мама", "Утренние витамины"): "магний-2, омега-1"}
habit, hit, kept = bot.pick_supplement_removal(r3, "Мама", "убери магний из витаминов утром")
assert habit == "Утренние витамины" and hit == ["магний-2"], (habit, hit)

assert bot.split_supplements("«магний-2, хондроитин-1, глутамин-1»") == ["магний-2", "хондроитин-1", "глутамин-1"]
assert bot.pick_supplement_removal(remembered, "Мама", "убери кальций") is None
print("OK")
