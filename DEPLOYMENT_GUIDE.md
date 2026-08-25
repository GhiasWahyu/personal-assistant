# Panduan Deploy Bot ke Server Cloud (24/7 Nonstop)

Bot asisten ini dapat berjalan 24 jam nonstop tanpa perlu laptop Anda menyala terus-menerus. Database sudah tersimpan di cloud (Supabase), sehingga data Anda aman dan sinkron.

---

## Opsi 1: Google Cloud Platform (GCP) - Always Free (100% Gratis Selamanya)

### Langkah 1: Buat Virtual Machine (VM) di Google Cloud
1. Buka [Google Cloud Console](https://console.cloud.google.com/).
2. Masuk ke **Compute Engine** > **VM instances** > Klik **Create Instance**.
3. Atur spesifikasi **Always Free Tier**:
   - **Name**: `personal-assistant`
   - **Region**: `us-central1`, `us-west1`, atau `us-east1` (Region Free Tier)
   - **Machine type**: `e2-micro` (2 vCPU, 1 GB memory)
   - **Boot disk**: Ubuntu 22.04 LTS / 24.04 LTS (Standard persistent disk 30 GB)
   - **Firewall**: Centang *Allow HTTP traffic* dan *Allow HTTPS traffic*.
4. Klik **Create**.

---

### Langkah 2: Masuk ke Terminal SSH VM
Setelah VM aktif, klik tombol **SSH** di sebelah nama VM Anda untuk membuka terminal browser.

---

### Langkah 3: Jalankan Perintah Deploy (1 Langkah Selesai)
Di terminal SSH VM, cukup jalankan perintah berikut:

```bash
# 1. Clone repositori Anda
git clone https://github.com/GhiasWahyu/personal-assistant.git
cd personal-assistant

# 2. Beri izin eksekusi & jalankan skrip instalasi otomatis
chmod +x deploy_gcp/setup_server.sh
./deploy_gcp/setup_server.sh
```

Skrip ini akan otomatis:
* Menginstall Python, Node.js, dan semua dependensi library.
* Menyiapkan background systemd service (`assistant-telegram` & `assistant-whatsapp`).
* Menjadikan bot aktif otomatis 24 jam dan otomatis menyala ulang jika server restart.

---

## Cek Status & Log di Server

Untuk melihat status bot kapan saja di server:
```bash
# Cek status Telegram Bot
sudo systemctl status assistant-telegram

# Cek status WhatsApp Gateway
sudo systemctl status assistant-whatsapp

# Cek Log Realtime
journalctl -u assistant-telegram -f
```

---

## Menghubungkan WhatsApp di Server
Jika Anda ingin menghubungkan WhatsApp di server:
1. Buka browser dan akses: `http://IP_EKSTERNAL_VM:3000` (atau gunakan SSH tunnel: `ssh -L 3000:localhost:3000 user@IP_VM`).
2. Masukkan nomor WhatsApp Anda untuk mendapatkan Pairing Code 8 digit, atau scan QR Code.
