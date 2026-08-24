#!/bin/bash
# ==============================================================================
# SCRIPT DEPLOYMENT OTOMATIS: PERSONAL ASSISTANT BOT (TELEGRAM + WHATSAPP)
# Google Cloud Always Free Tier (e2-micro) 100% Zero-Cost Guarantee
# ==============================================================================

set -e

echo "🚀 Memulai instalasi Asisten Pribadi di Google Cloud Server..."

# 1. Update sistem & install dependensi dasar
sudo apt-get update -y
sudo apt-get install -y curl git python3 python3-pip python3-venv postgresql postgresql-contrib ufw

# Install Node.js v20 LTS
if ! command -v node &> /dev/null; then
    echo "📦 Menginstall Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# 2. Setup Database PostgreSQL Lokal di Server
echo "🐘 Menyiapkan database PostgreSQL..."
sudo -u postgres psql -c "CREATE USER postgres WITH PASSWORD 'REDACTED_PG_PASSWORD';" || true
sudo -u postgres psql -c "CREATE DATABASE personal_assistant OWNER postgres;" || true

# 3. Setup Direktori Aplikasi
APP_DIR="$HOME/personal_assistant"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# 4. Clone atau Siapkan Kode Bot
echo "📂 Menyiapkan source code..."
# Buat struktur direktori
mkdir -p telegram_bot whatsapp_bot

# Inisialisasi PostgreSQL Schema
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

echo "✅ Database PostgreSQL berhasil disiapkan!"
