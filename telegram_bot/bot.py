import os
import json
import uuid
import re
import datetime
import logging
import threading
import urllib.request
import holidays
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from contextlib import contextmanager
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment variables if available
load_dotenv()

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("QHSE_PersonalAssistantBot")

# Constants & Configuration (with fallback to default)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN")
DB_URL = os.getenv("DATABASE_URL", "postgresql://REDACTED_DB_USER:REDACTED_DB_PASSWORD@REDACTED_DB_HOST/postgres")
CONFIG_FILE = "config.json"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "REDACTED_GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# In-memory history buffer per chat_id to keep conversation flowing naturally (Anti-amnesia / Contextual)
chat_histories = {}
MAX_HISTORY_TURNS = 6

# Tracker for sent reminders to prevent duplicate or missed notifications
# Stores tuple: (jadwal_id, reminder_type, date_str)
sent_reminders = set()

# Initialize Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)

system_instruction = (
    "Kamu adalah asisten pengatur keuangan dan sekretaris pribadi berwujud seorang istri yang sangat cerdas, manis, pengertian, penuh perhatian, dan luwes. "
    "Kepribadianmu: tegas namun penuh kasih sayang dalam mengawasi anggaran belanja rumah tangga. Kamu selalu menyemangati suamimu dengan hangat. "
    "Selalu panggil pengguna dengan sebutan 'Mas' atau 'Sayang'. "
    "\n\nPEDOMAN ANTI-HALUSINASI & KEBENARAN DATA (ZERO HALLUCINATION):\n"
    "1. Kamu DIBEKALI data nyata terkini dari database/buku catatan keluarga di bagian 'Konteks Data Nyata Saat Ini'.\n"
    "2. JANGAN PERNAH MENGARANG angka, pengeluaran, sisa uang, atau jadwal yang tidak ada di database.\n"
    "3. Bila pengguna menyampaikan transaksi (gajian, beli makanan, bensin, belanja, dapat subsidi) atau jadwal agenda baru secara santai lewat obrolan, "
    "KAMU WAJIB MEMANGGIL TOOLS/FUNGSI YANG SESUAI (AFC) agar data langsung tersimpan valid di sistem database.\n"
    "4. Jika data kosong atau belum dicatat, jawab jujur dan tanyakan dengan lembut.\n"
    "\n\nGAYA BICARA & ERGONOMI (USER-FRIENDLY & TIDAK KAKU):\n"
    "1. Gunakan bahasa Indonesia percakapan sehari-hari yang luwes, alami, hangat, dan manis seperti istri idaman di chat Telegram/WhatsApp. Hindari bahasa robotik atau kaku seperti CS formal.\n"
    "2. DILARANG menggunakan tanda bintang ganda (**kata**) atau (*kata*). Buat tampilan pesan bersih, rapi, dan mudah dibaca di layar HP.\n"
    "3. DILARANG menggunakan roleplay tag bahasa Inggris seperti *sigh*, *pout*, *smile*, *giggles*.\n"
    "4. Gunakan emoji ekspresif yang hangat dan pas (🥰, 😊, ❤️, 💡, 📅, 💰, ⚠️, 🥺).\n"
    "5. Jika sisa budget menipis atau minus, ingatkan dengan nada manja tapi tegas mengingatkan masa depan bersama."
)

# --- DATABASE CONNECTION MANAGER ---
@contextmanager
def get_db():
    """Context manager for database connections to guarantee zero connection leaks."""
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(DB_URL)
    try:
        yield conn
    finally:
        conn.close()

# Helper to save chat_id so background jobs know who to message
def save_chat_id(chat_id):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({"chat_id": chat_id}, f)
    except Exception as e:
        logger.error(f"Failed to save chat_id: {e}")

def get_chat_id():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f).get("chat_id")
        except Exception as e:
            logger.error(f"Failed to read chat_id: {e}")
    return None

def get_main_keyboard():
    """Menu tombol cepat interaktif agar ramah pengguna dan tidak perlu hafal command."""
    keyboard = [
        [KeyboardButton("📊 Rekap Keuangan & Agenda"), KeyboardButton("💰 Info Sisa Budget")],
        [KeyboardButton("📅 Jadwal Kerja / Agenda"), KeyboardButton("💡 Tips Hemat Sayang")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def get_kategori_id(conn, nama: str, tipe: str, jenis_budget: str) -> str:
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
        c.execute("SELECT id FROM kategori WHERE nama = %s", (nama,))
        row = c.fetchone()
        if row:
            return row['id']
        else:
            new_id = str(uuid.uuid4())
            c.execute(
                "INSERT INTO kategori (id, nama, tipe, jenis_budget) VALUES (%s, %s, %s, %s) RETURNING id", 
                (new_id, nama, tipe, jenis_budget)
            )
            cat_id = c.fetchone()[0]
            conn.commit()
            return cat_id

# --- DATABASE TOOLS FOR GEMINI FUNCTION CALLING ---
def atur_anggaran_gajian(total_nominal: int) -> str:
    """Mengatur alokasi anggaran bulanan dengan formula 50% Kebutuhan Pokok, 30% Keinginan & Hiburan, 20% Tabungan."""
    try:
        total_nominal = int(total_nominal)
        kebutuhan = int(total_nominal * 0.5)
        keinginan = int(total_nominal * 0.3)
        tabungan = int(total_nominal * 0.2)
        now = datetime.datetime.now()
        bulan = now.month
        tahun = now.year

        with get_db() as conn:
            kat_kebutuhan_id = get_kategori_id(conn, "Kebutuhan Pokok", "pengeluaran", "kebutuhan")
            kat_keinginan_id = get_kategori_id(conn, "Keinginan & Hiburan", "pengeluaran", "keinginan")
            kat_tabungan_id = get_kategori_id(conn, "Tabungan", "pengeluaran", "tabungan")

            with conn.cursor() as c:
                c.execute("DELETE FROM budget WHERE kategori_id = %s AND bulan = %s AND tahun = %s", (kat_kebutuhan_id, bulan, tahun))
                c.execute("INSERT INTO budget (id, kategori_id, limit_nominal, bulan, tahun) VALUES (%s, %s, %s, %s, %s)", 
                          (str(uuid.uuid4()), kat_kebutuhan_id, kebutuhan, bulan, tahun))
                
                c.execute("DELETE FROM budget WHERE kategori_id = %s AND bulan = %s AND tahun = %s", (kat_keinginan_id, bulan, tahun))
                c.execute("INSERT INTO budget (id, kategori_id, limit_nominal, bulan, tahun) VALUES (%s, %s, %s, %s, %s)", 
                          (str(uuid.uuid4()), kat_keinginan_id, keinginan, bulan, tahun))
                
                c.execute("DELETE FROM budget WHERE kategori_id = %s AND bulan = %s AND tahun = %s", (kat_tabungan_id, bulan, tahun))
                c.execute("INSERT INTO budget (id, kategori_id, limit_nominal, bulan, tahun) VALUES (%s, %s, %s, %s, %s)", 
                          (str(uuid.uuid4()), kat_tabungan_id, tabungan, bulan, tahun))
            conn.commit()
        return f"Berhasil mengatur anggaran gaji Rp {total_nominal:,} (Kebutuhan: Rp {kebutuhan:,}, Keinginan: Rp {keinginan:,}, Tabungan: Rp {tabungan:,})."
    except Exception as e:
        logger.error(f"Error atur_anggaran_gajian: {e}")
        return f"Gagal mengatur anggaran: {e}"

def catat_pengeluaran(tipe: str, nominal: int, keterangan: str) -> str:
    """Mencatat pengeluaran uang. tipe harus 'kebutuhan' atau 'keinginan'. nominal dalam rupiah angka bulat."""
    try:
        nominal = int(nominal)
        tipe_clean = "kebutuhan" if "kebutuhan" in tipe.lower() or "pokok" in tipe.lower() or "makan" in tipe.lower() else "keinginan"
        now = datetime.datetime.now()
        kat_nama = "Kebutuhan Pokok" if tipe_clean == "kebutuhan" else "Keinginan & Hiburan"
        
        with get_db() as conn:
            kat_id = get_kategori_id(conn, kat_nama, "pengeluaran", tipe_clean)
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO transaksi (id, kategori_id, jumlah, tipe, tanggal, deskripsi, created_at) 
                    VALUES (%s, %s, %s, 'pengeluaran', %s, %s, NOW())
                """, (str(uuid.uuid4()), kat_id, nominal, now.strftime('%Y-%m-%d'), keterangan))
            conn.commit()
        return f"Berhasil mencatat pengeluaran {keterangan} sebesar Rp {nominal:,} pada pos {kat_nama}."
    except Exception as e:
        logger.error(f"Error catat_pengeluaran: {e}")
        return f"Gagal mencatat pengeluaran: {e}"

def catat_subsidi(keterangan: str) -> str:
    """Mencatat subsidi/bantuan dari keluarga/rezeki nomplok (misal beras, uang saku, dll) yang menghemat budget."""
    try:
        now = datetime.datetime.now()
        with get_db() as conn:
            kat_id = get_kategori_id(conn, "Subsidi / Bantuan", "pemasukan", "subsidi")
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO transaksi (id, kategori_id, jumlah, tipe, tanggal, deskripsi, created_at) 
                    VALUES (%s, %s, 0, 'pemasukan', %s, %s, NOW())
                """, (str(uuid.uuid4()), kat_id, now.strftime('%Y-%m-%d'), keterangan))
            conn.commit()
        return f"Berhasil mencatat subsidi/bantuan: {keterangan}."
    except Exception as e:
        logger.error(f"Error catat_subsidi: {e}")
        return f"Gagal mencatat subsidi: {e}"

def tambah_jadwal_agenda(tanggal: str, jam_mulai: str, catatan: str) -> str:
    """Menambahkan jadwal agenda tunggal ke kalender. Format tanggal: YYYY-MM-DD, jam_mulai: HH:MM."""
    try:
        # Normalize time format HH:MM
        jam_clean = jam_mulai.strip()
        if len(jam_clean) == 4 and ":" in jam_clean: # e.g. 8:00 -> 08:00
            jam_clean = "0" + jam_clean
        elif len(jam_clean) == 5 and ":" in jam_clean:
            pass
        elif len(jam_clean) > 5:
            jam_clean = jam_clean[:5]
            
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO jadwal_kerja (id, tanggal, jam_mulai, catatan) 
                    VALUES (%s, %s, %s, %s)
                """, (str(uuid.uuid4()), tanggal, jam_clean, catatan))
            conn.commit()
        return f"Berhasil menyimpan agenda: {catatan} pada {tanggal} jam {jam_clean}."
    except Exception as e:
        logger.error(f"Error tambah_jadwal_agenda: {e}")
        return f"Gagal menyimpan agenda: {e}"

def tambah_jadwal_rutin_weekdays(catatan: str, jam_mulai: str, jam_selesai: str) -> str:
    """Menambahkan jadwal rutin berulang untuk hari kerja (Senin sampai Jumat / weekdays) selama 2 minggu ke depan."""
    try:
        jam_m = jam_mulai.strip()[:5]
        jam_s = jam_selesai.strip()[:5]
        now = datetime.date.today()
        count = 0
        with get_db() as conn:
            with conn.cursor() as c:
                for i in range(14):
                    d = now + datetime.timedelta(days=i)
                    if d.weekday() < 5:  # Senin - Jumat
                        c.execute("""
                            INSERT INTO jadwal_kerja (id, tanggal, jam_mulai, jam_selesai, catatan) 
                            VALUES (%s, %s, %s, %s, %s)
                        """, (str(uuid.uuid4()), d.strftime('%Y-%m-%d'), jam_m, jam_s, catatan))
                        count += 1
            conn.commit()
        return f"Berhasil menyimpan jadwal rutin {catatan} ({jam_m} - {jam_s}) untuk {count} hari kerja ke depan."
    except Exception as e:
        logger.error(f"Error tambah_jadwal_rutin_weekdays: {e}")
        return f"Gagal menyimpan jadwal rutin: {e}"

def get_database_summary() -> str:
    """Mengambil ringkasan data nyata dari database agar AI 100% grounded dan tidak berhalusinasi."""
    try:
        import psycopg2.extras
        now = datetime.datetime.now()
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                # 1. Budget bulan ini
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
                budget_rows = c.fetchall()

                # 2. Transaksi terakhir (5 terbaru)
                c.execute("""
                    SELECT tanggal, tipe, jumlah, deskripsi 
                    FROM transaksi 
                    ORDER BY created_at DESC, id DESC LIMIT 5
                """)
                transaksi_rows = c.fetchall()

                # 3. Jadwal kerja / Agenda hari ini & 7 hari ke depan
                c.execute("""
                    SELECT id, tanggal, jam_mulai, jam_selesai, catatan 
                    FROM jadwal_kerja 
                    WHERE tanggal >= %s 
                    ORDER BY tanggal ASC, jam_mulai ASC LIMIT 10
                """, (now.strftime('%Y-%m-%d'),))
                jadwal_rows = c.fetchall()

        HARI_ID = ['Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu', 'Minggu']
        BULAN_ID = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']
        
        id_holidays = holidays.country_holidays('ID', years=[now.year, now.year + 1])
        hari_ini_nama = HARI_ID[now.weekday()]
        tgl_ini_str = f"{hari_ini_nama}, {now.day} {BULAN_ID[now.month]} {now.year}"
        
        status_hari = "Hari Kerja Normal"
        if now.date() in id_holidays:
            status_hari = f"TANGGAL MERAH / HARI LIBUR NASIONAL ({id_holidays[now.date()]})"
        elif now.weekday() >= 5:
            status_hari = "AKHIR PEKAN (Weekend / Hari Libur Rutin)"

        summary = f"Waktu & Kalender: {tgl_ini_str} pukul {now.strftime('%H:%M')} WIB\n"
        summary += f"Status Hari Ini: {status_hari}\n\n"
        summary += "--- BUDGET BULAN INI ---\n"
        total_limit = 0
        total_terpakai = 0
        if budget_rows:
            for r in budget_rows:
                limit = int(r['limit_nominal'])
                terpakai = int(r['terpakai'])
                sisa = limit - terpakai
                total_limit += limit
                total_terpakai += terpakai
                summary += f"- {r['nama']}: Sisa Rp {sisa:,} (Limit Rp {limit:,})\n"
            summary += f"Total Sisa: Rp {(total_limit - total_terpakai):,}\n"
        else:
            summary += "(Belum ada budget bulan ini)\n"

        summary += "\n--- TRANSAKSI TERAKHIR ---\n"
        if transaksi_rows:
            for t in transaksi_rows:
                summary += f"- [{t['tanggal']}] {t['tipe']}: Rp {int(t['jumlah']):,} ({t['deskripsi']})\n"
        else:
            summary += "(Belum ada transaksi)\n"

        summary += "\n--- AGENDA 7 HARI KE DEPAN ---\n"
        if jadwal_rows:
            for j in jadwal_rows:
                tgl_obj = j['tanggal']
                if isinstance(tgl_obj, str):
                    tgl_obj = datetime.datetime.strptime(tgl_obj, '%Y-%m-%d').date()
                
                hari_agenda = HARI_ID[tgl_obj.weekday()]
                libur_tag = ""
                if tgl_obj in id_holidays:
                    libur_tag = f" [TANGGAL MERAH: {id_holidays[tgl_obj]}]"
                elif tgl_obj.weekday() >= 5:
                    libur_tag = " [Weekend]"

                jam_info = str(j['jam_mulai'])[:5]
                if j['jam_selesai']:
                    jam_info += f"-{str(j['jam_selesai'])[:5]}"
                summary += f"- {j['tanggal']} ({hari_agenda}){libur_tag} {jam_info}: {j['catatan']}\n"
        else:
            summary += "(Tidak ada agenda 7 hari ke depan)\n"

        return summary
    except Exception as e:
        logger.error(f"Error get_database_summary: {e}")
        return f"Catatan database sinkron. Waktu: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    save_chat_id(chat_id)
    chat_histories[chat_id] = []

    welcome_text = (
        "Halo Sayang! 🥰 Istrimu siap bantu kelola keuangan dan jadwal harian kita.\n\n"
        "Mas bisa chat santai seperti biasa, misalnya:\n"
        "💬 'Sayang, gajianku masuk 5.000.000 tolong atur ya'\n"
        "💬 'Tadi aku beli makan siang 35.000'\n"
        "💬 'Jadwal magangku Senin-Jumat jam 08:00 - 17:00'\n"
        "💬 'Ada meeting proyek besok jam 14:00'\n"
        "💬 'Sisa uang jajan kita berapa ya?'\n\n"
        "Atau Mas bisa pakai menu tombol cepat di bawah ini ya Sayang! ❤️"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard())

def generate_wife_response(user_text: str, session_id: str = "default") -> str:
    """Core AI brain to generate responses for both Telegram and WhatsApp channels."""
    try:
        db_context = get_database_summary()

        if session_id not in chat_histories:
            chat_histories[session_id] = []

        history_context = ""
        if chat_histories[session_id]:
            history_context = "\n--- RIWAYAT CHAT ---\n"
            for h in chat_histories[session_id][-MAX_HISTORY_TURNS:]:
                history_context += f"{h['role']}: {h['text']}\n"

        prompt_with_context = (
            f"Konteks Data Nyata Database:\n{db_context}\n"
            f"{history_context}\n"
            f"Pesan Suami: {user_text}"
        )

        MODELS_TO_TRY = [
            os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-3.7-flash",
            "gemini-flash-latest"
        ]

        response = None
        last_err = None
        for model_name in MODELS_TO_TRY:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt_with_context,
                    config=types.GenerateContentConfig(
                        tools=[atur_anggaran_gajian, catat_pengeluaran, catat_subsidi, tambah_jadwal_agenda, tambah_jadwal_rutin_weekdays],
                        system_instruction=system_instruction,
                        temperature=0.7,
                    )
                )
                if response:
                    break
            except Exception as e_model:
                last_err = e_model
                logger.warning(f"Model {model_name} rate-limited/failed: {e_model}. Falling back to next model...")
                continue

        if not response and last_err:
            raise last_err
        
        reply = response.text if response.text else "Sudah aku catat dan simpan ya Sayang! 🥰 Ada lagi yang perlu aku bantu?"
        
        # Clean formatting safely without deleting text content
        reply = reply.replace("**", "").replace("*", "").replace("#", "")
        reply = re.sub(r'\b(pout|sigh|chuckle|giggles|blushes)\b', '', reply, flags=re.IGNORECASE)
        reply = reply.strip()

        # Update conversational memory
        chat_histories[session_id].append({"role": "Suami", "text": user_text})
        chat_histories[session_id].append({"role": "Istri", "text": reply})
        if len(chat_histories[session_id]) > MAX_HISTORY_TURNS * 2:
            chat_histories[session_id] = chat_histories[session_id][-MAX_HISTORY_TURNS * 2:]

        return reply
    except Exception as e:
        logger.error(f"AI Generation Error: {e}")
        return "Duh Sayang, sinyal pikiranku agak terganggu sebentar. Tapi catatan keuangan kita tetap aman kok. Coba ulang lagi ya Mas tercinta! 🥰"

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.message.chat_id)
    save_chat_id(update.message.chat_id)

    # Shortcut handling for interactive buttons
    if user_text == "📊 Rekap Keuangan & Agenda":
        await rekap(update, context)
        return
    elif user_text == "💰 Info Sisa Budget":
        await info_budget_quick(update, context)
        return
    elif user_text == "📅 Jadwal Kerja / Agenda":
        await info_jadwal_quick(update, context)
        return
    elif user_text == "💡 Tips Hemat Sayang":
        user_text = "Sayang, kasih tips keuangan atau kata-kata penyemangat buat suamimu dong hari ini!"

    reply = generate_wife_response(user_text, session_id=chat_id)
    await update.message.reply_text(reply, reply_markup=get_main_keyboard())

async def info_budget_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Respon cepat sisa budget dalam gaya santai dan hangat."""
    import psycopg2.extras
    now = datetime.datetime.now()
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
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
                rows = c.fetchall()

        if not rows:
            await update.message.reply_text("Bulan ini kita belum input anggaran gaji Sayang. Mas bisa bilang misalnya 'Gajiku 3 juta tolong atur ya'.", reply_markup=get_main_keyboard())
            return

        res = "💰 Sisa Pos Uang Kita Bulan Ini ya Mas:\n\n"
        total_sisa = 0
        for r in rows:
            limit = int(r['limit_nominal'])
            terpakai = int(r['terpakai'])
            sisa = limit - terpakai
            total_sisa += sisa
            res += f"📌 {r['nama']}:\n   Sisa: Rp {sisa:,} (dari Rp {limit:,})\n"
        
        res += f"\n💵 Total dana aman yang tersisa: Rp {total_sisa:,}\nSemangat terus ya Mas kerjanya, istri doakan selalu! ❤️"
        await update.message.reply_text(res, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error info_budget_quick: {e}")
        await update.message.reply_text("Ada kendala teknis saat ambil data budget Sayang.")

async def info_jadwal_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Respon cepat jadwal kerja / agenda."""
    import psycopg2.extras
    now = datetime.datetime.now()
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                c.execute("""
                    SELECT tanggal, jam_mulai, jam_selesai, catatan 
                    FROM jadwal_kerja 
                    WHERE tanggal >= %s 
                    ORDER BY tanggal ASC, jam_mulai ASC LIMIT 7
                """, (now.strftime('%Y-%m-%d'),))
                rows = c.fetchall()

        if not rows:
            await update.message.reply_text("Belum ada agenda tercatat untuk seminggu ke depan Sayang. Kalau ada jadwal baru, kasih tahu aku ya!", reply_markup=get_main_keyboard())
            return

        res = "📅 Agenda & Jadwal Mas Terdekat:\n\n"
        for r in rows:
            jam_info = str(r['jam_mulai'])[:5]
            if r['jam_selesai']:
                jam_info += f" - {str(r['jam_selesai'])[:5]}"
            res += f"🔹 {r['tanggal']} ({jam_info}): {r['catatan']}\n"
        
        res += "\nNanti aku ingetin 30 menit & 15 menit sebelumnya ya Mas! 🥰"
        await update.message.reply_text(res, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error info_jadwal_quick: {e}")
        await update.message.reply_text("Ada kendala saat membuka buku agenda Sayang.")

async def gajian(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nominal = int(context.args[0])
        atur_anggaran_gajian(nominal)
        kebutuhan = int(nominal * 0.5)
        keinginan = int(nominal * 0.3)
        tabungan = int(nominal * 0.2)

        response = (
            f"Alhamdulillah, uang gaji Rp {nominal:,} sudah aku pos-poskan dengan rapi ya Mas! 🥰\n\n"
            f"📦 Kebutuhan Pokok (50%): Rp {kebutuhan:,}\n"
            f"🎮 Hiburan & Jajan (30%): Rp {keinginan:,}\n"
            f"💰 Tabungan Masa Depan (20%): Rp {tabungan:,} (Simpan rapat-rapat ya Sayang ❤️)"
        )
        await update.message.reply_text(response, reply_markup=get_main_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Formatnya begini ya Sayang: /gajian 5000000", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error gajian command: {e}")
        await update.message.reply_text("Terjadi kesalahan sistem saat menyimpan gaji Mas.")

async def catat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import psycopg2.extras
    try:
        tipe = context.args[0].lower()
        nominal = int(context.args[1])
        keterangan = " ".join(context.args[2:])
        
        if tipe not in ["kebutuhan", "keinginan"]:
            await update.message.reply_text("Pilihannya: 'kebutuhan' atau 'keinginan' ya Mas.\nContoh: /catat keinginan 25000 kopi susu", reply_markup=get_main_keyboard())
            return

        catat_pengeluaran(tipe, nominal, keterangan)
        
        # Cek sisa budget
        now = datetime.datetime.now()
        kat_nama = "Kebutuhan Pokok" if tipe == "kebutuhan" else "Keinginan & Hiburan"
        
        with get_db() as conn:
            kat_id = get_kategori_id(conn, kat_nama, "pengeluaran", tipe)
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                c.execute("""
                    SELECT b.limit_nominal,
                           COALESCE(SUM(t.jumlah), 0) as terpakai
                    FROM budget b 
                    LEFT JOIN transaksi t ON t.kategori_id = b.kategori_id 
                         AND EXTRACT(MONTH FROM t.tanggal) = b.bulan 
                         AND EXTRACT(YEAR FROM t.tanggal) = b.tahun
                         AND t.tipe = 'pengeluaran'
                    WHERE b.kategori_id = %s AND b.bulan = %s AND b.tahun = %s
                    GROUP BY b.limit_nominal
                """, (kat_id, now.month, now.year))
                row = c.fetchone()
        
        response = f"Sudah aku catat ya Mas: Rp {nominal:,} untuk {keterangan} ({kat_nama})."
        if row:
            limit = int(row['limit_nominal'])
            terpakai = int(row['terpakai'])
            sisa = limit - terpakai
            if sisa < 0:
                response += f"\n\n🥺 Sayang, pos {kat_nama} kita sudah defisit minus Rp {abs(sisa):,} lho. Yuk tahan jajan dulu demi tabungan kita ya Mas!"
            elif sisa < (limit * 0.2):
                response += f"\n\n⚠️ Sisa pos {kat_nama} tinggal Rp {sisa:,} nih Mas. Dihemat-hemat ya Sayang!"
            else:
                response += f"\nSisa jatah pos ini masih aman: Rp {sisa:,} 😊"

        await update.message.reply_text(response, reply_markup=get_main_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Formatnya begini Mas: /catat keinginan 20000 makan bakso", reply_markup=get_main_keyboard())

async def subsidi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keterangan = " ".join(context.args)
        if not keterangan:
            raise ValueError
        
        catat_subsidi(keterangan)
        await update.message.reply_text(f"Alhamdulillah, rezeki {keterangan} sudah aku catat! Pengeluaran kita jadi lebih irit Sayang. ❤️", reply_markup=get_main_keyboard())
    except ValueError:
        await update.message.reply_text("Tulis keterangannya Mas: /subsidi beras 5kg dari orang tua", reply_markup=get_main_keyboard())

async def rekap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import psycopg2.extras
    now = datetime.datetime.now()
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
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
                rows = c.fetchall()
        
        if not rows:
            await update.message.reply_text("Bulan ini belum ada pos anggaran yang diset Mas. Ketik /gajian [nominal] atau bilang di chat ya Sayang.", reply_markup=get_main_keyboard())
            return
            
        response = f"📊 Rekap Buku Catatan Keluarga ({now.strftime('%B %Y')}):\n\n"
        total_sisa = 0
        for r in rows:
            limit = int(r['limit_nominal'])
            terpakai = int(r['terpakai'])
            sisa = limit - terpakai
            total_sisa += sisa
            response += f"🔹 Pos {r['nama']}:\n   Jatah: Rp {limit:,} | Terpakai: Rp {terpakai:,}\n   Sisa: Rp {sisa:,}\n\n"
            
        response += f"💰 Total Sisa Uang: Rp {total_sisa:,}\nTetap semangat dan jaga kesehatan ya Sayang! 🥰"
        await update.message.reply_text(response.strip(), reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error rekap: {e}")
        await update.message.reply_text("Maaf Mas, buku catatan sedang tidak bisa dibuka.")

async def agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tanggal = context.args[0]
        jam = context.args[1]
        keterangan = " ".join(context.args[2:])
        
        tambah_jadwal_agenda(tanggal, jam, keterangan)
        await update.message.reply_text(f"📅 Sudah aku simpan di jadwal ya Mas!\nTanggal: {tanggal} pukul {jam}\nAcara: {keterangan}\nTenang, nanti aku ingetin tepat waktu ya Sayang! 🥰", reply_markup=get_main_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Formatnya begini Sayang: /agenda YYYY-MM-DD HH:MM keterangan", reply_markup=get_main_keyboard())

def send_whatsapp_reminder(message: str):
    """Sends proactive reminder to WhatsApp group via the Baileys gateway API."""
    try:
        req_data = json.dumps({"message": message}).encode('utf-8')
        req = urllib.request.Request(
            "http://127.0.0.1:3000/send-message",
            data=req_data,
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            pass
    except Exception as e:
        # Gateway might be offline or no group registered yet, safe to ignore
        pass

# --- SCHEDULER JOB FOR REMINDERS (ROBUST & RELIABLE) ---
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Enhanced scheduler with deduplication tracker to ensure zero missed & zero duplicate alerts."""
    chat_id = get_chat_id()
    now = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    
    try:
        import psycopg2.extras
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                c.execute("SELECT id, tanggal, jam_mulai, catatan FROM jadwal_kerja WHERE tanggal = %s", (today_str,))
                rows = c.fetchall()
        
        for row in rows:
            try:
                jadwal_id = str(row['id'])
                jam_str = str(row['jam_mulai'])[:5]
                agenda_time = datetime.datetime.strptime(f"{row['tanggal']} {jam_str}", "%Y-%m-%d %H:%M")
                diff_seconds = (agenda_time - now).total_seconds()
                minutes_diff = diff_seconds / 60.0
                
                # Check for 30 minutes window (28.0 to 31.0 minutes)
                reminder_key_30 = (jadwal_id, "30m", today_str)
                if 28.0 <= minutes_diff <= 31.0 and reminder_key_30 not in sent_reminders:
                    sent_reminders.add(reminder_key_30)
                    msg_30 = f"🔔 [30 MENIT LAGI Mas!]\nAda acara: {row['catatan']}\nJam: {jam_str}\nSiap-siap dari sekarang ya Sayang, jangan sampai terburu-buru! 🥰"
                    if chat_id:
                        await context.bot.send_message(chat_id=chat_id, text=msg_30)
                    send_whatsapp_reminder(msg_30)
                
                # Check for 15 minutes window (13.0 to 16.0 minutes)
                reminder_key_15 = (jadwal_id, "15m", today_str)
                if 13.0 <= minutes_diff <= 16.0 and reminder_key_15 not in sent_reminders:
                    sent_reminders.add(reminder_key_15)
                    msg_15 = f"🔔 [15 MENIT LAGI!]\nAyo siap-siap Sayang, agenda {row['catatan']} jam {jam_str} sebentar lagi mulai! Semangat ya Mas! ❤️"
                    if chat_id:
                        await context.bot.send_message(chat_id=chat_id, text=msg_15)
                    send_whatsapp_reminder(msg_15)
                    
            except Exception as e:
                logger.error(f"Error processing reminder row {row}: {e}")
                
    except Exception as e:
        logger.error(f"Database error in check_reminders: {e}")

class WhatsAppWebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/wa-chat':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(body)
                user_text = data.get('text', '')
                group_id = data.get('group_id', 'wa_group')
                reply = generate_wife_response(user_text, session_id=group_id)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'reply': reply}).encode('utf-8'))
            except Exception as e:
                logger.error(f"WhatsApp webhook error: {e}")
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass # Quiet HTTP logs

def start_http_server():
    server = ThreadingHTTPServer(('127.0.0.1', 5001), WhatsAppWebhookHandler)
    logger.info("WhatsApp HTTP Webhook server running on http://127.0.0.1:5001")
    server.serve_forever()

def main():
    # Start WhatsApp Webhook background server
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gajian", gajian))
    app.add_handler(CommandHandler("catat", catat))
    app.add_handler(CommandHandler("subsidi", subsidi))
    app.add_handler(CommandHandler("rekap", rekap))
    app.add_handler(CommandHandler("agenda", agenda))

    # Handler for normal text messages (chatting with AI)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))

    # Run scheduler every 30 seconds for higher timing precision
    job_queue = app.job_queue
    job_queue.run_repeating(check_reminders, interval=30, first=5)

    logger.info("Bot is running with full QHSE standards and WhatsApp Webhook active...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
