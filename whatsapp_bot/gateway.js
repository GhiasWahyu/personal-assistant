const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, Browsers } = require('@whiskeysockets/baileys');
const pino = require('pino');
const QRCode = require('qrcode');
const axios = require('axios');
const express = require('express');
const fs = require('fs');
const path = require('path');

const CONFIG_FILE = path.join(__dirname, 'config_wa.json');
const PYTHON_API_URL = process.env.PYTHON_API_URL || 'http://127.0.0.1:5001/wa-chat';
const PORT = process.env.PORT || 3000;

// Load config
let config = { target_group_id: null };
if (fs.existsSync(CONFIG_FILE)) {
    try {
        config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
    } catch (e) {
        console.error('Error loading config_wa.json:', e);
    }
}

function saveConfig() {
    fs.writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2));
}

let sock = null;
let qrCodeString = null;
let isConnected = false;
let pairingCodeString = null;
let state = null;
let saveCreds = null;

// Express server for outbound notifications and Web UI
const app = express();
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.get('/status', (req, res) => {
    res.json({
        connected: isConnected,
        target_group_id: config.target_group_id,
        qr: isConnected ? null : qrCodeString,
        pairingCode: pairingCodeString
    });
});

app.get('/', async (req, res) => {
    if (isConnected) {
        return res.send(`
            <!DOCTYPE html>
            <html>
            <head><meta charset="utf-8"><title>WhatsApp Gateway - Aktif</title></head>
            <body style="font-family:Segoe UI, sans-serif; text-align:center; padding:50px; background:#f0f2f5;">
                <div style="background:white; display:inline-block; padding:40px; border-radius:12px; box-shadow:0 4px 15px rgba(0,0,0,0.1); max-width:500px;">
                    <div style="font-size:48px; margin-bottom:10px;">✅</div>
                    <h2 style="color:#25D366; margin:0 0 15px;">WhatsApp Berhasil Terhubung!</h2>
                    <p style="color:#333; font-size:16px;">Status: <strong>Aktif & Siap Digunakan</strong></p>
                    <hr style="border:none; border-top:1px solid #eee; margin:20px 0;">
                    <p style="color:#555; text-align:left; line-height:1.6;">
                        <strong>Langkah Selanjutnya:</strong><br>
                        1. Buka WhatsApp di HP Anda.<br>
                        2. Buat grup baru (misal: <em>Asisten Istri & Keuangan</em>).<br>
                        3. Di dalam grup tersebut, ketik <code>.setbotgroup</code> untuk mengaktifkannya.
                    </p>
                </div>
            </body>
            </html>
        `);
    }

    let qrImageHtml = '<p style="color:#888;">Menyiapkan QR Code...</p>';
    if (qrCodeString) {
        try {
            const qrImage = await QRCode.toDataURL(qrCodeString, { width: 280, margin: 1 });
            qrImageHtml = `<img src="${qrImage}" alt="WhatsApp QR Code" style="border: 2px solid #25D366; border-radius:8px; margin: 10px 0;">`;
        } catch (e) {
            qrImageHtml = `<p style="color:red;">Error: ${e.message}</p>`;
        }
    }

    res.send(`
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta http-equiv="refresh" content="15">
            <title>Hubungkan WhatsApp</title>
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background:#f0f2f5; margin:0; padding:30px; text-align:center; }
                .container { background:white; display:inline-block; padding:30px 40px; border-radius:12px; box-shadow:0 4px 20px rgba(0,0,0,0.08); max-width:550px; text-align:left; }
                h2 { color:#075E54; margin-top:0; text-align:center; }
                .card { background:#f9fbfb; border:1px solid #e1e9e9; border-radius:8px; padding:20px; margin-bottom:20px; }
                .btn { background:#25D366; color:white; border:none; padding:12px 20px; border-radius:6px; font-weight:bold; cursor:pointer; font-size:15px; }
                .btn:hover { background:#1eb956; }
                input[type="text"] { padding:10px; width:70%; border:1px solid #ccc; border-radius:6px; font-size:15px; margin-right:8px; }
                .code-box { font-size:28px; font-weight:bold; letter-spacing:4px; color:#075E54; background:#e8f5e9; padding:12px; border-radius:6px; text-align:center; margin:15px 0; border:1px dashed #25D366; }
            </style>
        </head>
        <body>
            <div class="container">
                <h2>📱 Hubungkan Asisten ke WhatsApp</h2>
                
                <div class="card">
                    <h3 style="margin-top:0; color:#128C7E;">Opsi 1: Tautkan dengan Nomor HP (100% Anti-Gagal 🌟)</h3>
                    <p style="font-size:14px; color:#555;">Masukkan nomor WhatsApp Anda (diawali 62):</p>
                    <form method="POST" action="/request-code" style="display:flex;">
                        <input type="text" name="phone" placeholder="Contoh: 6281234567890" required>
                        <button type="submit" class="btn">Dapatkan Kode</button>
                    </form>
                    ${pairingCodeString ? `
                        <div style="margin-top:15px;">
                            <p style="font-size:14px; color:#333; margin-bottom:5px;">Masukkan kode 8 karakter ini di notifikasi WhatsApp HP Anda:</p>
                            <div class="code-box">${pairingCodeString}</div>
                        </div>
                    ` : ''}
                </div>

                <div class="card" style="text-align:center;">
                    <h3 style="margin-top:0; color:#128C7E;">Opsi 2: Scan QR Code</h3>
                    ${qrImageHtml}
                    <p style="font-size:13px; color:#777; margin:5px 0 0;">Buka WhatsApp > Perangkat Tertaut > Tautkan Perangkat</p>
                </div>
            </div>
        </body>
        </html>
    `);
});

app.post('/request-code', async (req, res) => {
    let phone = req.body.phone || '';
    phone = phone.replace(/[^0-9]/g, '');
    if (phone.startsWith('0')) {
        phone = '62' + phone.substring(1);
    }

    if (!sock || isConnected) {
        return res.redirect('/');
    }

    try {
        console.log(`[WhatsApp] Requesting pairing code for: ${phone}`);
        const code = await sock.requestPairingCode(phone);
        pairingCodeString = code;
        console.log(`[WhatsApp] PAIRING CODE: ${code}`);
    } catch (e) {
        console.error('[WhatsApp] Error requesting pairing code:', e);
    }
    res.redirect('/');
});

app.get('/qr', (req, res) => {
    res.redirect('/');
});

app.post('/send-message', async (req, res) => {
    const { jid, message } = req.body;
    const targetJid = jid || config.target_group_id;

    if (!sock || !isConnected) {
        return res.status(503).json({ success: false, error: 'WhatsApp is not connected yet.' });
    }
    if (!targetJid) {
        return res.status(400).json({ success: false, error: 'No target JID or registered group specified.' });
    }

    try {
        await sock.sendMessage(targetJid, { text: message });
        res.json({ success: true });
    } catch (error) {
        console.error('Error sending message:', error);
        res.status(500).json({ success: false, error: error.message });
    }
});

app.listen(PORT, () => {
    console.log(`[WhatsApp Gateway Web UI] Listening on http://localhost:${PORT}`);
});

// Connect to WhatsApp Multi-Device with standard Browser ID
async function startWhatsApp() {
    const authPath = path.join(__dirname, 'auth_session');
    const authState = await useMultiFileAuthState(authPath);
    state = authState.state;
    saveCreds = authState.saveCreds;

    const { version } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
        version,
        logger: pino({ level: 'silent' }),
        printQRInTerminal: false,
        auth: state,
        browser: Browsers.windows('Desktop'),
        syncFullHistory: false,
        defaultQueryTimeoutMs: 60000,
        connectTimeoutMs: 60000
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            qrCodeString = qr;
        }

        if (connection === 'close') {
            isConnected = false;
            pairingCodeString = null;
            const statusCode = (lastDisconnect?.error)?.output?.statusCode;
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
            console.log(`[WhatsApp] Connection closed (code ${statusCode}). Reconnecting: ${shouldReconnect}...`);
            if (shouldReconnect) {
                setTimeout(startWhatsApp, 3000);
            } else {
                console.log('[WhatsApp] Logged out. Resetting auth_session...');
                if (fs.existsSync(authPath)) {
                    fs.rmSync(authPath, { recursive: true, force: true });
                }
                setTimeout(startWhatsApp, 3000);
            }
        } else if (connection === 'open') {
            isConnected = true;
            qrCodeString = null;
            pairingCodeString = null;
            console.log('✅ [WhatsApp Gateway] Berhasil terhubung ke WhatsApp!');
        }
    });

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return;

        for (const msg of messages) {
            if (msg.key.remoteJid === 'status@broadcast') continue;

            const isGroup = msg.key.remoteJid.endsWith('@g.us');
            const remoteJid = msg.key.remoteJid;

            let text = msg.message?.conversation ||
                       msg.message?.extendedTextMessage?.text ||
                       msg.message?.imageMessage?.caption ||
                       '';
            text = text.trim();

            if (!text) continue;

            // Group registration command
            if (isGroup && (text.toLowerCase() === '.setbotgroup' || text.toLowerCase() === '.daftargrup' || text.toLowerCase() === '.daftarkangrup')) {
                config.target_group_id = remoteJid;
                saveConfig();
                await sock.sendMessage(remoteJid, {
                    text: '✅ *Grup ini telah berhasil didaftarkan sebagai Ruang Asisten Pribadi & Keuangan!*\n\nSekarang Anda bisa langsung chat santai seperti mencatat pengeluaran, tanya anggaran, atau atur jadwal di grup ini.'
                }, { quoted: msg });
                continue;
            }

            // Respond only inside the registered group
            if (isGroup && config.target_group_id === remoteJid) {
                try {
                    await sock.sendPresenceUpdate('composing', remoteJid);

                    const res = await axios.post(PYTHON_API_URL, {
                        text,
                        sender: msg.key.participant || msg.key.remoteJid,
                        group_id: remoteJid,
                        is_from_me: msg.key.fromMe
                    }, { timeout: 45000 });

                    const replyText = res.data?.reply;
                    if (replyText) {
                        await sock.sendMessage(remoteJid, { text: replyText });
                    }
                } catch (err) {
                    console.error('Error forwarding message to Python backend:', err.message);
                }
            }
        }
    });
}

startWhatsApp();
