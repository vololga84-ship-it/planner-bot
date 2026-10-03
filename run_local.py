"""Локальный запуск планер-бота.

Отличие от прямого запуска bot.py одно: библиотека httpx по умолчанию пишет
в лог каждый HTTP-запрос вместе с адресом, а в адресе Telegram содержится
токен бота. На сервере лог никто не хранил, на домашнем компьютере он лежит
файлом, поэтому эти строки приглушаем. Остальное поведение не меняется.
"""
import logging
import runpy

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)

runpy.run_path("bot.py", run_name="__main__")
