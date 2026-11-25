import crypto from 'node:crypto';
import http from 'node:http';
import { URL } from 'node:url';
import { ipv4Get } from './ipv4.mjs';
import {
    getContacts,
    getStatus,
    keepAliveActiveBots,
    mockConnect,
    mockDisconnect,
    restorePersistedBots,
    sendDirectMessage,
    setMockContacts,
    startPairing,
    stopBot,
    updateConfig,
} from './bot_runtime.mjs';

const HOST = process.env.WA_WORKER_HOST || '127.0.0.1';
const PORT = Number(process.env.WA_WORKER_PORT || process.env.PORT || 5056);
const WORKER_TOKEN = String(process.env.WA_BOT_WORKER_TOKEN || process.env.MEDIA_WORKER_TOKEN || '').trim();

function sendJson(res, status, payload) {
    const body = JSON.stringify(payload || {});
    res.writeHead(status, {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': Buffer.byteLength(body),
        'Cache-Control': 'no-store',
    });
    res.end(body);
}

function isAuthorized(req) {
    if (!WORKER_TOKEN) return true;
    const raw = req.headers['x-worker-token'];
    const provided = Array.isArray(raw) ? String(raw[0] || '') : String(raw || '');
    if (!provided) return false;
    const a = Buffer.from(provided.trim(), 'utf-8');
    const b = Buffer.from(WORKER_TOKEN, 'utf-8');
    if (a.length !== b.length) return false;
    return crypto.timingSafeEqual(a, b);
}

function readJsonBody(req, maxBytes = 1024 * 1024) {
    return new Promise((resolve, reject) => {
        const chunks = [];
        let total = 0;
        req.on('data', (c) => {
            total += c.length;
            if (total > maxBytes) {
                reject(new Error('Body too large'));
                req.destroy();
                return;
            }
            chunks.push(c);
        });
        req.on('end', () => {
            const raw = Buffer.concat(chunks).toString('utf-8').trim();
            if (!raw) {
                resolve({});
                return;
            }
            try {
                const parsed = JSON.parse(raw);
                resolve(parsed && typeof parsed === 'object' ? parsed : {});
            } catch (_) {
                reject(new Error('Invalid JSON'));
            }
        });
        req.on('error', reject);
    });
}

const server = http.createServer(async (req, res) => {
    try {
        const url = new URL(req.url || '/', `http://${req.headers.host || '127.0.0.1'}`);
        const pathname = url.pathname.replace(/\/+$/, '') || '/';

        if (req.method === 'GET' && pathname === '/health') {
            sendJson(res, 200, { status: 'ok', ipv4: true, ready: true });
            return;
        }
        if ((req.method === 'GET' || req.method === 'HEAD') && pathname === '/ping') {
            keepAliveActiveBots();
            if (req.method === 'HEAD') {
                res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
                res.end();
                return;
            }
            sendJson(res, 200, { ok: true, ts: Math.floor(Date.now() / 1000) });
            return;
        }

        if (!isAuthorized(req)) {
            sendJson(res, 401, { success: false, error: 'Yetkisiz istek.' });
            return;
        }

        if (req.method === 'GET' && pathname === '/wa/status') {
            const uid = url.searchParams.get('user_id') || '';
            sendJson(res, 200, getStatus(uid));
            return;
        }
        if (req.method === 'GET' && pathname === '/wa/contacts') {
            const uid = url.searchParams.get('user_id') || '';
            sendJson(res, 200, { success: true, contacts: getContacts(uid) });
            return;
        }

        if (req.method === 'POST') {
            const body = await readJsonBody(req);
            if (pathname === '/wa/pair') {
                const result = await startPairing(body.user_id, body.phone, body.config || {});
                sendJson(res, result.success ? 200 : 400, result);
                return;
            }
            if (pathname === '/wa/config') {
                const result = updateConfig(body.user_id, body.config || {});
                sendJson(res, result.success ? 200 : 400, result);
                return;
            }
            if (pathname === '/wa/stop') {
                const result = await stopBot(body.user_id, Boolean(body.logout));
                sendJson(res, 200, result);
                return;
            }
            if (pathname === '/wa/send') {
                const result = await sendDirectMessage(body.user_id, body.target, body.message);
                sendJson(res, result.success ? 200 : 400, result);
                return;
            }
            if (pathname === '/wa/wake') {
                keepAliveActiveBots();
                sendJson(res, 200, { ok: true, awake: true, sleep_wake: true, ts: Math.floor(Date.now() / 1000) });
                return;
            }
            if (pathname === '/wa/mock-connect' && process.env.WA_MOCK_MODE === '1') {
                const result = mockConnect(body.user_id);
                sendJson(res, result.success ? 200 : 400, result);
                return;
            }
            if (pathname === '/wa/mock-disconnect' && process.env.WA_MOCK_MODE === '1') {
                const result = mockDisconnect(body.user_id);
                sendJson(res, result.success ? 200 : 400, result);
                return;
            }
            if (pathname === '/wa/mock-contacts' && process.env.WA_MOCK_MODE === '1') {
                const result = setMockContacts(body.user_id, body.contacts);
                sendJson(res, result.success ? 200 : 400, result);
                return;
            }
        }

        sendJson(res, 404, { success: false, error: 'Bilinmeyen yol.' });
    } catch (_) {
        sendJson(res, 400, { success: false, error: 'Aslan AI WhatsApp servisi isteği işleyemedi.' });
    }
});

server.listen(PORT, HOST, () => {
    process.stdout.write(`Aslan WA Service listening on ${HOST}:${PORT} (IPv4)\n`);
    restorePersistedBots().catch(() => {});
});

const KEEPALIVE_INTERVAL_MS = Math.max(15000, Number(process.env.KEEPALIVE_INTERVAL_MS || 60000));
const KEEPALIVE_TARGET_URL = process.env.KEEPALIVE_DISABLED === '1'
    ? ''
    : String(
        process.env.KEEPALIVE_TARGET_URL ||
        process.env.VERCEL_KEEPALIVE_URL ||
        (process.env.WA_MOCK_MODE === '1' ? '' : 'https://aslansite.vercel.app/api/ping')
    ).trim();

const keepAliveTimer = setInterval(() => {
    try { keepAliveActiveBots(); } catch (_) {}
    if (KEEPALIVE_TARGET_URL) {
        ipv4Get(KEEPALIVE_TARGET_URL, { 'User-Agent': 'Aslan-AI-KeepAlive/1.0' }, 8000).catch(() => {});
    }
}, KEEPALIVE_INTERVAL_MS);
keepAliveTimer.unref();
