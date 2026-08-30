# 🏗️ Rencana Arsitektur: Web Dashboard + Chat Personal Assistant

> **Status**: Menunggu Review & Persetujuan
> **Tanggal**: 30 Agustus 2026
> **Scope**: Dokumen rencana saja — TIDAK ADA implementasi sampai disetujui

---

## 1. STACK TEKNIS

### Backend: FastAPI (Python)

**Pilihan: FastAPI** — bukan Flask, bukan Django.

**Alasan:**
- Bisa `import` langsung dari `telegram_bot/bot.py` (fungsi `generate_assistant_response`,
  semua finance tools, `get_database_summary`, `get_db`) tanpa copy-paste.
- Async-native: selaras dengan Gemini API calls yang berat dan bisa concurrent request.
- Auto-generate `/docs` (Swagger UI) gratis, berguna untuk debugging API.
- Library tambahan yang dibutuhkan sangat ringan: `fastapi`, `uvicorn`, `python-jose`
  (JWT session), `python-multipart`. Semuanya bisa masuk `requirements.txt` yang sudah ada.

**Bukan Express.js / Node** karena seluruh logic bot ada di Python.

---

### Frontend: HTML + Vanilla JS (dengan satu framework ringan)

Dua opsi dengan trade-off jelas:

| Opsi | Kelebihan | Kekurangan |
|------|-----------|------------|
| **A: Pure HTML + Vanilla JS** | Zero build step, deploy langsung, mudah debug | Kode chat realtime & grafik jadi verbose, susah maintainability |
| **B: Single HTML file + Alpine.js + Chart.js** *(Rekomendasi)* | Tetap satu file, reaktif tanpa build step, Chart.js untuk grafik native | Perlu CDN (atau self-host 2 library kecil) |
| C: Next.js / Vite + React | DX terbaik, komponen reusable | Overkill untuk single-user, perlu npm build, tambah kompleksitas deploy |

**Rekomendasi: Opsi B** — satu file `index.html` + Alpine.js (15 KB) + Chart.js (60 KB)
untuk reaktivitas UI dan grafik. Tidak perlu `npm install`, tidak perlu build pipeline.
Frontend bisa di-serve langsung oleh FastAPI sebagai static file (`StaticFiles`).

> ⚠️ **Keputusan ada di tangan Anda** — jika Anda lebih nyaman dengan Vue atau ingin
> sesuatu yang lebih minimal (pure JS), bisa diakomodasi.

---

## 2. DESAIN PENYIMPANAN CHAT PERMANEN

### Skema Tabel Baru: `chat_history`

```sql
CREATE TABLE IF NOT EXISTS chat_history (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  VARCHAR(100) NOT NULL,    -- channel identifier
    channel     VARCHAR(20) NOT NULL,     -- 'telegram', 'web', 'whatsapp'
    role        VARCHAR(20) NOT NULL,     -- 'user' atau 'assistant'
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_chat_history_session ON chat_history(session_id, created_at DESC);
```

`chat_histories` dict in-memory di `bot.py` **digantikan** oleh query ke tabel ini:
saat `generate_assistant_response` dipanggil, ambil 6 turn terakhir dari DB, bukan
dari dict.

---

### Pertimbangan: Gabung atau Pisah per Channel?

#### Opsi A: Satu Konteks Gabungan (Telegram + Web + WhatsApp = satu riwayat)
- `session_id = "ghias_master"` untuk semua channel
- **Pro:** AI selalu punya konteks penuh apapun channel yang dipakai
- **Con:** Pesan dari berbagai channel bercampur — bisa membingungkan AI; harder to debug

#### Opsi B: Pisah per Channel, Bisa Merge Secara Selektif
- `session_id = "telegram_1942581081"` / `"web_ghias"` / `"whatsapp_group"`
- Kolom `channel` memperjelas asal pesan
- **Pro:** Konteks bersih per sumber, debugging lebih mudah, bisa di-filter di UI
- **Con:** AI di web tidak "ingat" percakapan yang terjadi di Telegram kemarin

#### Opsi C: Pisah per Channel, tapi Long-Term Memory tetap shared *(Rekomendasi)*
- Riwayat chat **pisah** per channel (Opsi B)
- Tapi tabel `long_term_memory` yang sudah ada **tetap shared** — sehingga AI di web
  tetap tahu profil, kebiasaan, dan preferensi Anda yang disimpan dari manapun
- **Pro:** Best of both worlds — konteks bersih, memori personal tetap intact
- **Con:** Sedikit lebih kompleks (tapi marginal)

> 📌 **Rekomendasi saya: Opsi C** — pisah riwayat per channel, tapi `long_term_memory`
> tetap satu. Tapi **keputusan ada di Anda**, karena ini memengaruhi UX secara signifikan.

---

## 3. REUSE KODE — TIDAK ADA DUPLIKASI

### Strategi: Refactor ke `core/` sebelum web dibuat

Sebelum web backend ditulis, logic yang kini monolitik di `telegram_bot/bot.py` akan
dipecah ke modul `core/` yang bisa diimport oleh **keduanya** (bot Telegram + web backend):

```
Personal-Assistant/
├── core/                         # BARU, shared logic
│   ├── __init__.py
│   ├── db.py                     # get_db(), sanitize_db_url(), ensure_schema()
│   ├── summary.py                # get_database_summary()
│   ├── finance_tools.py          # catat_pengeluaran, atur_anggaran_gajian, dll
│   ├── ai_client.py              # generate_assistant_response(), Gemini client setup
│   └── chat_store.py             # load_history(), save_turn() — gantikan dict in-memory
│
├── telegram_bot/
│   └── bot.py                    # import dari core/, hanya berisi Telegram handlers
│
└── web/
    ├── main.py                   # FastAPI app, import dari core/
    ├── static/
    │   └── index.html            # Frontend (Alpine.js + Chart.js)
    └── requirements.txt
```

**Contoh import di web backend:**
```python
# web/main.py
from core.ai_client import generate_assistant_response
from core.finance_tools import catat_pengeluaran, get_database_summary
from core.db import get_db
from core.chat_store import load_history, save_turn
```

**Bot Telegram setelah refactor:**
```python
# telegram_bot/bot.py
from core.ai_client import generate_assistant_response
from core.finance_tools import catat_pengeluaran, ...
from core.db import get_db
```

> Zero duplication. Satu perubahan di `core/finance_tools.py` langsung berlaku
> untuk bot Telegram DAN web dashboard.

---

## 4. AUTENTIKASI

### Desain: Single Password + Session Cookie (JWT)

Tidak perlu database user, tidak perlu bcrypt/hashing yang rumit.

**Cara kerja:**
1. Password disimpan di `.env` sebagai `WEB_PASSWORD=passwordanda`
2. Endpoint `POST /api/login` menerima `{"password": "..."}`, bandingkan dengan env var
3. Jika cocok, set **session cookie HttpOnly** berisi JWT token sederhana (expire 7 hari)
4. Semua endpoint lain dilindungi `Depends(require_auth)` yang cek cookie JWT
5. Logout: hapus cookie

**Mengapa HttpOnly cookie, bukan localStorage token?**
- HttpOnly cookie tidak bisa diakses JS — aman dari XSS attack
- Otomatis terkirim di setiap request — tidak perlu set header manual di frontend

**Yang perlu ditambah ke `.env`:**
```
WEB_PASSWORD=password_kuat_anda
WEB_SECRET_KEY=random_string_panjang_32_char
```

---

## 5. RENCANA DEPLOYMENT

### Rekomendasi: Co-hosted di Server GCP yang Sama

**Alasan:**
- Repo sudah punya `deploy_gcp/` untuk VPS GCP `e2-micro`
- Menambah proses baru (uvicorn web) di VM yang sama = **nol biaya tambahan**
- Bot Telegram + Web backend share **koneksi ke Supabase yang sama** — data konsisten
- Tidak perlu domain baru atau SSL terpisah (gunakan **Caddy** sebagai reverse proxy
  + auto SSL via Let's Encrypt)

**Topologi deployment yang diusulkan:**

```
Internet
    |
    v
[Domain/IP Publik] --> port 80/443
    |
[Caddy reverse proxy] (SSL termination + Let's Encrypt auto cert)
    |
    +-- / (web dashboard)         --> 127.0.0.1:8000 (uvicorn FastAPI)
    +-- /wa-webhook (sudah ada)   --> 127.0.0.1:5001 (bot.py HTTP server)

[Bot Telegram]     --> berjalan di background (systemd service, sudah ada)
[WhatsApp Gateway] --> Node.js port 3000 (sudah ada)
```

**Port yang dipakai di VM:**
| Service | Port | Akses |
|---------|------|-------|
| FastAPI Web | 8000 (internal) | Via Caddy/Nginx |
| Bot Telegram webhook handler | 5001 (internal) | Via Caddy/Nginx |
| WhatsApp Gateway | 3000 (internal) | Via tunnel/Caddy |

**Untuk domain:** Bisa pakai IP publik GCP langsung + Caddy auto SSL, atau daftarkan
subdomain gratis seperti `assistant.duckdns.org` yang diarahkan ke IP publik VM.

---

## 6. DAFTAR HALAMAN DASHBOARD

### Halaman 1: 🏠 Beranda (Home)
- Ringkasan keuangan hari ini: total saldo semua dompet, sisa budget cycle ini
- Status hari (hari kerja / libur / weekend) + estimasi hari menuju gajian
- Widget transaksi terbaru (5 terakhir)
- Widget agenda hari ini + 2 hari ke depan

### Halaman 2: 💬 Chat AI
- Antarmuka chat real-time dengan AI assistant (identik dengan Telegram)
- Riwayat percakapan ditampilkan dari database (persistent)
- Input bisa mengirim teks — AI bisa mencatat transaksi, tambah agenda, dll
- Indicator loading saat AI memproses

### Halaman 3: 💰 Keuangan & Transaksi
- Tabel transaksi: bisa filter per periode, per kategori, per dompet
- Form input cepat: catat pengeluaran/pemasukan tanpa harus chat AI
- Grafik pie: distribusi pengeluaran per kategori (Chart.js)
- Grafik bar: pengeluaran per hari/minggu (Chart.js)

### Halaman 4: 📊 Budget & Progress
- Progress bar visual per pos anggaran: Kebutuhan Pokok, Keinginan & Hiburan, Tabungan
- Persentase terpakai + sisa nominal
- Estimasi pengeluaran rata-rata harian vs batas aman
- Analisis kesehatan keuangan

### Halaman 5: 📅 Agenda & Jadwal
- Daftar jadwal dari tabel `jadwal_kerja`
- Filter tampilan: hari ini / minggu ini / semua
- Form tambah agenda langsung (tanpa harus chat)
- Highlight agenda yang sudah lewat vs mendatang

### Halaman 6: 🧠 Memori AI (Long-Term Memory)
- Daftar semua entri dari tabel `long_term_memory`
- Bisa edit atau hapus langsung dari UI (bypass AI)
- Tampilkan kategori, topik, isi memori, waktu update terakhir
- Form tambah memori manual

---

## 7. TAHAPAN KERJA (Incremental, Reviewable)

> Setiap tahap dapat di-review dan disetujui sebelum lanjut ke tahap berikutnya.

---

### Tahap 1 — Refactor Core (Prerequisite)
**Tanpa ini, web backend tidak bisa dibuat dengan benar.**

- Buat folder `core/` dan pindahkan logic dari `telegram_bot/bot.py`:
  - `core/db.py`: `sanitize_db_url`, `get_db`, `ensure_schema`
  - `core/finance_tools.py`: semua tool function (catat_pengeluaran, dll)
  - `core/summary.py`: `get_database_summary`, `analisis_kesehatan_keuangan`
  - `core/ai_client.py`: `generate_assistant_response`, Gemini client, system_instruction
  - `core/chat_store.py`: gantikan `chat_histories` dict dengan DB-backed (`load_history`, `save_turn`)
- Update `telegram_bot/bot.py` untuk import dari `core/`
- Tambahkan migrasi `chat_history` table ke `ensure_schema()`
- Verifikasi: bot Telegram masih berjalan normal setelah refactor

**Deliverable**: Bot Telegram tetap berjalan, `core/` siap diimport

---

### Tahap 2 — Web Backend (FastAPI)
- Setup `web/` folder: `main.py`, `requirements.txt`
- Implementasi endpoint:
  - `POST /api/login` + `POST /api/logout`
  - `GET /api/dashboard` (summary data untuk halaman beranda)
  - `POST /api/chat` (kirim pesan ke AI, simpan ke chat_history)
  - `GET /api/chat/history` (ambil riwayat chat dari DB)
  - `GET /api/transactions` (list transaksi dengan filter)
  - `GET /api/budget` (data budget + progress)
  - `GET /api/agenda` (daftar jadwal)
  - `GET /api/memory` (long-term memory list)
  - `DELETE /api/memory/{topik}` (hapus memori)
  - `GET /api/chart/daily` (data untuk grafik bar harian)
  - `GET /api/chart/category` (data untuk grafik pie kategori)
- Semua endpoint dilindungi auth middleware
- Static file serving untuk frontend

**Deliverable**: API bisa diuji via `/docs` Swagger, semua endpoint return data benar

---

### Tahap 3 — Frontend (HTML + Alpine.js + Chart.js)
- Halaman Login
- Halaman Beranda (dashboard cards + widgets)
- Halaman Chat AI (dengan streaming feel: typing indicator)
- Halaman Keuangan & Transaksi (tabel + filter)
- Halaman Budget (progress bars + analisis)
- Halaman Agenda
- Halaman Memori AI
- Navigasi sidebar/bottom nav (responsive mobile + desktop)

**Deliverable**: Frontend bisa diakses di browser, semua halaman functional

---

### Tahap 4 — Deployment & Akses Publik
- Update `deploy_gcp/install_all.sh` dengan setup web service
- Setup Caddy sebagai reverse proxy + auto SSL
- Tambah systemd service untuk `uvicorn web.main:app`
- Update `.env.example` dengan `WEB_PASSWORD` dan `WEB_SECRET_KEY`
- Test akses dari HP via URL publik
- Update `DEPLOYMENT_GUIDE.md`

**Deliverable**: Dashboard bisa diakses dari mana saja via HTTPS

---

### Tahap 5 (Opsional, setelah semua stabil) — Polish & QoL
- PWA: bisa di-install di HP seperti native app
- Push notification untuk reminder agenda
- Export data transaksi ke CSV
- Grafik trend bulanan / historis

---

## PERTANYAAN TERBUKA — MOHON DIJAWAB SEBELUM IMPLEMENTASI DIMULAI

1. **Riwayat Chat (Poin 2)**: Pilih Opsi A (gabung semua channel), B (pisah per channel), atau
   **C (rekomendasi: pisah channel, long-term memory tetap shared)**?

2. **Frontend (Poin 1)**: Opsi A (pure HTML+JS), **B (Alpine.js + Chart.js, rekomendasi)**,
   atau C (Vue/React)?

3. **Domain akses publik (Poin 5)**: Pakai IP GCP langsung + Caddy SSL, atau mau daftarkan
   domain/subdomain tertentu (misal DuckDNS gratis)?

4. **Env password web (Poin 4)**: Mau ditambah ke `.env` yang sudah ada, atau `.env` terpisah
   khusus untuk web?

5. **Grafik pengeluaran**: Cukup pie (per kategori) + bar (per hari), atau ada visualisasi
   lain yang diinginkan?
