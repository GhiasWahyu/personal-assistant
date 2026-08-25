#!/bin/bash
# ==============================================================================
# SCRIPT DEPLOYMENT LENGKAP BOT ASISTEN PRIBADI KE GOOGLE CLOUD / VPS LINUX
# Otomatis: Install Dependensi, Setup Systemd Services, & Autostart 24/7 Nonstop
# ==============================================================================

set -e

echo "=========================================================="
echo "  MEMULAI SETUP BOT ASISTEN PRIBADI DI SERVER (24/7 CLOUD)"
echo "=========================================================="

# 1. Update paket sistem
sudo apt-get update -y
sudo apt-get install -y curl git python3 python3-pip python3-venv

# 2. Install Node.js v20 LTS jika belum ada
if ! command -v node &> /dev/null; then
    echo "📦 Menginstall Node.js v20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# 3. Direktori Aplikasi
APP_DIR="/opt/personal_assistant"
sudo mkdir -p "$APP_DIR"
sudo chown -R $USER:$USER "$APP_DIR"

# 4. Salin / Pindahkan kode proyek ke /opt/personal_assistant jika dijalankan dari repo
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -d "$CURRENT_DIR/telegram_bot" ] && [ -d "$CURRENT_DIR/whatsapp_bot" ]; then
    echo "📂 Menyalin file proyek..."
    cp -r "$CURRENT_DIR/telegram_bot" "$APP_DIR/"
    cp -r "$CURRENT_DIR/whatsapp_bot" "$APP_DIR/"
fi

# 5. Setup Python Virtual Environment
echo "🐍 Menyiapkan Python Virtual Environment..."
cd "$APP_DIR/telegram_bot"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install google-genai psycopg2-binary python-dotenv "python-telegram-bot[job-queue]" APScheduler holidays requests

# 6. Setup WhatsApp Gateway (Node.js)
echo "📱 Menyiapkan dependensi WhatsApp Gateway..."
cd "$APP_DIR/whatsapp_bot"
npm install --legacy-peer-deps

# 7. Membuat Systemd Service untuk Telegram Bot & AI Backend
echo "⚙️ Membuat systemd service untuk Telegram & AI Backend..."
sudo tee /etc/systemd/system/assistant-telegram.service > /dev/null <<EOF
[Unit]
Description=Personal Assistant Telegram Bot and AI Backend
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR/telegram_bot
ExecStart=$APP_DIR/telegram_bot/venv/bin/python bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# 8. Membuat Systemd Service untuk WhatsApp Gateway
echo "⚙️ Membuat systemd service untuk WhatsApp Gateway..."
sudo tee /etc/systemd/system/assistant-whatsapp.service > /dev/null <<EOF
[Unit]
Description=Personal Assistant WhatsApp Gateway
After=network.target assistant-telegram.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR/whatsapp_bot
ExecStart=/usr/bin/node gateway.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 9. Reload & Aktifkan Service agar jalan otomatis saat server booting
sudo systemctl daemon-reload
sudo systemctl enable assistant-telegram.service
sudo systemctl enable assistant-whatsapp.service
sudo systemctl restart assistant-telegram.service
sudo systemctl restart assistant-whatsapp.service

echo "=========================================================="
echo "  ✅ DEPLOYMENT BERHASIL! BOT SUDAH AKTIF 24/7 NONSTOP    "
echo "=========================================================="
echo "Status Service Telegram: sudo systemctl status assistant-telegram"
echo "Status Service WhatsApp: sudo systemctl status assistant-whatsapp"
echo "Cek Log: journalctl -u assistant-telegram -f"
echo "=========================================================="
