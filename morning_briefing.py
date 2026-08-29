#!/usr/bin/env python3
"""
==============================================================================
PROACTIVE MORNING BRIEFING RUNNER
Dijalankan secara otomatis oleh GitHub Actions Scheduled Cron (07:00 WIB)
atau dapat dijalankan secara mandiri di lokal.
==============================================================================
"""

import os
import sys
import json
import logging
import datetime
import urllib.request
import urllib.parse
from contextlib import contextmanager

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("MorningBriefing")

# Try loading .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
    # Also check inside telegram_bot folder
    env_bot_path = os.path.join(os.path.dirname(__file__), "telegram_bot", ".env")
    if os.path.exists(env_bot_path):
        load_dotenv(env_bot_path)
except Exception:
    pass

def sanitize_db_url(raw_url: str) -> str:
    if not raw_url:
        return raw_url
    if raw_url.count('@') > 1 and '://' in raw_url:
        prefix, rest = raw_url.split('://', 1)
        last_at = rest.rfind('@')
        creds = rest[:last_at]
        host_db = rest[last_at+1:]
        if ':' in creds:
            user, pwd = creds.split(':', 1)
            pwd_encoded = urllib.parse.quote_plus(urllib.parse.unquote_plus(pwd))
            return f"{prefix}://{user}:{pwd_encoded}@{host_db}"
    return raw_url

# Configuration
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN belum di-set di environment variable")

_raw_db_url = os.getenv("DATABASE_URL")
if not _raw_db_url:
    raise RuntimeError("DATABASE_URL belum di-set di environment variable")
DB_URL = sanitize_db_url(_raw_db_url)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY belum di-set di environment variable")

DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def get_target_chat_id():
    """Mendapatkan chat_id tujuan dari file config.json atau fallback env."""
    config_paths = [
        os.path.join(os.path.dirname(__file__), "telegram_bot", "config.json"),
        os.path.join(os.path.dirname(__file__), "config.json")
    ]
    for cp in config_paths:
        if os.path.exists(cp):
            try:
                with open(cp, 'r') as f:
                    data = json.load(f)
                    if data.get("chat_id"):
                        return data.get("chat_id")
            except Exception as e:
                logger.warning(f"Error reading {cp}: {e}")
    return DEFAULT_CHAT_ID

@contextmanager
def get_db():
    import psycopg2
    db_url_clean = DB_URL
    if "sslmode=" not in db_url_clean:
        sep = "&" if "?" in db_url_clean else "?"
        db_url_clean += f"{sep}sslmode=require"
    
    conn = psycopg2.connect(db_url_clean, connect_timeout=15)
    try:
        yield conn
    finally:
        if conn and not conn.closed:
            conn.close()

def get_live_weather_and_news(location_query: str = "Purwakarta Jawa Barat") -> str:
    """Mencari prakiraan cuaca & konteks info pagi hari secara live."""
    results_text = []
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            # 1. Prakiraan cuaca
            weather_query = f"prakiraan cuaca hari ini {location_query}"
            w_res = list(ddgs.text(weather_query, max_results=2))
            if w_res:
                results_text.append(f"Cuaca: {w_res[0].get('title', '')} - {w_res[0].get('body', '')}")
            
            # 2. Berita singkat pagi
            news_query = "berita ekonomi keuangan indonesia hari ini"
            n_res = list(ddgs.text(news_query, max_results=2))
            if n_res:
                results_text.append(f"Konteks Finansial/Pagi: {n_res[0].get('title', '')} - {n_res[0].get('body', '')}")
    except Exception as e:
        logger.warning(f"Live web search note: {e}")
    
    if not results_text:
        return "Prakiraan cuaca umum cerah berawan, suhu sejuk di pagi hari."
    return "\n".join(results_text)

def gather_context_data():
    """Mengumpulkan data keuangan, agenda, memori, dan cuaca nyata."""
    import psycopg2.extras
    import holidays

    now = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    HARI_ID = ['Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu', 'Minggu']
    BULAN_ID = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']
    
    id_holidays = holidays.country_holidays('ID', years=[now.year, now.year + 1])
    hari_ini_nama = HARI_ID[now.weekday()]
    tgl_ini_str = f"{hari_ini_nama}, {now.day} {BULAN_ID[now.month]} {now.year}"
    
    status_hari = "Hari Kerja Normal"
    if now.date() in id_holidays:
        status_hari = f"TANGGAL MERAH / LIBUR NASIONAL ({id_holidays[now.date()]})"
    elif now.weekday() >= 5:
        status_hari = "AKHIR PEKAN (Weekend)"

    dompet_summary = []
    budget_summary = []
    agenda_today = []
    memory_list = []
    sisa_hari_gajian = 0
    rekomendasi_harian = 0

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
            # 1. Saldo Dompet
            c.execute("""
                SELECT COALESCE(NULLIF(TRIM(dompet), ''), 'Cash') as nama_dompet,
                       COALESCE(SUM(CASE WHEN tipe = 'pemasukan' THEN jumlah ELSE -jumlah END), 0) as saldo
                FROM transaksi
                GROUP BY COALESCE(NULLIF(TRIM(dompet), ''), 'Cash')
                ORDER BY saldo DESC;
            """)
            for r in c.fetchall():
                dompet_summary.append(f"{r['nama_dompet']}: Rp {int(r['saldo']):,}")

            # 2. Budget & Sisa Hari Menuju Gajian
            tanggal_gajian = 25
            if now.day >= tanggal_gajian:
                next_month = now.month + 1 if now.month < 12 else 1
                next_year = now.year if now.month < 12 else now.year + 1
                next_payday = datetime.date(next_year, next_month, tanggal_gajian)
            else:
                next_payday = datetime.date(now.year, now.month, tanggal_gajian)
                
            sisa_hari = (next_payday - now.date()).days
            sisa_hari_gajian = sisa_hari if sisa_hari > 0 else 1

            c.execute("""
                SELECT k.nama, b.limit_nominal,
                       COALESCE(SUM(t.jumlah), 0) as terpakai
                FROM budget b 
                JOIN kategori k ON b.kategori_id = k.id 
                LEFT JOIN transaksi t ON t.kategori_id = k.id 
                      AND EXTRACT(MONTH FROM t.tanggal) = b.bulan 
                      AND EXTRACT(YEAR FROM t.tanggal) = b.tahun
                      AND t.tipe = 'pengeluaran'
                WHERE b.bulan = %s AND b.tahun = %s
                GROUP BY k.nama, b.limit_nominal
            """, (now.month, now.year))
            
            sisa_pengeluaran = 0
            for r in c.fetchall():
                limit = int(r['limit_nominal'])
                terpakai = int(r['terpakai'])
                sisa = limit - terpakai
                if r['nama'].lower() != 'tabungan':
                    sisa_pengeluaran += sisa
                budget_summary.append(f"{r['nama']}: Sisa Rp {sisa:,} (Limit Rp {limit:,})")

            if sisa_pengeluaran > 0 and sisa_hari_gajian > 0:
                rekomendasi_harian = int(sisa_pengeluaran / sisa_hari_gajian)

            # 3. Agenda Hari Ini
            c.execute("""
                SELECT jam_mulai, jam_selesai, catatan 
                FROM jadwal_kerja 
                WHERE tanggal = %s 
                ORDER BY jam_mulai ASC
            """, (today_str,))
            for r in c.fetchall():
                jam_m = str(r['jam_mulai'])[:5]
                jam_s = f"-{str(r['jam_selesai'])[:5]}" if r['jam_selesai'] else ""
                agenda_today.append(f"• {jam_m}{jam_s} WIB: {r['catatan']}")

            # 4. Long-Term Memory Graph
            try:
                c.execute("SELECT topik, isi_memori, kategori FROM long_term_memory ORDER BY updated_at DESC LIMIT 10")
                for r in c.fetchall():
                    memory_list.append(f"[{r['topik']}] {r['isi_memori']}")
            except Exception:
                pass

    # Tentukan query lokasi dari memory jika ada
    location_target = "Purwakarta Jawa Barat"
    for m in memory_list:
        if "tinggal" in m.lower() or "domisili" in m.lower() or "kota" in m.lower() or "lokasi" in m.lower():
            location_target = m
            break

    live_web_info = get_live_weather_and_news(location_target)

    return {
        "tanggal_str": tgl_ini_str,
        "status_hari": status_hari,
        "dompet": dompet_summary,
        "budget": budget_summary,
        "sisa_hari_gajian": sisa_hari_gajian,
        "rekomendasi_harian": rekomendasi_harian,
        "agenda": agenda_today,
        "memory": memory_list,
        "live_info": live_web_info
    }

def generate_briefing_message(data: dict) -> str:
    """Menggunakan Gemini AI untuk merangkum briefing pagi yang hangat, cerdas, dan elegan."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
Kamu adalah asisten pribadi cerdas, profesional, dan hangat bernama Asisten GWS.
Tugasmu adalah membuat pesan MORNING BRIEFING (Selamat Pagi) yang ringkas, elegan, sangat rapi, dan memotivasi untuk pengguna (Mas Ghias) setiap jam 07:00 pagi.

Data Nyata Pagi Ini:
- Hari & Tanggal: {data['tanggal_str']}
- Status Hari: {data['status_hari']}
- Live Info / Cuaca: {data['live_info']}
- Agenda Jadwal Hari Ini: {json.dumps(data['agenda'], ensure_ascii=False) if data['agenda'] else 'Tidak ada agenda tertulis untuk hari ini'}
- Sisa Hari Menuju Gajian (Tgl 25): {data['sisa_hari_gajian']} hari
- Batas Aman Pengeluaran Hari Ini: Rp {data['rekomendasi_harian']:,}
- Saldo Dompet: {', '.join(data['dompet'])}
- Sisa Pos Anggaran: {', '.join(data['budget'])}
- Ingatan Jangka Panjang Pengguna: {json.dumps(data['memory'], ensure_ascii=False) if data['memory'] else 'Belum ada ingatan khusus'}

Format Pesan yang Diharapkan:
1. Salam pembuka pagi yang ramah dan menyegarkan (sapa Mas Ghias).
2. 🌤️ Cuaca & Kondisi Pagi (ringkas dari live info).
3. 📅 Agenda Hari Ini (tampilkan dengan rapi, atau berikan ucapan selamat istirahat/fokus produktif jika tidak ada jadwal).
4. 💰 Sorotan Keuangan Hari Ini (sebutkan batas aman pengeluaran hari ini Rp {data['rekomendasi_harian']:,} dan sisa {data['sisa_hari_gajian']} hari menuju gajian).
5. 💡 Catatan / Pengingat Personal (satu kalimat pengingat cerdas berdasarkan Long-Term Memory atau tips semangat harian).
6. Penutup yang menyenangkan.

ATURAN WAJIB:
- DILARANG menggunakan tanda bintang ganda (**kata**) atau (*kata*). Tulis secara plain text yang rapi dan bersih.
- Gunakan emoji pendukung yang proporsional (🌅, 🌤️, 📅, 💰, 💡, ✨).
- Bahasa Indonesia yang natural, sopan, dan efisien.
"""

    MODELS = [
        os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        "gemini-3.5-flash",
        "gemini-3.7-flash",
        "gemini-flash-latest"
    ]

    for m in MODELS:
        try:
            res = client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.7)
            )
            if res and res.text:
                text = res.text.replace("**", "").replace("*", "").replace("#", "").strip()
                return text
        except Exception as e:
            logger.warning(f"Model {m} failed: {e}")
            continue

    # Fallback template if all API calls fail
    return (
        f"🌅 Selamat Pagi, Mas Ghias!\n\n"
        f"📅 {data['tanggal_str']} ({data['status_hari']})\n\n"
        f"🌤️ Info Pagi: Cuaca cerah berawan, siapkan hari dengan baik.\n\n"
        f"📅 Agenda Hari Ini:\n" + ("\n".join(data['agenda']) if data['agenda'] else "Tidak ada agenda tercatat untuk hari ini.") + "\n\n"
        f"💰 Status Finansial:\n"
        f"• Sisa hari menuju gajian (Tgl 25): {data['sisa_hari_gajian']} hari\n"
        f"• Batas pengeluaran aman hari ini: Rp {data['rekomendasi_harian']:,}\n\n"
        f"💡 Semoga hari Anda produktif dan menyenangkan! ✨"
    )

def send_telegram_message(chat_id: str, text: str):
    """Mengirimkan pesan ke Telegram Bot via HTTP API."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text
    }).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status == 200

def main():
    logger.info("Memulai pembuatan Morning Briefing...")
    chat_id = get_target_chat_id()
    if not chat_id:
        logger.error("Chat ID tidak ditemukan. Batalkan pengiriman.")
        sys.exit(1)

    try:
        data = gather_context_data()
        briefing_text = generate_briefing_message(data)
        logger.info(f"Pesan Briefing berhasil disusun:\n{briefing_text}")

        # Kirim ke Telegram
        success = send_telegram_message(chat_id, briefing_text)
        if success:
            logger.info(f"✅ Morning Briefing berhasil dikirim ke Telegram ({chat_id})!")
        else:
            logger.error("❌ Gagal mengirim pesan ke Telegram.")
    except Exception as e:
        logger.error(f"Error menjalankan Morning Briefing: {e}", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    main()
