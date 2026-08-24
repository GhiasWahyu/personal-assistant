#!/bin/bash
set -e

echo "Starting WhatsApp Gateway on port 3000..."
cd /app/whatsapp_bot && node gateway.js &

echo "Starting Telegram & AI Backend on port 5001..."
cd /app/telegram_bot && python3 bot.py &

# Wait for any process to exit
wait -n
exit $?
