@echo off
cd /d "D:\Проект python"
if "%TELEGRAM_BOT_TOKEN%"=="" (
    echo Ошибка: не задан TELEGRAM_BOT_TOKEN
    echo Задайте токен командой: set TELEGRAM_BOT_TOKEN=ваш_токен
    pause
    exit /b
)
python telegram_bot.py
pause
