"""Локальный запуск планер-бота.

Отличия от прямого запуска bot.py:
1. Библиотека httpx по умолчанию пишет в лог каждый HTTP-запрос вместе с
   адресом, а в адресе Telegram содержится токен бота. На сервере лог никто
   не хранил, на домашнем компьютере он лежит файлом, поэтому эти строки
   приглушаем.
2. Google Таблица ходит напрямую, мимо VPN: через VPN открытие таблицы
   занимало ~10 с против ~2 с напрямую, и бот замирал на минуту каждые
   5 минут (проверка напоминаний). Telegram и Groq — по-прежнему через VPN.
3. Сторож: 03.10.2026 бот перестал забирать обновления у Telegram (кнопки
   не отвечали, 22 нажатия висели в очереди), а таймеры при этом работали —
   снаружи не видно. Если очередь в Telegram не пустеет 2 минуты подряд,
   процесс завершается, и run_bot.ps1 поднимает бота заново.
"""
import logging
import os
import runpy
import threading
import time

import requests
from dotenv import load_dotenv

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)

load_dotenv()
_direct = "googleapis.com,accounts.google.com"
os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY"), _direct]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]

WATCH_EVERY = 60     # сек между проверками
WATCH_LIMIT = 2      # столько проверок подряд с непустой очередью — перезапуск (было 4, 05.10.2026 уменьшено)


def _watchdog():
    log = logging.getLogger("watchdog")
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/getWebhookInfo"
    stuck = 0
    time.sleep(120)  # даём боту стартовать и разобрать накопившееся
    while True:
        try:
            pending = requests.get(url, timeout=30).json()["result"]["pending_update_count"]
        except Exception:
            pending = None  # нет сети — это не зависание бота, не считаем
        stuck = stuck + 1 if pending else 0
        if stuck >= WATCH_LIMIT:
            log.error(f"Сторож: в очереди Telegram {pending} обновлений уже {stuck} мин — перезапускаю бота")
            logging.shutdown()
            os._exit(3)
        time.sleep(WATCH_EVERY)


threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
runpy.run_path("bot.py", run_name="__main__")
