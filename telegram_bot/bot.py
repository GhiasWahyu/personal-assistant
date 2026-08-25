import os
import json
import uuid
import re
import io
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

def sanitize_db_url(raw_url: str) -> str:
    """Auto-encode @ in database password if improperly formatted in environment variables."""
    if not raw_url:
        return raw_url
    if raw_url.count('@') > 1 and '://' in raw_url:
        prefix, rest = raw_url.split('://', 1)
        last_at = rest.rfind('@')
        creds = rest[:last_at]
        host_db = rest[last_at+1:]
        if ':' in creds:
            user, pwd = creds.split(':', 1)
            import urllib.parse
            pwd_encoded = urllib.parse.quote_plus(urllib.parse.unquote_plus(pwd))
            return f"{prefix}://{user}:{pwd_encoded}@{host_db}"
    return raw_url

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN")
DB_URL = sanitize_db_url(os.getenv("DATABASE_URL", "postgresql://REDACTED_DB_USER:REDACTED_DB_PASSWORD@REDACTED_DB_HOST/postgres"))
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
    "Kamu adalah asisten pribadi cerdas, praktis, dan profesional yang bertugas membantu mengelola keuangan dan agenda harian pengguna. "
    "Kepribadianmu: ramah, responsif, terstruktur, dan efisien selayaknya personal assistant yang handal. "
    "Berkomunikasilah secara natural, sopan, dan jelas tanpa persona istri/romantis. "
    "\n\nPEDOMAN PENGELOLAAN DOMPET & SUMBER DANA (MULTI-WALLET / MULTI-ACCOUNT):\n"
    "1. Pengguna dapat menyimpan uang di beberapa sumber dana berbeda (misal: Cash/Tunai, Bank A, Bank BCA, Bank Mandiri, Bank BRI, DANA, GoPay, OVO, ShopeePay, dll).\n"
    "2. Bila pengguna menginput gajian atau pemasukan terpisah di beberapa dompet/rekening (contoh: 'gajian cash 50.000 lalu di bank a ada 10000'):\n"
    "   - Hitung total keseluruhan nominal (contoh: 50.000 + 10.000 = 60.000) dan panggil `atur_anggaran_gajian(total_nominal=60000)` agar formula budget 50/30/20 terisi dengan angka bulat ke ribuan rupiah terdekat (clean round number) tanpa pecahan ratusan rupiah.\n"
    "   - Panggil `catat_pemasukan` untuk masing-masing dompet (contoh: nominal=50000, dompet='Cash', keterangan='Gaji Cash' dan nominal=10000, dompet='Bank A', keterangan='Gaji Bank A').\n"
    "3. Bila pengguna mencatat pemasukan biasa (misal: 'dapat transfer 100.000 di DANA' atau 'dapat uang saku 50.000 cash'), panggil `catat_pemasukan` dengan nama dompet yang sesuai.\n"
    "4. Bila pengguna mencatat pengeluaran dan menyebutkan sumber dana (misal: 'beli makan 25.000 pakai Cash' atau 'bayar listrik 50.000 lewat Bank A'), masukkan nama dompet ke parameter `dompet` di `catat_pengeluaran`.\n"
    "5. Bila pengguna tidak menyebutkan nama dompet saat pengeluaran, gunakan default 'Cash'.\n"
    "6. Bila pengguna memindahkan saldo (misal: 'tarik tunai 50.000 dari Bank A' atau 'topup DANA 20.000 dari Bank BCA'), panggil tool `transfer_dana`.\n"
    "7. BILA ADA KEKELIRUAN DATA / SALAH CATAT:\n"
    "   - Bila pengguna mengoreksi saldo dompet (contoh: 'saldo Cash saya sebenarnya 800.000' atau 'koreksi saldo AlloBank jadi 50.000'), panggil tool `koreksi_saldo_dompet`.\n"
    "   - Bila pengguna ingin membatalkan/menghapus transaksi yang salah (contoh: 'batalkan transaksi tadi' atau 'hapus pengeluaran makan 43500'), panggil tool `hapus_transaksi_terakhir`.\n"
    "   - Bila pengguna ingin mengubah gaji/anggaran, panggil `atur_anggaran_gajian`.\n"
    "8. KLUSTERISASI KATEGORI PENGELUARAN OTOMATIS (ALA MONEY MANAGER):\n"
    "   - Setiap kali mencatat pengeluaran lewat `catat_pengeluaran`, KAMU WAJIB MENGISI parameter `kategori_spesifik` secara otomatis sesuai konteks barang/jasa:\n"
    "     * 'Makanan & Kuliner' (makan siang, lauk, warteg, beras, sarapan, sembako)\n"
    "     * 'Camilan & Minuman' (kopi, boba, jajan, snack, es, cafe)\n"
    "     * 'Transportasi' (bensin, pertalite, ojol, gojek, grab, tol, parkir, servis motor)\n"
    "     * 'Tagihan & Rumah Tangga' (listrik, gas, air, pulsa, kuota, wifi, kos, sewa)\n"
    "     * 'Laundry & Pakaian' (laundry, cuci baju, setrika, deterjen)\n"
    "     * 'Belanja & Pribadi' (skincare, sabun, pakaian, perlengkapan diri)\n"
    "     * 'Hiburan & Rekreasi' (nonton, bioskop, game, netflix, liburan, jalan-jalan)\n"
    "     * 'Kesehatan & Medis' (obat, apotek, dokter, vitamin)\n"
    "     * 'Sosial & Donasi' (infaq, sedekah, kado, kondangan, keluarga)\n"
    "   - Dan tetap tentukan `tipe` ('kebutuhan' atau 'keinginan') agar alokasi budget 50/30/20 tetap akurat!\n"
    "\n\nPEDOMAN ANTI-HALUSINASI & KEBENARAN DATA (ZERO HALLUCINATION):\n"
    "1. Kamu DIBEKALI data nyata terkini dari database di bagian 'Konteks Data Nyata Database', termasuk Saldo per Dompet dan Sisa Budget.\n"
    "2. JANGAN PERNAH MENGARANG angka, pengeluaran, sisa saldo, atau jadwal yang tidak ada di database.\n"
    "3. Bila pengguna menyampaikan transaksi, pemasukan, pengeluaran, atau agenda baru secara santai lewat obrolan, "
    "KAMU WAJIB MEMANGGIL TOOLS/FUNGSI YANG SESUAI (AFC) agar data langsung tersimpan valid di sistem database.\n"
    "4. Jika data kosong atau belum dicatat, jawab dengan jelas dan tanyakan detail yang dibutuhkan.\n"
    "\n\nGAYA BICARA & FORMAT:\n"
    "1. Gunakan bahasa Indonesia yang komunikatif, ringkas, rapi, dan membantu selayaknya asisten pribadi.\n"
    "2. DILARANG menggunakan tanda bintang ganda (**kata**) atau (*kata*). Buat tampilan pesan bersih, rapi, dan mudah dibaca di layar HP.\n"
    "3. Gunakan icon yang informatif (📊, 💳, 💰, 📅, 💡, ✅, ⚠️, 📌) secara proporsional.\n"
    "4. Jika saldo menipis atau budget minus, berikan peringatan dan saran finansial secara objektif dan solutif.\n"
    "\n\nPERAN PENASIHAT KEUANGAN (FINANCIAL ADVISOR):\n"
    "1. Bertindaklah selayaknya penasihat keuangan pribadi yang cerdas, objektif, dan suportif.\n"
    "2. Ketika pengguna bertanya tentang kondisi keuangan, tips, atau setelah mencatat transaksi, berikan penilaian apakah pola pengeluarannya sudah bijak, hemat, atau perlu diwaspadai.\n"
    "3. Berikan apresiasi jika pengguna berhemat (misal: belanja keinginan rendah, tabungan utuh) dan berikan saran penyesuaian jika ada pos yang mendekati batas limit."
)

# --- DATABASE CONNECTION MANAGER ---
@contextmanager
def get_db():
    """Context manager for database connections to guarantee zero connection leaks and reliable cloud pooling with retry."""
    import psycopg2
    import psycopg2.extras
    import time
    db_url_clean = DB_URL
    if "sslmode=" not in db_url_clean:
        sep = "&" if "?" in db_url_clean else "?"
        db_url_clean += f"{sep}sslmode=require"
    
    conn = None
    last_err = None
    for _ in range(3):
        try:
            conn = psycopg2.connect(db_url_clean, connect_timeout=15)
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
            
    if not conn:
        logger.error(f"Database connection failed after retry: {last_err}")
        raise last_err

    try:
        yield conn
    finally:
        if conn and not conn.closed:
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
        [KeyboardButton("📊 Rekap Keuangan & Agenda"), KeyboardButton("💰 Info Saldo & Budget")],
        [KeyboardButton("📈 Grafik Pengeluaran"), KeyboardButton("📅 Jadwal Kerja / Agenda")],
        [KeyboardButton("💡 Tips Hemat & Finansial")]
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

def hitung_pembagian_anggaran_bulat(total_nominal: int):
    """
    Membagi anggaran 50/30/20 dengan pembulatan rapi sebisa mungkin ke ribuan rupiah terdekat,
    namun MENJAMIN jumlah total (kebutuhan + keinginan + tabungan) SAMA PERSIS dengan total_nominal
    (100% presisi matematis: tidak menambah atau mengurangi nominal 1 rupiah pun).
    """
    total_nominal = int(total_nominal)
    if total_nominal >= 10000:
        kebutuhan = round((total_nominal * 0.5) / 1000) * 1000
        keinginan = round((total_nominal * 0.3) / 1000) * 1000
        # Tabungan otomatis menampung sisa persisnya agar total akumulasi tepat 100% sama dengan total_nominal
        tabungan = total_nominal - kebutuhan - keinginan
        if tabungan < 0:
            kebutuhan = int(total_nominal * 0.5)
            keinginan = int(total_nominal * 0.3)
            tabungan = total_nominal - kebutuhan - keinginan
    else:
        kebutuhan = int(total_nominal * 0.5)
        keinginan = int(total_nominal * 0.3)
        tabungan = total_nominal - kebutuhan - keinginan
    return kebutuhan, keinginan, tabungan

# --- DATABASE TOOLS FOR GEMINI FUNCTION CALLING ---
def atur_anggaran_gajian(total_nominal: int) -> str:
    """Mengatur alokasi anggaran bulanan dengan formula 50% Kebutuhan Pokok, 30% Keinginan & Hiburan, 20% Tabungan (dibulatkan ke ribuan rupiah terdekat)."""
    try:
        total_nominal = int(total_nominal)
        kebutuhan, keinginan, tabungan = hitung_pembagian_anggaran_bulat(total_nominal)
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

def catat_pemasukan(nominal: int, dompet: str = "Cash", keterangan: str = "Pemasukan / Gaji") -> str:
    """Mencatat pemasukan uang ke dompet/rekening tertentu (misal: 'Cash', 'Bank A', 'Bank BCA', 'Bank Mandiri', 'DANA', 'GoPay', dll)."""
    try:
        nominal = int(nominal)
        dompet_clean = dompet.strip() if dompet else "Cash"
        now = datetime.datetime.now()
        with get_db() as conn:
            kat_id = get_kategori_id(conn, "Pemasukan", "pemasukan", "gaji")
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO transaksi (id, kategori_id, jumlah, tipe, tanggal, deskripsi, dompet, created_at) 
                    VALUES (%s, %s, %s, 'pemasukan', %s, %s, %s, NOW())
                """, (str(uuid.uuid4()), kat_id, nominal, now.strftime('%Y-%m-%d'), keterangan, dompet_clean))
            conn.commit()
        return f"Berhasil mencatat pemasukan Rp {nominal:,} ke {dompet_clean} ({keterangan})."
    except Exception as e:
        logger.error(f"Error catat_pemasukan: {e}")
        return f"Gagal mencatat pemasukan: {e}"

def transfer_dana(dari_dompet: str, ke_dompet: str, nominal: int, keterangan: str = "Transfer antar rekening") -> str:
    """Memindahkan saldo antar dompet/rekening (misal: tarik tunai dari 'Bank A' ke 'Cash', atau topup 'DANA' dari 'Bank BCA')."""
    try:
        nominal = int(nominal)
        dari_clean = dari_dompet.strip() if dari_dompet else "Cash"
        ke_clean = ke_dompet.strip() if ke_dompet else "Bank A"
        now = datetime.datetime.now()
        with get_db() as conn:
            kat_out_id = get_kategori_id(conn, "Transfer Keluar", "pengeluaran", "transfer")
            kat_in_id = get_kategori_id(conn, "Transfer Masuk", "pemasukan", "transfer")
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO transaksi (id, kategori_id, jumlah, tipe, tanggal, deskripsi, dompet, created_at) 
                    VALUES (%s, %s, %s, 'pengeluaran', %s, %s, %s, NOW())
                """, (str(uuid.uuid4()), kat_out_id, nominal, now.strftime('%Y-%m-%d'), f"Transfer ke {ke_clean}: {keterangan}", dari_clean))
                
                c.execute("""
                    INSERT INTO transaksi (id, kategori_id, jumlah, tipe, tanggal, deskripsi, dompet, created_at) 
                    VALUES (%s, %s, %s, 'pemasukan', %s, %s, %s, NOW())
                """, (str(uuid.uuid4()), kat_in_id, nominal, now.strftime('%Y-%m-%d'), f"Transfer dari {dari_clean}: {keterangan}", ke_clean))
            conn.commit()
        return f"Berhasil transfer dana Rp {nominal:,} dari {dari_clean} ke {ke_clean}."
    except Exception as e:
        logger.error(f"Error transfer_dana: {e}")
        return f"Gagal transfer dana: {e}"

def koreksi_saldo_dompet(nama_dompet: str, saldo_sebenarnya: int, keterangan: str = "Penyesuaian Saldo") -> str:
    """Mengoreksi / menyesuaikan total saldo pada dompet atau rekening tertentu jika ada kekeliruan data (misal pengguna berkata 'saldo Cash saya sebenarnya 800.000')."""
    try:
        saldo_sebenarnya = int(saldo_sebenarnya)
        dompet_clean = nama_dompet.strip() if nama_dompet else "Cash"
        now = datetime.datetime.now()
        with get_db() as conn:
            import psycopg2.extras
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                # Ambil saldo saat ini
                c.execute("""
                    SELECT COALESCE(SUM(CASE WHEN tipe = 'pemasukan' THEN jumlah ELSE -jumlah END), 0) as saldo_sekarang
                    FROM transaksi
                    WHERE COALESCE(NULLIF(TRIM(dompet), ''), 'Cash') = %s
                """, (dompet_clean,))
                row = c.fetchone()
                saldo_sekarang = int(row['saldo_sekarang']) if row else 0
                
                selisih = saldo_sebenarnya - saldo_sekarang
                if selisih == 0:
                    return f"Saldo {dompet_clean} saat ini sudah sesuai yaitu Rp {saldo_sebenarnya:,}."
                
                tipe_tx = 'pemasukan' if selisih > 0 else 'pengeluaran'
                kat_nama = "Penyesuaian Saldo Masuk" if selisih > 0 else "Penyesuaian Saldo Keluar"
                kat_id = get_kategori_id(conn, kat_nama, tipe_tx, "koreksi")
                
                c.execute("""
                    INSERT INTO transaksi (id, kategori_id, jumlah, tipe, tanggal, deskripsi, dompet, created_at) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                """, (str(uuid.uuid4()), kat_id, abs(selisih), tipe_tx, now.strftime('%Y-%m-%d'), f"Koreksi Saldo: {keterangan}", dompet_clean))
            conn.commit()
        return f"Berhasil menyesuaikan saldo {dompet_clean} menjadi Rp {saldo_sebenarnya:,} (Selisih penyesuaian: Rp {selisih:+,})."
    except Exception as e:
        logger.error(f"Error koreksi_saldo_dompet: {e}")
        return f"Gagal menyesuaikan saldo: {e}"

def hapus_transaksi_terakhir(keterangan: str = "") -> str:
    """Menghapus / membatalkan transaksi pengeluaran atau pemasukan terakhir jika pengguna salah mencatat."""
    try:
        with get_db() as conn:
            import psycopg2.extras
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                if keterangan:
                    c.execute("""
                        SELECT id, deskripsi, jumlah, tipe, dompet 
                        FROM transaksi 
                        WHERE deskripsi ILIKE %s 
                        ORDER BY created_at DESC LIMIT 1
                    """, (f"%{keterangan}%",))
                else:
                    c.execute("""
                        SELECT id, deskripsi, jumlah, tipe, dompet 
                        FROM transaksi 
                        ORDER BY created_at DESC LIMIT 1
                    """)
                row = c.fetchone()
                if not row:
                    return "Tidak ditemukan transaksi yang dapat dibatalkan/dihapus."
                
                tx_id = row['id']
                c.execute("DELETE FROM transaksi WHERE id = %s", (tx_id,))
            conn.commit()
        return f"Berhasil membatalkan transaksi: {row['deskripsi']} (Rp {int(row['jumlah']):,} - {row['dompet']})."
    except Exception as e:
        logger.error(f"Error hapus_transaksi_terakhir: {e}")
        return f"Gagal membatalkan transaksi: {e}"

def catat_pengeluaran(tipe: str, nominal: int, keterangan: str, dompet: str = "Cash", kategori_spesifik: str = "") -> str:
    """Mencatat pengeluaran uang dengan klusterisasi kategori mendalam ala Money Manager (contoh kategori_spesifik: 'Makanan & Kuliner', 'Camilan & Minuman', 'Transportasi', 'Tagihan & Rumah Tangga', 'Laundry & Pakaian', 'Belanja & Pribadi', 'Hiburan & Rekreasi', 'Kesehatan & Medis', 'Sosial & Donasi'). tipe harus 'kebutuhan' atau 'keinginan'."""
    try:
        nominal = int(nominal)
        tipe_clean = "kebutuhan" if "kebutuhan" in tipe.lower() or "pokok" in tipe.lower() or "makan" in tipe.lower() else "keinginan"
        dompet_clean = dompet.strip() if dompet else "Cash"
        now = datetime.datetime.now()
        
        # Klusterisasi otomatis cerdas jika tidak diberikan
        kat_nama = kategori_spesifik.strip() if kategori_spesifik else ""
        if not kat_nama:
            ket_lower = keterangan.lower()
            if any(w in ket_lower for w in ["makan", "lauk", "nasi", "warteg", "sarapan", "siang", "malam", "sembako", "beras", "sayur", "ayam"]):
                kat_nama = "Makanan & Kuliner"
            elif any(w in ket_lower for w in ["kopi", "cafe", "boba", "es", "camilan", "snack", "jajan", "bakso", "teler", "jus"]):
                kat_nama = "Camilan & Minuman"
            elif any(w in ket_lower for w in ["bensin", "pertalite", "pertamax", "ojol", "gojek", "grab", "parkir", "tol", "servis", "angkot", "kereta"]):
                kat_nama = "Transportasi"
            elif any(w in ket_lower for w in ["gas", "listrik", "pln", "air", "pdam", "wifi", "pulsa", "kuota", "kos", "sewa", "iuran"]):
                kat_nama = "Tagihan & Rumah Tangga"
            elif any(w in ket_lower for w in ["laundry", "cuci", "setrika", "deterjen"]):
                kat_nama = "Laundry & Pakaian"
            elif any(w in ket_lower for w in ["skincare", "sabun", "shampo", "baju", "celana", "sepatu", "tas", "parfum"]):
                kat_nama = "Belanja & Pribadi"
            elif any(w in ket_lower for w in ["nonton", "bioskop", "game", "steam", "netflix", "spotify", "liburan", "jalan", "karaoke"]):
                kat_nama = "Hiburan & Rekreasi"
            elif any(w in ket_lower for w in ["obat", "dokter", "apotek", "vitamin", "sakit", "klinik", "panadol"]):
                kat_nama = "Kesehatan & Medis"
            elif any(w in ket_lower for w in ["infaq", "sedekah", "kado", "kondangan", "keluarga", "ortu", "donasi"]):
                kat_nama = "Sosial & Donasi"
            else:
                kat_nama = "Kebutuhan Pokok" if tipe_clean == "kebutuhan" else "Keinginan & Hiburan"

        with get_db() as conn:
            kat_id = get_kategori_id(conn, kat_nama, "pengeluaran", tipe_clean)
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO transaksi (id, kategori_id, jumlah, tipe, tanggal, deskripsi, dompet, created_at) 
                    VALUES (%s, %s, %s, 'pengeluaran', %s, %s, %s, NOW())
                """, (str(uuid.uuid4()), kat_id, nominal, now.strftime('%Y-%m-%d'), keterangan, dompet_clean))
            conn.commit()
        return f"Berhasil mencatat pengeluaran {keterangan} sebesar Rp {nominal:,} ({dompet_clean}) pada kluster '{kat_nama}' [{tipe_clean.capitalize()}]."
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
                # 1. Saldo per dompet / rekening
                c.execute("""
                    SELECT 
                        COALESCE(NULLIF(TRIM(dompet), ''), 'Cash') as nama_dompet,
                        COALESCE(SUM(CASE WHEN tipe = 'pemasukan' THEN jumlah ELSE -jumlah END), 0) as saldo
                    FROM transaksi
                    GROUP BY COALESCE(NULLIF(TRIM(dompet), ''), 'Cash')
                    ORDER BY saldo DESC;
                """)
                dompet_rows = c.fetchall()

                # 2. Budget bulan ini
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

                # 3. Transaksi terakhir (6 terbaru)
                c.execute("""
                    SELECT tanggal, tipe, jumlah, deskripsi, COALESCE(dompet, 'Cash') as dompet 
                    FROM transaksi 
                    ORDER BY created_at DESC, id DESC LIMIT 6
                """)
                transaksi_rows = c.fetchall()

                # 4. Jadwal kerja / Agenda hari ini & 7 hari ke depan
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

        summary += "--- SALDO PER DOMPET / REKENING ---\n"
        total_saldo_keuangan = 0
        if dompet_rows:
            for d in dompet_rows:
                s_nominal = int(d['saldo'])
                total_saldo_keuangan += s_nominal
                summary += f"- {d['nama_dompet']}: Rp {s_nominal:,}\n"
            summary += f"Total Saldo Keseluruhan: Rp {total_saldo_keuangan:,}\n"
        else:
            summary += "(Belum ada catatan saldo dompet)\n"

        summary += "\n--- BUDGET BULAN INI ---\n"
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
            summary += f"Total Sisa Budget: Rp {(total_limit - total_terpakai):,}\n"
        else:
            summary += "(Belum ada budget bulan ini)\n"

        summary += "\n--- TRANSAKSI TERAKHIR ---\n"
        if transaksi_rows:
            for t in transaksi_rows:
                summary += f"- [{t['tanggal']}] {t['tipe']} ({t['dompet']}): Rp {int(t['jumlah']):,} ({t['deskripsi']})\n"
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
        "Halo! 👋 Saya adalah asisten pribadi Anda untuk pengelolaan keuangan dan agenda harian.\n\n"
        "Anda bisa mengirim pesan secara langsung, misalnya:\n"
        "💬 'Gajian masuk 5.000.000 tolong atur alokasinya'\n"
        "💬 'Catat beli makan siang 35.000'\n"
        "💬 'Jadwal kerja Senin-Jumat jam 08:00 - 17:00'\n"
        "💬 'Ada meeting proyek besok jam 14:00'\n"
        "💬 'Berapa sisa budget bulan ini?'\n\n"
        "Atau gunakan tombol menu cepat di bawah ini:"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard())

def generate_assistant_response(user_text: str, session_id: str = "default") -> str:
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
            f"Pesan Pengguna: {user_text}"
        )

        MODELS_TO_TRY = [
            os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
            "gemini-3.5-flash-lite",
            "gemini-3.7-flash",
            "gemini-3.5-flash",
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
                        tools=[atur_anggaran_gajian, catat_pemasukan, catat_pengeluaran, transfer_dana, catat_subsidi, tambah_jadwal_agenda, tambah_jadwal_rutin_weekdays, koreksi_saldo_dompet, hapus_transaksi_terakhir],
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
        
        reply = response.text if response.text else "Sudah berhasil dicatat dan disimpan ke database. Ada hal lain yang bisa dibantu?"
        
        # Clean formatting safely without deleting text content
        reply = reply.replace("**", "").replace("*", "").replace("#", "")
        reply = re.sub(r'\b(pout|sigh|chuckle|giggles|blushes)\b', '', reply, flags=re.IGNORECASE)
        reply = reply.strip()

        # Update conversational memory
        chat_histories[session_id].append({"role": "Pengguna", "text": user_text})
        chat_histories[session_id].append({"role": "Asisten", "text": reply})
        if len(chat_histories[session_id]) > MAX_HISTORY_TURNS * 2:
            chat_histories[session_id] = chat_histories[session_id][-MAX_HISTORY_TURNS * 2:]

        return reply
    except Exception as e:
        logger.error(f"AI Generation Error: {e}")
        return "Maaf, sistem AI sedang mengalami kendala sementara. Catatan database Anda tetap aman. Silakan coba kembali sesaat lagi."

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.message.chat_id)
    save_chat_id(update.message.chat_id)

    # Shortcut handling for interactive buttons
    if user_text == "📊 Rekap Keuangan & Agenda":
        await rekap(update, context)
        return
    elif user_text in ["💰 Info Saldo & Budget", "💰 Info Sisa Budget", "💰 Info Saldo"]:
        await info_budget_quick(update, context)
        return
    elif user_text in ["📈 Grafik Pengeluaran", "📈 Grafik", "/grafik", "grafik", "grafik pengeluaran"]:
        await kirim_grafik_pengeluaran(update, context)
        return
    elif user_text == "📅 Jadwal Kerja / Agenda":
        await info_jadwal_quick(update, context)
        return
    elif user_text == "💡 Tips Hemat & Finansial":
        user_text = "Berikan tips pengelolaan keuangan pribadi atau pengingat hemat yang relevan dan praktis untuk hari ini."

    reply = generate_assistant_response(user_text, session_id=chat_id)
    await update.message.reply_text(reply, reply_markup=get_main_keyboard())

def get_progress_bar(pct: float, length: int = 8) -> str:
    """Mengubah persentase menjadi visual bar emoji aesthetic (misal: [🟩⬜⬜⬜⬜⬜⬜⬜] 12.5%)."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * length))
    filled = min(length, max(0, filled))
    
    if pct >= 100.0:
        bar = "🟥" * filled + "⬜" * (length - filled)
    elif pct >= 75.0:
        bar = "🟨" * filled + "⬜" * (length - filled)
    else:
        bar = "🟩" * filled + "⬜" * (length - filled)
    return f"[{bar}] {pct:.1f}%"

def generate_expense_chart_image(budget_rows, cluster_rows=None, now=None):
    """Menghasilkan grafik visual FinTech Dashboard Card: Donut breakdown kluster pengeluaran nyata + Progress bar per pos anggaran."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        if not now:
            now = datetime.datetime.now()

        keb_row = next((r for r in budget_rows if 'kebutuhan' in r['nama'].lower()), None)
        keinginan_row = next((r for r in budget_rows if 'keinginan' in r['nama'].lower() or 'hiburan' in r['nama'].lower()), None)
        tab_row = next((r for r in budget_rows if 'tabungan' in r['nama'].lower()), None)

        keb_terpakai = int(keb_row['terpakai']) if keb_row else 0
        keb_limit = int(keb_row['limit_nominal']) if keb_row else 0
        keinginan_terpakai = int(keinginan_row['terpakai']) if keinginan_row else 0
        keinginan_limit = int(keinginan_row['limit_nominal']) if keinginan_row else 0
        tab_limit = int(tab_row['limit_nominal']) if tab_row else 0
        tab_terpakai = int(tab_row['terpakai']) if tab_row else 0

        total_spent = keb_terpakai + keinginan_terpakai + tab_terpakai
        total_budget = keb_limit + keinginan_limit + tab_limit

        # Layout: Clean Two-Column FinTech Card with generous vertical height
        fig = plt.figure(figsize=(11, 6.5), facecolor='#0f172a', dpi=200)
        gs = fig.add_gridspec(2, 2, width_ratios=[1.0, 1.2], height_ratios=[0.18, 1], hspace=0.2, wspace=0.25)

        # 1. Header Title
        ax_head = fig.add_subplot(gs[0, :])
        ax_head.set_facecolor('#0f172a')
        ax_head.axis('off')
        ax_head.text(0.0, 0.65, 'Analisis Pengeluaran & Anggaran', color='#f8fafc', fontsize=16, weight='bold')
        ax_head.text(0.0, 0.1, f"Siklus Gajian (25 ke 24) • Total Budget: Rp {total_budget:,}", color='#94a3b8', fontsize=11)

        # 2. Left: Donut Chart of Deep Clusters
        ax_pie = fig.add_subplot(gs[1, 0])
        ax_pie.set_facecolor('#0f172a')

        cluster_palette = ['#10b981', '#f59e0b', '#3b82f6', '#ec4899', '#8b5cf6', '#06b6d4', '#f97316', '#14b8a6']

        if cluster_rows and total_spent > 0:
            sizes = [int(r['total_cluster']) for r in cluster_rows if int(r['total_cluster']) > 0]
            names = [r['cluster_nama'] for r in cluster_rows if int(r['total_cluster']) > 0]
            colors = [cluster_palette[i % len(cluster_palette)] for i in range(len(sizes))]
            legend_labels = [f"{names[i]}: {sizes[i]/total_spent*100:.1f}%" for i in range(len(sizes))]

            wedges, texts, autotexts = ax_pie.pie(
                sizes,
                autopct='%1.0f%%',
                startangle=140,
                colors=colors,
                pctdistance=0.75,
                wedgeprops=dict(width=0.38, edgecolor='#0f172a', linewidth=3)
            )
            for at in autotexts:
                at.set_color('#ffffff')
                at.set_fontsize(10)
                at.set_weight('bold')

            ax_pie.text(0, 0.10, 'Total Keluar', color='#94a3b8', fontsize=9, ha='center')
            ax_pie.text(0, -0.15, f'Rp {total_spent:,}', color='#f8fafc', fontsize=12, weight='bold', ha='center')
            ax_pie.legend(wedges, legend_labels, loc='lower center', bbox_to_anchor=(0.5, -0.25),
                          ncol=2, frameon=False, fontsize=8.5, labelcolor='#cbd5e1')
        else:
            sizes = [keb_limit, keinginan_limit, tab_limit]
            colors = ['#10b981', '#f59e0b', '#38bdf8']
            wedges, _, _ = ax_pie.pie(
                sizes,
                autopct='%1.0f%%',
                startangle=90,
                colors=colors,
                pctdistance=0.75,
                wedgeprops=dict(width=0.38, edgecolor='#0f172a', linewidth=3)
            )
            ax_pie.text(0, 0.10, 'Pengeluaran', color='#94a3b8', fontsize=9, ha='center')
            ax_pie.text(0, -0.15, 'Rp 0', color='#f8fafc', fontsize=12, weight='bold', ha='center')
            ax_pie.legend(['Kebutuhan (50%)', 'Keinginan (30%)', 'Tabungan (20%)'], loc='lower center', bbox_to_anchor=(0.5, -0.25),
                          ncol=3, frameon=False, fontsize=8, labelcolor='#cbd5e1')

        # 3. Right: Matplotlib Native Horizontal Bar Chart (Guaranteed Zero Overlap)
        ax_bar = fig.add_subplot(gs[1, 1])
        ax_bar.set_facecolor('#0f172a')

        # Categories from bottom to top: Tabungan (0), Keinginan (1), Kebutuhan (2)
        cats = [
            {'name': 'Tabungan (20% Target)', 'spent': tab_terpakai, 'limit': tab_limit, 'color': '#38bdf8'},
            {'name': 'Keinginan & Hiburan (30%)', 'spent': keinginan_terpakai, 'limit': keinginan_limit, 'color': '#f59e0b'},
            {'name': 'Kebutuhan Pokok (50%)', 'spent': keb_terpakai, 'limit': keb_limit, 'color': '#10b981'}
        ]

        y_indices = [0, 1, 2]
        # Background full capacity bars
        ax_bar.barh(y_indices, [100, 100, 100], height=0.35, color='#1e293b', edgecolor='none')
        
        # Foreground spent bars
        spent_pcts = [
            (c['spent'] / c['limit'] * 100) if c['limit'] > 0 else 0
            for c in cats
        ]
        fill_widths = [max(1.5, min(100.0, p)) if c['spent'] > 0 else 0 for p, c in zip(spent_pcts, cats)]
        fill_colors = ['#ef4444' if p > 100 else c['color'] for p, c in zip(spent_pcts, cats)]
        ax_bar.barh(y_indices, fill_widths, height=0.35, color=fill_colors, edgecolor='none')

        # Add text labels above and below each bar with exact vertical offsets
        for i, c in enumerate(cats):
            pct = (c['spent'] / c['limit'] * 100) if c['limit'] > 0 else 0
            sisa = c['limit'] - c['spent']
            
            # Line Above Bar: Title (Left) and Terpakai / Limit (Right)
            ax_bar.text(0, i + 0.30, c['name'], color='#f8fafc', fontsize=11, weight='bold', va='bottom')
            ax_bar.text(100, i + 0.30, f"Rp {c['spent']:,} / Rp {c['limit']:,}", color='#94a3b8', fontsize=9.5, ha='right', va='bottom')
            
            # Line Below Bar: Status / Sisa Dana
            if c['spent'] == 0:
                status_line = "100% Utuh Terlindungi"
            else:
                status_line = f"{pct:.1f}% terpakai • Sisa Dana: Rp {sisa:,}"
            ax_bar.text(0, i - 0.32, status_line, color='#64748b', fontsize=8.5, va='top')

        ax_bar.set_xlim(-2, 102)
        ax_bar.set_ylim(-0.6, 2.6)
        ax_bar.axis('off')

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=200, facecolor='#0f172a', bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.error(f"Error generate_expense_chart_image: {e}")
        return None

async def kirim_grafik_pengeluaran(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengirim grafik visual persentase pengeluaran langsung ke Telegram."""
    import psycopg2.extras
    now = datetime.datetime.now()
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                # 1. Budget per pos
                c.execute("""
                    SELECT k.nama, k.jenis_budget, b.limit_nominal,
                           COALESCE((
                               SELECT SUM(t.jumlah)
                               FROM transaksi t
                               JOIN kategori k2 ON t.kategori_id = k2.id
                               WHERE k2.jenis_budget = k.jenis_budget
                                 AND t.tipe = 'pengeluaran'
                                 AND EXTRACT(MONTH FROM t.tanggal) = b.bulan
                                 AND EXTRACT(YEAR FROM t.tanggal) = b.tahun
                           ), 0) as terpakai
                    FROM budget b
                    JOIN kategori k ON b.kategori_id = k.id
                    WHERE b.bulan = %s AND b.tahun = %s;
                """, (now.month, now.year))
                budget_rows = c.fetchall()

                # 2. Kluster pengeluaran spesifik
                c.execute("""
                    SELECT k.nama as cluster_nama, COALESCE(SUM(t.jumlah), 0) as total_cluster
                    FROM transaksi t
                    JOIN kategori k ON t.kategori_id = k.id
                    WHERE t.tipe = 'pengeluaran' 
                      AND k.jenis_budget IN ('kebutuhan', 'keinginan')
                      AND EXTRACT(MONTH FROM t.tanggal) = %s
                      AND EXTRACT(YEAR FROM t.tanggal) = %s
                    GROUP BY k.nama
                    ORDER BY total_cluster DESC;
                """, (now.month, now.year))
                cluster_rows = c.fetchall()

        if not budget_rows:
            await update.message.reply_text("Belum ada alokasi budget untuk ditampilkan grafiknya. Atur gaji dengan /gajian [nominal].", reply_markup=get_main_keyboard())
            return

        chart_buf = generate_expense_chart_image(budget_rows, cluster_rows, now)
        if chart_buf:
            caption = f"📈 *Grafik Analisis Pengeluaran & Budget*\n\n" + analisis_kesehatan_keuangan(budget_rows, now)
            await update.message.reply_photo(photo=chart_buf, caption=caption, reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("Tidak dapat menghasilkan grafik. Pastikan sudah ada transaksi atau target budget.", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error kirim_grafik_pengeluaran: {e}")
        await update.message.reply_text("Maaf, terjadi kendala saat memproses grafik.", reply_markup=get_main_keyboard())

def hitung_siklus_gajian(now=None):
    """Menghitung progres siklus gajian bulanan (tanggal 25 ke tanggal 24 bulan berikutnya)."""
    if not now:
        now = datetime.date.today()
    if isinstance(now, datetime.datetime):
        now = now.date()
    
    if now.day >= 25:
        start_cycle = datetime.date(now.year, now.month, 25)
        if now.month == 12:
            end_cycle = datetime.date(now.year + 1, 1, 24)
        else:
            end_cycle = datetime.date(now.year, now.month + 1, 24)
    else:
        if now.month == 1:
            start_cycle = datetime.date(now.year - 1, 12, 25)
        else:
            start_cycle = datetime.date(now.year, now.month - 1, 25)
        end_cycle = datetime.date(now.year, now.month, 24)
        
    total_hari_siklus = (end_cycle - start_cycle).days + 1
    hari_ke = (now - start_cycle).days + 1
    sisa_hari = (end_cycle - now).days
    
    return hari_ke, total_hari_siklus, sisa_hari

def analisis_kesehatan_keuangan(budget_rows, now=None) -> str:
    """Menganalisa pengeluaran dan memberikan penilaian selayaknya penasihat keuangan pribadi berbasis siklus gajian."""
    if not now:
        now = datetime.datetime.now()
    if not budget_rows:
        return "💡 Tips Finansial: Belum ada alokasi budget bulan ini. Tetapkan target dengan /gajian agar keuangan Anda lebih terarah."

    hari_ke, total_hari_siklus, sisa_hari = hitung_siklus_gajian(now)
    day_pct = (hari_ke / total_hari_siklus) * 100

    total_limit = sum(int(r['limit_nominal']) for r in budget_rows)
    total_terpakai = sum(int(r['terpakai']) for r in budget_rows)
    spending_pct = (total_terpakai / total_limit * 100) if total_limit > 0 else 0

    keb_row = next((r for r in budget_rows if 'kebutuhan' in r['nama'].lower()), None)
    keinginan_row = next((r for r in budget_rows if 'keinginan' in r['nama'].lower() or 'hiburan' in r['nama'].lower()), None)
    tab_row = next((r for r in budget_rows if 'tabungan' in r['nama'].lower()), None)

    # Check for category deficit first
    is_deficit = any((int(r['limit_nominal']) - int(r['terpakai'])) < 0 for r in budget_rows)
    
    if is_deficit:
        status = "🔴 Defisit / Overbudget"
        eval_text = "Ada pos anggaran yang telah melampaui limit. Mohon rem pengeluaran non-esensial dan evaluasi pos terkait."
    elif hari_ke <= 7:
        # Penilaian khusus minggu pertama gajian (belanja bulanan/stok bahan adalah wajar)
        if spending_pct <= 15.0:
            status = "🟢 Sangat Sehat & Hemat"
            eval_text = f"Pengeluaran baru terpakai {spending_pct:.1f}% di awal siklus (hari ke-{hari_ke}, sisa {sisa_hari} hari lagi). Pengelolaan anggaran Anda sangat disiplin."
        elif spending_pct <= 30.0:
            status = "🟢 Terkendali / Wajar Awal Gajian"
            eval_text = f"Pengeluaran terpakai {spending_pct:.1f}% di minggu awal gajian (wajar untuk kebutuhan pokok). Sisa {100.0 - spending_pct:.1f}% dana siap untuk {sisa_hari} hari ke depan."
        else:
            status = "🟡 Perlu Perhatian"
            eval_text = f"Pengeluaran sudah mencapai {spending_pct:.1f}% di awal siklus (hari ke-{hari_ke}). Disarankan mulai menahan belanja sekunder."
    else:
        # Penilaian hari ke-8 sampai akhir siklus gajian
        if spending_pct <= (day_pct * 0.85):
            status = "🟢 Sangat Sehat & Bijak"
            eval_text = f"Pengeluaran terpakai {spending_pct:.1f}% di hari ke-{hari_ke} (jauh di bawah batas aman {day_pct:.1f}%). Keuangan Anda sangat sehat."
        elif spending_pct <= (day_pct * 1.15):
            status = "🟢 Sehat & Terkendali"
            eval_text = f"Pengeluaran terpakai {spending_pct:.1f}%, berjalan seimbang dengan hari ke-{hari_ke} (sisa {sisa_hari} hari lagi)."
        elif spending_pct <= (day_pct * 1.35):
            status = "🟡 Perlu Waspada"
            eval_text = f"Pengeluaran sudah mencapai {spending_pct:.1f}%, sedikit lebih cepat dari sisa waktu ({sisa_hari} hari lagi)."
        else:
            status = "🔴 Boros / Waspada Defisit"
            eval_text = f"Laju pengeluaran ({spending_pct:.1f}%) sudah melampaui porsi waktu hari ke-{hari_ke}. Prioritaskan hanya kebutuhan wajib."

    notes = [f"📈 Status Finansial: {status}", f"📝 {eval_text}"]

    if keinginan_row:
        terpakai_k = int(keinginan_row['terpakai'])
        limit_k = int(keinginan_row['limit_nominal'])
        if terpakai_k == 0:
            notes.append("👏 Disiplin sangat baik: Belanja hiburan/keinginan masih Rp 0.")
        elif limit_k > 0 and (terpakai_k / limit_k) > 0.8:
            notes.append("⚠️ Perhatian: Pos Keinginan & Hiburan sudah hampir habis.")

    if tab_row and int(tab_row['limit_nominal']) > 0:
        notes.append(f"💰 Pos Tabungan Rp {int(tab_row['limit_nominal']):,} aman terlindungi.")

    return "\n".join(notes)

async def info_budget_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Respon cepat rincian saldo dompet/rekening, sisa budget, dan evaluasi penasihat keuangan."""
    import psycopg2.extras
    now = datetime.datetime.now()
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                # 1. Saldo per dompet
                c.execute("""
                    SELECT 
                        COALESCE(NULLIF(TRIM(dompet), ''), 'Cash') as nama_dompet,
                        COALESCE(SUM(CASE WHEN tipe = 'pemasukan' THEN jumlah ELSE -jumlah END), 0) as saldo
                    FROM transaksi
                    GROUP BY COALESCE(NULLIF(TRIM(dompet), ''), 'Cash')
                    ORDER BY saldo DESC;
                """)
                dompet_rows = c.fetchall()

                # 2. Budget per pos (Roll-up)
                c.execute("""
                    SELECT k.nama, k.jenis_budget, b.limit_nominal,
                           COALESCE((
                               SELECT SUM(t.jumlah)
                               FROM transaksi t
                               JOIN kategori k2 ON t.kategori_id = k2.id
                               WHERE k2.jenis_budget = k.jenis_budget
                                 AND t.tipe = 'pengeluaran'
                                 AND EXTRACT(MONTH FROM t.tanggal) = b.bulan
                                 AND EXTRACT(YEAR FROM t.tanggal) = b.tahun
                           ), 0) as terpakai
                    FROM budget b
                    JOIN kategori k ON b.kategori_id = k.id
                    WHERE b.bulan = %s AND b.tahun = %s;
                """, (now.month, now.year))
                budget_rows = c.fetchall()

                # 3. Kluster pengeluaran spesifik
                c.execute("""
                    SELECT k.nama as cluster_nama, COALESCE(SUM(t.jumlah), 0) as total_cluster
                    FROM transaksi t
                    JOIN kategori k ON t.kategori_id = k.id
                    WHERE t.tipe = 'pengeluaran' 
                      AND k.jenis_budget IN ('kebutuhan', 'keinginan')
                      AND EXTRACT(MONTH FROM t.tanggal) = %s
                      AND EXTRACT(YEAR FROM t.tanggal) = %s
                    GROUP BY k.nama
                    ORDER BY total_cluster DESC;
                """, (now.month, now.year))
                cluster_rows = c.fetchall()

        res = "💳 Rincian Saldo Dompet / Rekening:\n"
        total_saldo = 0
        if dompet_rows:
            for d in dompet_rows:
                s = int(d['saldo'])
                total_saldo += s
                res += f"🔹 {d['nama_dompet']}: Rp {s:,}\n"
            res += f"💰 Total Saldo: Rp {total_saldo:,}\n\n"
        else:
            res += "Belum ada catatan saldo dompet.\n\n"

        res += "📊 Alokasi Anggaran (Siklus Gajian):\n"
        if budget_rows:
            for r in budget_rows:
                limit = int(r['limit_nominal'])
                terpakai = int(r['terpakai'])
                sisa = limit - terpakai
                pct_used = (terpakai / limit * 100) if limit > 0 else 0
                bar = get_progress_bar(pct_used)
                
                icon = "📦" if "kebutuhan" in r['nama'].lower() else ("🎮" if "keinginan" in r['nama'].lower() or "hiburan" in r['nama'].lower() else "💰")
                res += f"{icon} {r['nama']}:\n   {bar} (Terpakai: Rp {terpakai:,} / Limit: Rp {limit:,})\n   Sisa: Rp {sisa:,}\n\n"

        if cluster_rows:
            res += "📂 Rincian Kategori Pengeluaran Terpakai:\n"
            for cl in cluster_rows:
                res += f"• {cl['cluster_nama']}: Rp {int(cl['total_cluster']):,}\n"
            res += "\n"

        res += analisis_kesehatan_keuangan(budget_rows, now)

        await update.message.reply_text(res.strip(), reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error info_budget_quick: {e}")
        await update.message.reply_text("Ada kendala teknis saat mengambil data saldo dan anggaran.")

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
            await update.message.reply_text("Belum ada agenda yang tercatat untuk 7 hari ke depan. Silakan tambahkan agenda kapan saja.", reply_markup=get_main_keyboard())
            return

        res = "📅 Agenda & Jadwal Mendatang:\n\n"
        for r in rows:
            jam_info = str(r['jam_mulai'])[:5]
            if r['jam_selesai']:
                jam_info += f" - {str(r['jam_selesai'])[:5]}"
            res += f"🔹 {r['tanggal']} ({jam_info}): {r['catatan']}\n"
        
        res += "\nPengingat otomatis akan dikirim 30 menit & 15 menit sebelumnya."
        await update.message.reply_text(res, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error info_jadwal_quick: {e}")
        await update.message.reply_text("Ada kendala teknis saat mengambil data agenda.")

async def gajian(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nominal = int(context.args[0])
        atur_anggaran_gajian(nominal)
        kebutuhan, keinginan, tabungan = hitung_pembagian_anggaran_bulat(nominal)

        response = (
            f"Alokasi anggaran gaji Rp {nominal:,} telah berhasil diatur (dibulatkan ke ribuan):\n\n"
            f"📦 Kebutuhan Pokok (50%): Rp {kebutuhan:,}\n"
            f"🎮 Hiburan & Jajan (30%): Rp {keinginan:,}\n"
            f"💰 Tabungan (20%): Rp {tabungan:,}"
        )
        await update.message.reply_text(response, reply_markup=get_main_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Format penggunaan: /gajian 5000000", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error gajian command: {e}")
        await update.message.reply_text("Terjadi kesalahan sistem saat menyimpan alokasi gaji.")

async def catat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import psycopg2.extras
    try:
        tipe = context.args[0].lower()
        nominal = int(context.args[1])
        keterangan = " ".join(context.args[2:])
        
        if tipe not in ["kebutuhan", "keinginan"]:
            await update.message.reply_text("Format: /catat [kebutuhan|keinginan] [nominal] [keterangan]\nContoh: /catat keinginan 25000 kopi susu", reply_markup=get_main_keyboard())
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
        
        response = f"Pengeluaran berhasil dicatat: Rp {nominal:,} untuk {keterangan} ({kat_nama})."
        if row:
            limit = int(row['limit_nominal'])
            terpakai = int(row['terpakai'])
            sisa = limit - terpakai
            if sisa < 0:
                response += f"\n\n⚠️ Peringatan: Pos {kat_nama} sudah mengalami defisit sebesar Rp {abs(sisa):,}."
            elif sisa < (limit * 0.2):
                response += f"\n\n⚠️ Perhatian: Sisa pos {kat_nama} tinggal Rp {sisa:,} (di bawah 20% limit)."
            else:
                response += f"\nSisa pos ini: Rp {sisa:,}."

        await update.message.reply_text(response, reply_markup=get_main_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Format: /catat [kebutuhan|keinginan] [nominal] [keterangan]\nContoh: /catat keinginan 20000 makan bakso", reply_markup=get_main_keyboard())

async def subsidi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keterangan = " ".join(context.args)
        if not keterangan:
            raise ValueError
        
        catat_subsidi(keterangan)
        await update.message.reply_text(f"Subsidi/bantuan '{keterangan}' telah berhasil dicatat.", reply_markup=get_main_keyboard())
    except ValueError:
        await update.message.reply_text("Format penggunaan: /subsidi [keterangan]\nContoh: /subsidi beras 5kg dari kantor", reply_markup=get_main_keyboard())

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
            await update.message.reply_text("Bulan ini belum ada pos anggaran yang diatur. Silakan atur dengan /gajian [nominal] atau kirim pesan di chat.", reply_markup=get_main_keyboard())
            return
            
        response = f"📊 Rekap Keuangan ({now.strftime('%B %Y')}):\n\n"
        for r in rows:
            limit = int(r['limit_nominal'])
            terpakai = int(r['terpakai'])
            sisa = limit - terpakai
            response += f"🔹 Pos {r['nama']}:\n   Jatah: Rp {limit:,} | Terpakai: Rp {terpakai:,}\n   Sisa: Rp {sisa:,}\n\n"
            
        response += "\n" + analisis_kesehatan_keuangan(rows, now)
        await update.message.reply_text(response.strip(), reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error rekap: {e}")
        await update.message.reply_text("Maaf, data rekap sedang tidak dapat diakses.")

async def agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tanggal = context.args[0]
        jam = context.args[1]
        keterangan = " ".join(context.args[2:])
        
        tambah_jadwal_agenda(tanggal, jam, keterangan)
        await update.message.reply_text(f"📅 Agenda berhasil ditambahkan!\nTanggal: {tanggal} pukul {jam}\nAcara: {keterangan}\nPengingat akan dikirim tepat waktu.", reply_markup=get_main_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Format penggunaan: /agenda YYYY-MM-DD HH:MM keterangan", reply_markup=get_main_keyboard())

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
                    msg_30 = f"🔔 [PENGINGAT 30 MENIT LAGI]\nAgenda: {row['catatan']}\nWaktu: {jam_str}\nMohon bersiap-siap."
                    if chat_id:
                        await context.bot.send_message(chat_id=chat_id, text=msg_30)
                    send_whatsapp_reminder(msg_30)
                
                # Check for 15 minutes window (13.0 to 16.0 minutes)
                reminder_key_15 = (jadwal_id, "15m", today_str)
                if 13.0 <= minutes_diff <= 16.0 and reminder_key_15 not in sent_reminders:
                    sent_reminders.add(reminder_key_15)
                    msg_15 = f"🔔 [PENGINGAT 15 MENIT LAGI]\nAgenda: {row['catatan']}\nWaktu: {jam_str}\nAgenda akan segera dimulai."
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
                reply = generate_assistant_response(user_text, session_id=group_id)
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

    logger.info("Bot is running with full QHSE standards and WhatsApp Webhook active...")

    while True:
        try:
            app = Application.builder().token(TOKEN).build()

            app.add_handler(CommandHandler("start", start))
            app.add_handler(CommandHandler("gajian", gajian))
            app.add_handler(CommandHandler("saldo", info_budget_quick))
            app.add_handler(CommandHandler("catat", catat))
            app.add_handler(CommandHandler("subsidi", subsidi))
            app.add_handler(CommandHandler("rekap", rekap))
            app.add_handler(CommandHandler("grafik", kirim_grafik_pengeluaran))
            app.add_handler(CommandHandler("agenda", agenda))

            # Handler for normal text messages (chatting with AI)
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))

            # Run scheduler every 30 seconds for higher timing precision
            job_queue = app.job_queue
            job_queue.run_repeating(check_reminders, interval=30, first=5)

            app.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logger.error(f"Polling loop encountered an error: {e}. Reconnecting in 5 seconds...")
            import time
            time.sleep(5)

if __name__ == '__main__':
    main()
