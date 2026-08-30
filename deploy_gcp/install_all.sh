#!/bin/bash
# ==============================================================================
# INSTALLER LENGKAP BOT ASISTEN PRIBADI (GOOGLE CLOUD 24/7)
# ==============================================================================

set -e

echo "=========================================================="
echo "  MEMULAI INSTALASI BOT ASISTEN PRIBADI (GCP ALWAYS FREE) "
echo "=========================================================="

# 1. Update & Prasyarat
sudo apt-get update -y
sudo apt-get install -y curl git python3 python3-pip python3-venv postgresql postgresql-contrib

# Install Node.js v20 LTS jika belum ada
if ! command -v node &> /dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# 2. Setup PostgreSQL
echo "🐘 Mengatur database PostgreSQL..."
sudo -u postgres psql -c "ALTER USER postgres WITH PASSWORD 'REDACTED_PG_PASSWORD';" || sudo -u postgres psql -c "CREATE USER postgres WITH PASSWORD 'REDACTED_PG_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE personal_assistant OWNER postgres;" || true

sudo -u postgres psql -d personal_assistant << 'EOF'
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS kategori (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    nama VARCHAR(100) NOT NULL UNIQUE,
    tipe VARCHAR(50) NOT NULL,
    jenis_budget VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS budget (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kategori_id UUID REFERENCES kategori(id) ON DELETE CASCADE,
    limit_nominal NUMERIC(15,2) NOT NULL,
    bulan INT NOT NULL,
    tahun INT NOT NULL
);

CREATE TABLE IF NOT EXISTS transaksi (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kategori_id UUID REFERENCES kategori(id) ON DELETE SET NULL,
    jumlah NUMERIC(15,2) NOT NULL,
    tipe VARCHAR(50) NOT NULL,
    tanggal DATE NOT NULL DEFAULT CURRENT_DATE,
    deskripsi VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jadwal_kerja (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tanggal DATE NOT NULL,
    jam_mulai TIME NOT NULL,
    jam_selesai TIME,
    lokasi VARCHAR(100),
    catatan TEXT
);

INSERT INTO kategori (nama, tipe, jenis_budget) VALUES
('Kebutuhan Pokok', 'pengeluaran', 'kebutuhan'),
('Keinginan & Hiburan', 'pengeluaran', 'keinginan'),
('Tabungan', 'pengeluaran', 'tabungan')
ON CONFLICT (nama) DO NOTHING;
EOF

# 3. Direktori Aplikasi
APP_DIR="/opt/personal_assistant"
sudo mkdir -p "$APP_DIR/telegram_bot" "$APP_DIR/whatsapp_bot"
sudo chown -R $USER:$USER "$APP_DIR"

# 4. Setup Python Virtual Environment
echo "🐍 Menginstall library Python..."
cd "$APP_DIR/telegram_bot"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install google-genai psycopg2-binary python-dotenv "python-telegram-bot[job-queue]" APScheduler holidays requests matplotlib

# 5. Setup Node.js WhatsApp Gateway
echo "📱 Menginstall library WhatsApp Gateway..."
cd "$APP_DIR/whatsapp_bot"
npm init -y
npm install @whiskeysockets/baileys@^6.7.9 axios express pino qrcode qrcode-terminal

echo "=========================================================="
echo "  INSTALASI SELESAI! TINGGAL SALIN SOURCE CODE & JALANKAN "
echo "=========================================================="
