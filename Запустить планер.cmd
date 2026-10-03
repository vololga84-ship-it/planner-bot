@echo off
rem Запуск планер-бота. Этот файл можно положить в автозагрузку Windows.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "D:\Мой планнер\planner_bot\run_bot.ps1"
