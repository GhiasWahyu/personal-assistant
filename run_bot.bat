@echo off
title Personal Assistant Bot
echo ====================================================
echo   MEMULAI BOT ASISTEN PRIBADI (TELEGRAM & WHATSAPP)
echo ====================================================

:: 1. Start WhatsApp Gateway in background window
start "WhatsApp Gateway" cmd /k "cd /d %~dp0whatsapp_bot && npm start"

:: 2. Start Telegram & Assistant Backend
echo Memulai Telegram Bot & Backend AI...
cd /d "%~dp0telegram_bot"
call venv\Scripts\activate.bat
python bot.py

pause
