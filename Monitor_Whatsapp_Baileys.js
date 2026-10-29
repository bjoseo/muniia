/**
 * Monitor_Whatsapp_Baileys.js
 * Ingestión con Debounce Buffer, filtros de seguridad y escritura atómica.
 */

const {
  default: makeWASocket,
  useMultiFileAuthState,
  downloadMediaMessage,
} = require('@whiskeysockets/baileys');
const fs = require('fs');
const path = require('path');
const qrcode = require('qrcode-terminal');
const { Client } = require('pg');
require('dotenv').config({ path: path.resolve(__dirname, '.env') });

const INBOX_DIR = path.resolve(__dirname, 'whatsapp_inbox');
if (!fs.existsSync(INBOX_DIR)) {
  fs.mkdirSync(INBOX_DIR, { recursive: true });
}

// Configuración PostgreSQL (sin fallback hardcodeado)
const dbConfig = {
  host: process.env.DB_HOST || 'localhost',
  port: process.env.DB_PORT || 5432,
  database: process.env.DB_NAME || 'muniia',
  user: process.env.DB_USER || 'postgres',
  password: process.env.DB_PASS,
};

// Buffers para Debounce por usuario
const userBuffers = new Map(); // remoteJid -> Array de textos
const userTimers = new Map(); // remoteJid -> setTimeout ID
const DEBOUNCE_MS = 10000; // Ventana de 10 segundos

async function checkAndSendResponses(sock) {
  const client = new Client(dbConfig);
  try {
    await client.connect();
    const res = await client.query(`
      SELECT id_reclamo, remoto_jid, mensaje_respuesta 
      FROM reclamos 
      WHERE remoto_jid IS NOT NULL 
        AND mensaje_respuesta IS NOT NULL 
        AND respuesta_enviada = FALSE
      LIMIT 5
    `);

    for (const row of res.rows) {
      console.log(`📤 Enviando respuesta a ${row.remoto_jid}...`);
      await sock.sendMessage(row.remoto_jid, { text: row.mensaje_respuesta });
      await client.query(
        'UPDATE reclamos SET respuesta_enviada = TRUE WHERE id_reclamo = $1',
        [row.id_reclamo],
      );
    }
  } catch (err) {
    // Manejo de errores silencioso para reconexión DB
  } finally {
    await client.end();
  }
}

function flushUserBuffer(remoteJid) {
  const messages = userBuffers.get(remoteJid) || [];
  userBuffers.delete(remoteJid);
  userTimers.delete(remoteJid);

  if (messages.length === 0) return;

  const textoConsolidado = messages.join(' ').trim();
  const palabras = textoConsolidado.split(/\s+/).filter(Boolean);

  // Filtro anti-ruido: mínimo 4 palabras
  if (palabras.length < 4) {
    console.log(
      `⚠️ Mensaje descartado por corto (${palabras.length} palabras) de ${remoteJid}: "${textoConsolidado}"`,
    );
    return;
  }

  const timestamp = Date.now();
  const filepath = path.join(INBOX_DIR, `${timestamp}_texto.txt`);
  const metaContent = `JID: ${remoteJid}\n${textoConsolidado}`;

  // Escritura atómica
  const tempPath = `${filepath}.tmp`;
  fs.writeFileSync(tempPath, metaContent, 'utf-8');
  fs.renameSync(tempPath, filepath);

  console.log(
    `📩 Mensaje consolidado guardado: ${timestamp}_texto.txt de ${remoteJid}`,
  );
}

async function startBot() {
  const { state, saveCreds } = await useMultiFileAuthState(
    path.resolve(__dirname, 'auth_info_baileys'),
  );

  const sock = makeWASocket({
    auth: state,
    printQRInTerminal: false,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, qr } = update;
    if (qr) qrcode.generate(qr, { small: true });
    if (connection === 'close') {
      setTimeout(startBot, 10000);
    } else if (connection === 'open') {
      console.log('✅ Conectado a WhatsApp - Servidor activo.');
      setInterval(() => checkAndSendResponses(sock), 5000);
    }
  });

  sock.ev.on('messages.upsert', async ({ messages }) => {
    for (const msg of messages) {
      //    if (msg.key.fromMe || !msg.message) continue;

      const remoteJid = msg.key.remoteJid || '';

      // Filtros de seguridad (sin historias ni grupos)
      if (
        remoteJid.includes('@status') ||
        remoteJid.includes('@broadcast') ||
        remoteJid.endsWith('@g.us')
      ) {
        continue;
      }

      const isAudio = msg.message.audioMessage || msg.message.pttMessage;
      const isImage = msg.message.imageMessage;
      const caption = (
        msg.message.imageMessage?.caption ||
        msg.message.conversation ||
        msg.message.extendedTextMessage?.text ||
        ''
      ).trim();

      const timestamp = Date.now();

      try {
        if (isAudio) {
          const buffer = await downloadMediaMessage(
            msg,
            'buffer',
            {},
            { logger: console, reuploadRequest: sock.updateMediaMessage },
          );
          const finalPath = path.join(INBOX_DIR, `${timestamp}_audio.ogg`);
          const tempPath = `${finalPath}.tmp`;

          fs.writeFileSync(tempPath, buffer);
          fs.renameSync(tempPath, finalPath);

          const metaContent = `JID: ${remoteJid}\n${caption}`;
          fs.writeFileSync(`${finalPath}.txt`, metaContent.trim(), 'utf-8');
          console.log(`📩 Audio listo: ${timestamp}_audio.ogg de ${remoteJid}`);
        } else if (isImage) {
          const buffer = await downloadMediaMessage(
            msg,
            'buffer',
            {},
            { logger: console, reuploadRequest: sock.updateMediaMessage },
          );
          const finalPath = path.join(INBOX_DIR, `${timestamp}_imagen.jpg`);
          const tempPath = `${finalPath}.tmp`;

          fs.writeFileSync(tempPath, buffer);
          fs.renameSync(tempPath, finalPath);

          const metaContent = `JID: ${remoteJid}\n${caption}`;
          fs.writeFileSync(`${finalPath}.txt`, metaContent.trim(), 'utf-8');
          console.log(
            `📩 Imagen lista: ${timestamp}_imagen.jpg de ${remoteJid}`,
          );
        } else if (caption.length > 0) {
          // Debounce logic para mensajes de texto
          if (!userBuffers.has(remoteJid)) {
            userBuffers.set(remoteJid, []);
          }
          userBuffers.get(remoteJid).push(caption);

          if (userTimers.has(remoteJid)) {
            clearTimeout(userTimers.get(remoteJid));
          }

          const timer = setTimeout(
            () => flushUserBuffer(remoteJid),
            DEBOUNCE_MS,
          );
          userTimers.set(remoteJid, timer);
        }
      } catch (err) {
        console.error('❌ Error guardando mensaje:', err);
      }
    }
  });
}

startBot();
