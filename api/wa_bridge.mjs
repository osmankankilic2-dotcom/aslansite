import crypto from 'node:crypto';
import dns from 'node:dns';
import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { URL } from 'node:url';
import makeWASocketImport, {
    Browsers,
    DisconnectReason,
    fetchLatestBaileysVersion,
    initAuthCreds,
    BufferJSON,
    proto,
} from '@whiskeysockets/baileys';
import { waitUntil } from '@vercel/functions';
import { Boom } from '@hapi/boom';
import pino from 'pino';

try {
    dns.setDefaultResultOrder('ipv4first');
} catch (_) {}

const makeWASocket = typeof makeWASocketImport === 'function'
    ? makeWASocketImport
    : (makeWASocketImport && makeWASocketImport.default);

const BLOB_API_VERSION = '12';
const BLOB_MAGIC = Buffer.from('ASLANWA1', 'ascii');
const FALLBACK_WA_VERSION = [2, 3000, 1043857760];
const CONTINUOUS_ACTIVE_MODE = process.env.WA_CONTINUOUS_ACTIVE !== '0';
const DEFAULT_PAIR_WINDOW_MS = Math.max(15000, Number(process.env.WA_PAIR_WINDOW_MS || 180000));
const DEFAULT_WAKE_MAX_MS = Math.max(20000, Number(process.env.WA_WAKE_MAX_MS || 240000));
const DEFAULT_IDLE_SLEEP_MS = Math.max(1500, Number(process.env.WA_IDLE_SLEEP_MS || 8000));
const DEFAULT_POST_REPLY_GRACE_MS = Math.max(1500, Number(process.env.WA_POST_REPLY_GRACE_MS || 15000));
const ACTIVE_SLEEP_MS = Math.max(15000, Number(process.env.WA_ACTIVE_SLEEP_MS || 30000));
const IDLE_SLEEP_INTERVAL_MS = Math.max(20000, Number(process.env.WA_IDLE_SLEEP_INTERVAL_MS || 60000));
const DEEP_SLEEP_INTERVAL_MS = Math.max(30000, Number(process.env.WA_DEEP_SLEEP_INTERVAL_MS || 60000));
const MIN_WAKE_GAP_MS = Math.max(5000, Number(process.env.WA_MIN_WAKE_GAP_MS || 20000));
const SOCKET_HEARTBEAT_MS = Math.max(10000, Number(process.env.WA_HEARTBEAT_MS || 25000));
const LEASE_STALE_MS = Math.max(30000, Number(process.env.WA_LEASE_STALE_MS || 55000));
const RELAY_HANDOVER_GAP_MS = Math.max(100, Number(process.env.WA_RELAY_GAP_MS || 350));
const MAX_RECENT_MSG_IDS = 80;
const TMP_SESSION_ROOT = process.env.WA_TMP_DIR || path.join(os.tmpdir(), 'aslan_wa_sessions');

const instances = new Map();
let cachedWaVersion = { version: FALLBACK_WA_VERSION, fetchedAt: 0 };

function ipv4Lookup(hostname, options, callback) {
    let opts = options;
    let cb = callback;
    if (typeof options === 'function') {
        cb = options;
        opts = {};
    }
    const cleanOpts = Object.assign({}, opts || {}, { family: 4 });
    return dns.lookup(hostname, cleanOpts, cb);
}

const ipv4Agent = new https.Agent({
    family: 4,
    keepAlive: true,
    lookup: ipv4Lookup,
});

const ipv4HttpAgent = new http.Agent({
    family: 4,
    keepAlive: true,
    lookup: ipv4Lookup,
});

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function safeWaitUntil(promise) {
    const p = Promise.resolve(promise).catch(() => {});
    try {
        waitUntil(p);
    } catch (_) {}
    return p;
}

function isSocketOpen(sock) {
    const ws = sock && sock.ws;
    return Boolean(ws && (ws.isOpen || (ws.socket && ws.socket.readyState === 1) || ws.readyState === 1));
}

function pingSocket(sock) {
    const ws = sock && sock.ws;
    if (!ws) return false;
    try {
        if (ws.socket && typeof ws.socket.ping === 'function') {
            ws.socket.ping();
            return true;
        }
        if (typeof ws.ping === 'function') {
            ws.ping();
            return true;
        }
    } catch (_) {}
    return false;
}

function resolveBridgeSeed() {
    return (
        process.env.QUOTA_SIGNING_SECRET ||
        process.env.BLOB_READ_WRITE_TOKEN ||
        process.env.GEMINI_API_KEY ||
        'aslan-local-wa-secret'
    ).trim();
}

function resolveBridgeToken() {
    const explicit = (
        process.env.WA_BOT_WORKER_TOKEN ||
        process.env.MEDIA_WORKER_TOKEN ||
        ''
    ).trim();
    if (explicit) return explicit;
    return crypto.createHmac('sha256', resolveBridgeSeed()).update('aslan-wa-bridge-v1').digest('hex');
}

function resolveSelfWakeUrl() {
    const parseHost = (raw) => {
        const s = String(raw || '').trim();
        if (!s) return '';
        if (s.includes('://')) {
            try {
                return new URL(s).hostname.toLowerCase().replace(/\.$/, '');
            } catch (_) {
                return '';
            }
        }
        return s.split('/')[0].split(':')[0].toLowerCase().replace(/\.$/, '');
    };
    const prod = parseHost(process.env.VERCEL_PROJECT_PRODUCTION_URL);
    if (prod) return `https://${prod}/api/wa_bridge/wa/wake`;
    const env = String(process.env.VERCEL_ENV || '').trim().toLowerCase();
    const preview = parseHost(process.env.VERCEL_URL);
    if (env !== 'production' && preview && preview.endsWith('.vercel.app')) {
        return `https://${preview}/api/wa_bridge/wa/wake`;
    }
    return 'https://aslansite.vercel.app/api/wa_bridge/wa/wake';
}

function resolveFallbackKeepAliveUrl() {
    return resolveSelfWakeUrl().replace(/\/api\/wa_bridge\/wa\/wake$/i, '/api/whatsapp/keep-alive');
}

export function isSessionEligibleForActiveRelay(sess) {
    return Boolean(
        sess &&
        sess.userId &&
        !sess.loggedOut &&
        !sess.stopped &&
        !(sess.config && sess.config.enabled === false) &&
        ((sess.creds && sess.creds.registered) || (sess.authBox && sess.authBox.creds && sess.authBox.creds.registered))
    );
}

export function evaluateWakeLeaseDecision(existingLease, opts = {}, nowMs = Date.now()) {
    const force = Boolean(opts && opts.force);
    const handover = Boolean(opts && opts.handover);
    const chainToken = String((opts && opts.chainToken) || '').trim();
    const hbTs = Number((existingLease && (existingLease.lastHeartbeatAt || existingLease.lastWakeAt)) || 0);
    const leaseExp = Number((existingLease && existingLease.leaseExpiresAt) || 0);
    const isFresh = Boolean(
        existingLease &&
        existingLease.activeToken &&
        hbTs > 0 &&
        (nowMs - hbTs) < LEASE_STALE_MS &&
        (leaseExp === 0 || nowMs < leaseExp)
    );

    if (force) {
        return { allow: true, reason: 'forced' };
    }
    if (chainToken) {
        if (!existingLease || !existingLease.activeToken || existingLease.activeToken === chainToken || !isFresh) {
            return { allow: true, reason: 'chain_valid' };
        }
        return { allow: false, reason: 'stale_chain' };
    }
    if (!handover && isFresh) {
        return { allow: false, reason: 'active_relay_healthy' };
    }
    return { allow: true, reason: 'stale_or_missing_recover' };
}

async function triggerRelayHandover(payload = {}) {
    const targetUrl = resolveSelfWakeUrl();
    const token = resolveBridgeToken();
    const delays = [0, 1000, 2500, 5000];
    for (let i = 0; i < delays.length; i++) {
        if (delays[i] > 0) await sleep(delays[i]);
        try {
            const wakeRes = await ipv4PostJson(
                targetUrl,
                payload,
                { 'X-Worker-Token': token },
                12000,
            );
            if (wakeRes && wakeRes.status >= 200 && wakeRes.status < 500) {
                return true;
            }
        } catch (_) {}
    }
    try {
        const fbRes = await ipv4Request(resolveFallbackKeepAliveUrl(), {
            method: 'GET',
            headers: {
                'X-Vercel-Cron': '1',
                'User-Agent': 'Aslan-AI-KeepAlive/1.0',
            },
            timeoutMs: 10000,
        });
        return Boolean(fbRes && fbRes.status >= 200 && fbRes.status < 500);
    } catch (_) {}
    return false;
}

function isAuthorizedRequest(request) {
    const expected = resolveBridgeToken();
    if (!expected) return true;
    const provided = String(request.headers.get('x-worker-token') || '').trim();
    if (!provided) return false;
    const a = Buffer.from(provided, 'utf-8');
    const b = Buffer.from(expected, 'utf-8');
    if (a.length !== b.length) return false;
    return crypto.timingSafeEqual(a, b);
}

function jsonResponse(status, payload) {
    return new Response(JSON.stringify(payload || {}), {
        status,
        headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store, no-cache, must-revalidate, private',
            'Pragma': 'no-cache',
            'X-Content-Type-Options': 'nosniff',
            'X-Engine': 'Aslan AI',
        },
    });
}

function ipv4Request(urlStr, options = {}, redirectCount = 0) {
    return new Promise((resolve, reject) => {
        let parsed;
        try {
            parsed = new URL(urlStr);
        } catch (err) {
            reject(err);
            return;
        }
        const isHttps = parsed.protocol === 'https:';
        const mod = isHttps ? https : http;
        const timeoutMs = Number(options.timeoutMs || 20000);
        const body = options.body !== undefined && options.body !== null
            ? (Buffer.isBuffer(options.body) ? options.body : Buffer.from(String(options.body), 'utf-8'))
            : null;
        const headers = Object.assign({
            'User-Agent': 'Aslan-AI/1.0',
        }, options.headers || {});
        if (body && !headers['Content-Length'] && !headers['content-length']) {
            headers['Content-Length'] = String(body.length);
        }
        const reqOpts = {
            protocol: parsed.protocol,
            hostname: parsed.hostname,
            port: parsed.port || (isHttps ? 443 : 80),
            path: parsed.pathname + parsed.search,
            method: String(options.method || 'GET').toUpperCase(),
            family: 4,
            lookup: ipv4Lookup,
            agent: isHttps ? ipv4Agent : ipv4HttpAgent,
            timeout: timeoutMs,
            headers,
        };
        const req = mod.request(reqOpts, (res) => {
            const statusCode = res.statusCode || 200;
            const loc = res.headers && res.headers.location;
            if (
                loc &&
                [301, 302, 303, 307, 308].includes(statusCode) &&
                redirectCount < 3
            ) {
                res.resume();
                try {
                    const nextUrl = new URL(loc, urlStr);
                    if (nextUrl.protocol === 'https:' || nextUrl.protocol === 'http:') {
                        const nextOpts = Object.assign({}, options);
                        if (statusCode === 303 || ((statusCode === 301 || statusCode === 302) && reqOpts.method === 'POST')) {
                            nextOpts.method = 'GET';
                            nextOpts.body = null;
                        }
                        ipv4Request(nextUrl.toString(), nextOpts, redirectCount + 1)
                            .then(resolve)
                            .catch(reject);
                        return;
                    }
                } catch (_) {}
            }
            const chunks = [];
            res.on('data', (c) => chunks.push(c));
            res.on('end', () => {
                const buffer = Buffer.concat(chunks);
                const text = buffer.toString('utf-8');
                let json = null;
                try {
                    json = JSON.parse(text);
                } catch (_) {}
                resolve({ status: statusCode, headers: res.headers, buffer, text, json });
            });
        });
        req.on('timeout', () => req.destroy(new Error('Request timeout')));
        req.on('error', reject);
        if (body) req.write(body);
        req.end();
    });
}

async function ipv4PostJson(urlStr, payload, headers = {}, timeoutMs = 25000) {
    const body = JSON.stringify(payload || {});
    const res = await ipv4Request(urlStr, {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, headers || {}),
        body,
        timeoutMs,
    });
    return { status: res.status, data: res.json, text: res.text };
}

/* ------------------------------------------------------------------ Encrypted Vercel Blob + /tmp Session Store */
function blobToken() {
    return String(process.env.BLOB_READ_WRITE_TOKEN || '').trim();
}

function blobApiUrl() {
    return String(process.env.VERCEL_BLOB_API_URL || 'https://blob.vercel-storage.com')
        .trim()
        .replace(/\/+$/, '');
}

function blobStoreId() {
    const explicit = String(process.env.BLOB_STORE_ID || '').trim();
    if (explicit) return explicit;
    const tok = blobToken();
    const parts = tok.split('_');
    if (parts.length >= 5 && parts[0] === 'vercel' && parts[1] === 'blob') {
        return parts[3];
    }
    return '';
}

function blobEnabled() {
    if (process.env.ASLAN_DISABLE_BLOB_DB === '1') return false;
    return Boolean(blobToken() && blobStoreId());
}

function userTag(userId) {
    return crypto.createHash('sha256').update(String(userId || '')).digest('hex').slice(0, 24);
}

function userBlobPrefix(userId) {
    return `aslan-wa/u/${userTag(userId)}/`;
}

function blobCipherKey() {
    return crypto.createHash('sha256').update(`aslan-wa-blob-v1|${resolveBridgeSeed()}`).digest();
}

function encryptSessionPayload(payload) {
    const jsonStr = JSON.stringify(payload || {}, BufferJSON.replacer);
    const plain = Buffer.from(jsonStr, 'utf-8');
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', blobCipherKey(), iv);
    const enc = Buffer.concat([cipher.update(plain), cipher.final()]);
    const tag = cipher.getAuthTag();
    return Buffer.concat([BLOB_MAGIC, iv, tag, enc]);
}

function decryptSessionPayload(buf) {
    if (!Buffer.isBuffer(buf) || buf.length < BLOB_MAGIC.length + 12 + 16) return null;
    if (!buf.subarray(0, BLOB_MAGIC.length).equals(BLOB_MAGIC)) return null;
    try {
        const ivStart = BLOB_MAGIC.length;
        const tagStart = ivStart + 12;
        const encStart = tagStart + 16;
        const iv = buf.subarray(ivStart, tagStart);
        const tag = buf.subarray(tagStart, encStart);
        const enc = buf.subarray(encStart);
        const decipher = crypto.createDecipheriv('aes-256-gcm', blobCipherKey(), iv);
        decipher.setAuthTag(tag);
        const plain = Buffer.concat([decipher.update(enc), decipher.final()]).toString('utf-8');
        const parsed = JSON.parse(plain, BufferJSON.reviver);
        return parsed && typeof parsed === 'object' ? parsed : null;
    } catch (_) {
        return null;
    }
}

function tmpSessionFilePath(userId) {
    return path.join(TMP_SESSION_ROOT, userTag(userId), 'session.enc');
}

function saveUserSessionToTmp(userId, payload) {
    if (!userId || process.env.WA_MOCK_MODE === '1') return false;
    try {
        const filePath = tmpSessionFilePath(userId);
        fs.mkdirSync(path.dirname(filePath), { recursive: true });
        fs.writeFileSync(filePath, encryptSessionPayload(payload), { mode: 0o600 });
        return true;
    } catch (_) {
        return false;
    }
}

function loadUserSessionFromTmp(userId) {
    if (!userId || process.env.WA_MOCK_MODE === '1') return null;
    try {
        const filePath = tmpSessionFilePath(userId);
        if (!fs.existsSync(filePath)) return null;
        const parsed = decryptSessionPayload(fs.readFileSync(filePath));
        return parsed && parsed.userId === userId ? parsed : null;
    } catch (_) {
        return null;
    }
}

function deleteUserSessionFromTmp(userId) {
    if (!userId || process.env.WA_MOCK_MODE === '1') return false;
    try {
        fs.rmSync(path.dirname(tmpSessionFilePath(userId)), { recursive: true, force: true });
        return true;
    } catch (_) {
        return false;
    }
}

async function blobList(prefix, limit = 25) {
    if (!blobEnabled() || !prefix) return [];
    const qs = new URLSearchParams({ limit: String(limit), prefix: String(prefix) }).toString();
    const url = `${blobApiUrl()}/?${qs}`;
    try {
        const res = await ipv4Request(url, {
            method: 'GET',
            headers: {
                authorization: `Bearer ${blobToken()}`,
                'x-api-version': BLOB_API_VERSION,
                'x-vercel-blob-store-id': blobStoreId(),
            },
            timeoutMs: 12000,
        });
        if (res.status >= 200 && res.status < 300 && res.json && Array.isArray(res.json.blobs)) {
            return res.json.blobs;
        }
    } catch (_) {}
    return [];
}

async function blobPutExact(pathname, dataBuf) {
    if (!blobEnabled() || !pathname || !dataBuf) return null;
    const encodedPath = String(pathname).split('/').map(encodeURIComponent).join('/');
    const url = `${blobApiUrl()}/?pathname=${encodedPath}`;
    try {
        const res = await ipv4Request(url, {
            method: 'PUT',
            headers: {
                authorization: `Bearer ${blobToken()}`,
                'x-api-version': BLOB_API_VERSION,
                'x-vercel-blob-store-id': blobStoreId(),
                'x-vercel-blob-access': 'public',
                'x-add-random-suffix': '0',
                'x-allow-overwrite': '0',
                'content-type': 'application/octet-stream',
                'content-length': String(dataBuf.length),
            },
            body: dataBuf,
            timeoutMs: 18000,
        });
        if (res.status >= 200 && res.status < 300 && res.json && res.json.url) {
            return res.json;
        }
    } catch (_) {}
    return null;
}

async function blobDeleteUrls(urls) {
    const clean = (urls || []).map(String).filter(Boolean);
    if (!blobEnabled() || !clean.length) return false;
    try {
        const res = await ipv4Request(`${blobApiUrl()}/delete`, {
            method: 'POST',
            headers: {
                authorization: `Bearer ${blobToken()}`,
                'x-api-version': BLOB_API_VERSION,
                'x-vercel-blob-store-id': blobStoreId(),
                'content-type': 'application/json',
            },
            body: JSON.stringify({ urls: clean }),
            timeoutMs: 12000,
        });
        return res.status === 200 || res.status === 204;
    } catch (_) {
        return false;
    }
}

async function blobFetchUrl(url) {
    if (!url) return null;
    const sep = url.includes('?') ? '&' : '?';
    try {
        const res = await ipv4Request(`${url}${sep}_cb=${Date.now()}`, {
            method: 'GET',
            headers: { 'Cache-Control': 'no-cache' },
            timeoutMs: 12000,
        });
        if (res.status >= 200 && res.status < 300) {
            return res.buffer;
        }
    } catch (_) {}
    return null;
}

async function saveUserSessionToBlob(userId, payload) {
    if (!blobEnabled() || !userId || process.env.WA_MOCK_MODE === '1') return false;
    const prefix = userBlobPrefix(userId);
    const ts = String(Date.now()).padStart(15, '0');
    const rand = crypto.randomBytes(3).toString('hex');
    const newPath = `${prefix}v${ts}_${rand}.enc`;
    const encBuf = encryptSessionPayload(payload);
    const uploaded = await blobPutExact(newPath, encBuf);
    if (!uploaded) return false;
    try {
        const blobs = await blobList(prefix, 100);
        const stale = blobs
            .filter((b) => String(b.pathname || '').startsWith(prefix) && b.pathname !== newPath && b.url)
            .map((b) => b.url);
        if (stale.length) {
            await blobDeleteUrls(stale);
        }
    } catch (_) {}
    return true;
}

async function loadUserSessionFromBlob(userId) {
    if (!blobEnabled() || !userId || process.env.WA_MOCK_MODE === '1') return null;
    const prefix = userBlobPrefix(userId);
    const blobs = await blobList(prefix, 25);
    const candidates = blobs
        .filter((b) => String(b.pathname || '').startsWith(prefix) && String(b.pathname || '').endsWith('.enc'))
        .sort((a, b) => String(b.pathname || '').localeCompare(String(a.pathname || '')));
    for (const item of candidates) {
        const target = item.url || item.downloadUrl || '';
        const raw = await blobFetchUrl(target);
        const parsed = decryptSessionPayload(raw);
        if (parsed && parsed.userId === userId) {
            return parsed;
        }
    }
    return null;
}

async function loadSavedSession(userId) {
    if (!userId || process.env.WA_MOCK_MODE === '1') return null;
    const localSess = loadUserSessionFromTmp(userId);
    const blobSess = blobEnabled() ? await loadUserSessionFromBlob(userId) : null;
    if (localSess && blobSess) {
        return Number(blobSess.updatedAt || 0) >= Number(localSess.updatedAt || 0) ? blobSess : localSess;
    }
    return blobSess || localSess || null;
}

async function deleteUserSessionFromBlob(userId) {
    if (!blobEnabled() || !userId || process.env.WA_MOCK_MODE === '1') return false;
    const prefix = userBlobPrefix(userId);
    const blobs = await blobList(prefix, 100);
    const urls = blobs
        .filter((b) => String(b.pathname || '').startsWith(prefix) && b.url)
        .map((b) => b.url);
    return blobDeleteUrls(urls);
}

async function listActiveBlobSessions() {
    if (!blobEnabled() || process.env.WA_MOCK_MODE === '1') return [];
    const blobs = await blobList('aslan-wa/u/', 100);
    const byTag = new Map();
    for (const b of blobs) {
        const p = String(b.pathname || '');
        const parts = p.split('/');
        if (parts.length < 4 || !p.endsWith('.enc')) continue;
        const tag = parts[2];
        if (!byTag.has(tag)) byTag.set(tag, []);
        byTag.get(tag).push(b);
    }
    const out = [];
    for (const list of byTag.values()) {
        list.sort((a, b) => String(b.pathname || '').localeCompare(String(a.pathname || '')));
        for (const item of list) {
            const raw = await blobFetchUrl(item.url || item.downloadUrl || '');
            const parsed = decryptSessionPayload(raw);
            if (parsed && parsed.userId) {
                out.push(parsed);
                break;
            }
        }
    }
    return out;
}

async function listSavedActiveSessions() {
    if (process.env.WA_MOCK_MODE === '1') return [];
    const byUser = new Map();
    if (blobEnabled()) {
        const blobSessions = await listActiveBlobSessions();
        for (const s of blobSessions) {
            if (s && s.userId) byUser.set(String(s.userId), s);
        }
    }
    try {
        if (fs.existsSync(TMP_SESSION_ROOT)) {
            const entries = fs.readdirSync(TMP_SESSION_ROOT, { withFileTypes: true });
            for (const ent of entries) {
                if (!ent.isDirectory()) continue;
                const fp = path.join(TMP_SESSION_ROOT, ent.name, 'session.enc');
                if (!fs.existsSync(fp)) continue;
                try {
                    const parsed = decryptSessionPayload(fs.readFileSync(fp));
                    if (parsed && parsed.userId) {
                        const prev = byUser.get(String(parsed.userId));
                        if (!prev || Number(parsed.updatedAt || 0) > Number(prev.updatedAt || 0)) {
                            byUser.set(String(parsed.userId), parsed);
                        }
                    }
                } catch (_) {}
            }
        }
    } catch (_) {}
    return Array.from(byUser.values());
}

/* ------------------------------------------------------------------ Persistent Wake Lease */
const WAKE_LEASE_TMP_PATH = path.join(TMP_SESSION_ROOT, '_sys_wake_lease.enc');
const WAKE_LEASE_BLOB_PREFIX = 'aslan-wa/sys/wake_lease/';

async function loadWakeLease() {
    let tmpLease = null;
    try {
        if (fs.existsSync(WAKE_LEASE_TMP_PATH)) {
            tmpLease = decryptSessionPayload(fs.readFileSync(WAKE_LEASE_TMP_PATH));
        }
    } catch (_) {}

    let blobLease = null;
    if (blobEnabled() && process.env.WA_MOCK_MODE !== '1') {
        const blobs = await blobList(WAKE_LEASE_BLOB_PREFIX, 15);
        const sorted = blobs
            .filter((b) => String(b.pathname || '').startsWith(WAKE_LEASE_BLOB_PREFIX) && String(b.pathname || '').endsWith('.enc'))
            .sort((a, b) => String(b.pathname || '').localeCompare(String(a.pathname || '')));
        for (const b of sorted) {
            const parsed = decryptSessionPayload(await blobFetchUrl(b.url || b.downloadUrl || ''));
            if (parsed && typeof parsed === 'object') {
                blobLease = parsed;
                break;
            }
        }
    }

    if (tmpLease && blobLease) {
        return Number(blobLease.updatedAt || 0) >= Number(tmpLease.updatedAt || 0) ? blobLease : tmpLease;
    }
    return blobLease || tmpLease || null;
}

async function saveWakeLease(lease) {
    const payload = Object.assign({}, lease || {}, { updatedAt: Date.now() });
    try {
        fs.mkdirSync(path.dirname(WAKE_LEASE_TMP_PATH), { recursive: true });
        fs.writeFileSync(WAKE_LEASE_TMP_PATH, encryptSessionPayload(payload), { mode: 0o600 });
    } catch (_) {}

    if (blobEnabled() && process.env.WA_MOCK_MODE !== '1') {
        const ts = String(Date.now()).padStart(15, '0');
        const rand = crypto.randomBytes(3).toString('hex');
        const newPath = `${WAKE_LEASE_BLOB_PREFIX}v${ts}_${rand}.enc`;
        const up = await blobPutExact(newPath, encryptSessionPayload(payload));
        if (up) {
            try {
                const blobs = await blobList(WAKE_LEASE_BLOB_PREFIX, 20);
                const stale = blobs
                    .filter((b) => String(b.pathname || '').startsWith(WAKE_LEASE_BLOB_PREFIX) && b.pathname !== newPath && b.url)
                    .map((b) => b.url);
                if (stale.length) await blobDeleteUrls(stale);
            } catch (_) {}
        }
    }
}

/* ------------------------------------------------------------------ Config & Bot Message Runtime */
function normalizePhoneDigits(rawPhone) {
    return String(rawPhone || '').replace(/\D/g, '');
}

function normalizeRecentIds(arr) {
    if (!Array.isArray(arr)) return [];
    return arr.map(String).filter(Boolean).slice(-MAX_RECENT_MSG_IDS);
}

function normalizeTurkishFold(s) {
    return String(s || '')
        .replace(/İ/g, 'i')
        .replace(/I/g, 'ı')
        .replace(/ı/g, 'i')
        .replace(/Ş/gi, 's')
        .replace(/Ğ/gi, 'g')
        .replace(/Ü/gi, 'u')
        .replace(/Ö/gi, 'o')
        .replace(/Ç/gi, 'c')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLowerCase()
        .trim();
}

function hydrateContacts(raw) {
    const map = new Map();
    const list = Array.isArray(raw) ? raw : [];
    for (const item of list) {
        if (!item || typeof item !== 'object') continue;
        const jid = String(item.jid || item.id || '').trim();
        if (!jid || jid === 'status@broadcast') continue;
        map.set(jid, {
            jid,
            name: String(item.name || item.subject || '').trim(),
            notify: String(item.notify || '').trim(),
            isGroup: Boolean(item.isGroup || item.is_group || jid.endsWith('@g.us')),
            updatedAt: Number(item.updatedAt || Date.now()),
        });
    }
    return map;
}

function serializeContacts(instance) {
    const map = instance && instance.contacts instanceof Map ? instance.contacts : null;
    if (!map) return [];
    const arr = Array.from(map.values())
        .sort((a, b) => Number(b.updatedAt || 0) - Number(a.updatedAt || 0))
        .slice(0, 500);
    return arr;
}

function upsertContactEntry(instance, entry) {
    if (!instance) return;
    if (!(instance.contacts instanceof Map)) instance.contacts = new Map();
    if (!entry || typeof entry !== 'object') return;
    const jid = String(entry.jid || entry.id || '').trim();
    if (!jid || jid === 'status@broadcast') return;
    const prev = instance.contacts.get(jid) || {};
    const name = String(entry.name || entry.subject || prev.name || '').trim();
    const notify = String(entry.notify || prev.notify || '').trim();
    instance.contacts.set(jid, {
        jid,
        name,
        notify,
        isGroup: Boolean(entry.isGroup || entry.is_group || jid.endsWith('@g.us')),
        updatedAt: Date.now(),
    });
    if (instance.contacts.size > 500) {
        const sorted = serializeContacts(instance);
        instance.contacts = hydrateContacts(sorted);
    }
}

async function syncParticipatingGroups(instance, sock) {
    if (!sock || typeof sock.groupFetchAllParticipating !== 'function') return;
    try {
        const groups = await sock.groupFetchAllParticipating();
        if (groups && typeof groups === 'object') {
            for (const [jid, meta] of Object.entries(groups)) {
                if (!jid) continue;
                upsertContactEntry(instance, {
                    jid,
                    name: (meta && meta.subject) || '',
                    isGroup: true,
                });
            }
        }
    } catch (_) {}
}

function bindContactEvents(instance, sock, onDirty = null) {
    if (!sock || !sock.ev) return;
    sock.ev.on('contacts.upsert', (list) => {
        for (const c of (list || [])) {
            upsertContactEntry(instance, { jid: c.id || c.jid, name: c.name || c.verifiedName || '', notify: c.notify || '', isGroup: false });
        }
        if (onDirty) onDirty();
    });
    sock.ev.on('contacts.update', (list) => {
        for (const c of (list || [])) {
            upsertContactEntry(instance, { jid: c.id || c.jid, name: c.name || c.verifiedName || '', notify: c.notify || '', isGroup: false });
        }
        if (onDirty) onDirty();
    });
    sock.ev.on('chats.upsert', (list) => {
        for (const c of (list || [])) {
            const jid = c.id || '';
            upsertContactEntry(instance, { jid, name: c.name || c.subject || '', isGroup: jid.endsWith('@g.us') });
        }
        if (onDirty) onDirty();
    });
    sock.ev.on('chats.update', (list) => {
        for (const c of (list || [])) {
            const jid = c.id || '';
            if (c.name || c.subject) {
                upsertContactEntry(instance, { jid, name: c.name || c.subject || '', isGroup: jid.endsWith('@g.us') });
            }
        }
        if (onDirty) onDirty();
    });
    sock.ev.on('groups.upsert', (list) => {
        for (const g of (list || [])) {
            upsertContactEntry(instance, { jid: g.id, name: g.subject || '', isGroup: true });
        }
        if (onDirty) onDirty();
    });
    sock.ev.on('groups.update', (list) => {
        for (const g of (list || [])) {
            if (g && g.id && g.subject) {
                upsertContactEntry(instance, { jid: g.id, name: g.subject, isGroup: true });
            }
        }
        if (onDirty) onDirty();
    });
}

function normalizePhoneTarget(rawTarget) {
    const raw = String(rawTarget || '').trim();
    if (!/^\+?[\d\s().-]{10,20}$/.test(raw)) return '';
    let digits = raw.replace(/\D/g, '');
    if (digits.length === 10 && digits.startsWith('5')) digits = '90' + digits;
    else if (digits.length === 11 && digits.startsWith('05')) digits = '9' + digits;
    return (digits.length >= 10 && digits.length <= 15) ? digits : '';
}

async function resolveTargetJid(instance, sock, rawTarget) {
    const clean = String(rawTarget || '').trim();
    if (!clean) return { ok: false, notFound: true, error: 'Hedef belirtilmedi.' };

    if (clean.endsWith('@s.whatsapp.net') || clean.endsWith('@g.us')) {
        const hit = instance && instance.contacts ? instance.contacts.get(clean) : null;
        return {
            ok: true,
            jid: clean,
            target_label: (hit && (hit.name || hit.notify)) || clean,
            is_group: clean.endsWith('@g.us'),
        };
    }

    const digits = normalizePhoneTarget(clean);
    if (digits) {
        let jid = `${digits}@s.whatsapp.net`;
        if (sock && typeof sock.onWhatsApp === 'function') {
            try {
                const res = await sock.onWhatsApp(digits);
                if (Array.isArray(res) && res[0]) {
                    if (res[0].exists === false) {
                        return { ok: false, notFound: true, error: 'Bu numara WhatsApp üzerinde kayıtlı görünmüyor.' };
                    }
                    if (res[0].jid) jid = res[0].jid;
                }
            } catch (_) {}
        }
        const hit = instance && instance.contacts ? instance.contacts.get(jid) : null;
        return {
            ok: true,
            jid,
            target_label: (hit && (hit.name || hit.notify)) || `+${digits}`,
            is_group: false,
        };
    }

    if (sock) {
        await syncParticipatingGroups(instance, sock);
    }

    const queryFold = normalizeTurkishFold(clean);
    const entries = instance && instance.contacts instanceof Map
        ? Array.from(instance.contacts.values())
        : [];
    const exactMatches = [];
    const prefixMatches = [];
    const subMatches = [];

    for (const c of entries) {
        const nFold = normalizeTurkishFold(c.name || '');
        const pFold = normalizeTurkishFold(c.notify || '');
        const names = [nFold, pFold].filter(Boolean);
        if (!names.length) continue;
        if (names.some((n) => n === queryFold)) {
            exactMatches.push(c);
        } else if (names.some((n) => n.startsWith(queryFold))) {
            prefixMatches.push(c);
        } else if (names.some((n) => n.includes(queryFold))) {
            subMatches.push(c);
        }
    }

    const bucket = exactMatches.length ? exactMatches : (prefixMatches.length ? prefixMatches : subMatches);
    if (!bucket.length) {
        return {
            ok: false,
            notFound: true,
            error: `"${clean}" adıyla eşleşen bir kişi veya grup sohbeti bulunamadı. Telefon numarasını ülke koduyla yazabilirsiniz.`,
        };
    }
    if (bucket.length > 1) {
        return {
            ok: false,
            ambiguous: true,
            candidates: bucket.slice(0, 6).map((c) => ({
                jid: c.jid,
                name: c.name || c.notify || c.jid,
                is_group: Boolean(c.isGroup || String(c.jid).endsWith('@g.us')),
            })),
            error: `"${clean}" için birden fazla eşleşme bulundu. Lütfen tam adını veya numarasını belirtin.`,
        };
    }

    const chosen = bucket[0];
    return {
        ok: true,
        jid: chosen.jid,
        target_label: chosen.name || chosen.notify || chosen.jid,
        is_group: Boolean(chosen.isGroup || String(chosen.jid).endsWith('@g.us')),
    };
}

function rememberMessageId(instance, msgId) {
    const cleanId = String(msgId || '').trim();
    if (!cleanId) return true;
    if (!Array.isArray(instance.recentMessageIds)) {
        instance.recentMessageIds = [];
    }
    if (instance.recentMessageIds.includes(cleanId)) {
        return false;
    }
    instance.recentMessageIds.push(cleanId);
    if (instance.recentMessageIds.length > MAX_RECENT_MSG_IDS) {
        instance.recentMessageIds = instance.recentMessageIds.slice(-MAX_RECENT_MSG_IDS);
    }
    return true;
}

function defaultConfig(raw) {
    const cfg = (raw && typeof raw === 'object') ? raw : {};
    const customAi = (cfg.custom_ai && typeof cfg.custom_ai === 'object') ? cfg.custom_ai : {};
    return {
        enabled: cfg.enabled !== false,
        allow_groups: Boolean(cfg.allow_groups),
        allow_private: cfg.allow_private !== false,
        group_mode: String(cfg.group_mode || (cfg.allow_groups ? 'mentions_or_triggers' : 'off')),
        trigger_keywords: Array.isArray(cfg.trigger_keywords) ? cfg.trigger_keywords.map(String).filter(Boolean) : [],
        ignored_keywords: Array.isArray(cfg.ignored_keywords) ? cfg.ignored_keywords.map(String).filter(Boolean) : [],
        typing_delay_seconds: Math.max(0, Math.min(30, Number(cfg.typing_delay_seconds ?? 2) || 0)),
        show_typing: cfg.show_typing !== false,
        tone: String(cfg.tone || 'Samimi, nazik ve yardımcı'),
        instructions: String(cfg.instructions || ''),
        default_reply: String(cfg.default_reply || ''),
        rules: Array.isArray(cfg.rules) ? cfg.rules.filter((r) => r && typeof r === 'object') : [],
        auto_reactions: Array.isArray(cfg.auto_reactions) ? cfg.auto_reactions.filter((r) => r && typeof r === 'object') : [],
        custom_ai: {
            enabled: Boolean(customAi.enabled),
            provider: String(customAi.provider || ''),
            model: String(customAi.model || ''),
            endpoint: String(customAi.endpoint || ''),
            api_key: String(customAi.api_key || ''),
        },
    };
}

function sanitizeConfigSummary(rawCfg) {
    const cfg = defaultConfig(rawCfg);
    return {
        enabled: cfg.enabled,
        allow_groups: cfg.allow_groups,
        allow_private: cfg.allow_private,
        group_mode: cfg.group_mode,
        trigger_keywords: cfg.trigger_keywords,
        ignored_keywords: cfg.ignored_keywords,
        typing_delay_seconds: cfg.typing_delay_seconds,
        show_typing: cfg.show_typing,
        tone: cfg.tone,
        rules_count: cfg.rules.length,
        auto_reactions_count: cfg.auto_reactions.length,
        has_default_reply: Boolean(cfg.default_reply),
        custom_ai: {
            enabled: Boolean(cfg.custom_ai.enabled),
            provider: cfg.custom_ai.provider || '',
            has_key: Boolean(cfg.custom_ai.api_key),
        },
    };
}

function extractMessageText(msg) {
    const m = (msg && msg.message) || {};
    return String(
        m.conversation ||
        (m.extendedTextMessage && m.extendedTextMessage.text) ||
        (m.imageMessage && m.imageMessage.caption) ||
        (m.videoMessage && m.videoMessage.caption) ||
        (m.documentMessage && m.documentMessage.caption) ||
        ''
    ).trim();
}

function matchesKeywordList(textLower, keywords) {
    if (!Array.isArray(keywords) || !keywords.length) return false;
    return keywords.some((kw) => {
        const clean = String(kw || '').trim().toLowerCase();
        return clean && textLower.includes(clean);
    });
}

function matchRuleReply(textLower, rules) {
    if (!Array.isArray(rules) || !rules.length) return '';
    for (const rule of rules) {
        const trig = String(rule.trigger || '').trim().toLowerCase();
        const resp = String(rule.response || '').trim();
        if (!trig || !resp) continue;
        const mtype = String(rule.match_type || 'contains').toLowerCase();
        if (mtype === 'exact' && textLower === trig) return resp;
        if (mtype === 'startswith' && textLower.startsWith(trig)) return resp;
        if (mtype === 'contains' && textLower.includes(trig)) return resp;
    }
    return '';
}

function findAutoReaction(textLower, reactions) {
    if (!Array.isArray(reactions) || !reactions.length) return '';
    for (const item of reactions) {
        const trig = String(item.trigger || '').trim().toLowerCase();
        const emoji = String(item.emoji || '').trim();
        if (trig && emoji && textLower.includes(trig)) {
            return emoji;
        }
    }
    return '';
}

function isSafeCustomHttpsUrl(urlStr) {
    try {
        const s = String(urlStr || '').trim();
        if (!s || s.length > 512) return false;
        const u = new URL(s);
        if (u.protocol !== 'https:') return false;
        if (u.username || u.password) return false;
        if (u.port && u.port !== '443' && u.port !== '8443') return false;
        const host = String(u.hostname || '').toLowerCase().replace(/\.$/, '');
        if (!host) return false;
        if (
            host === 'localhost' ||
            host === 'metadata.google.internal' ||
            host.endsWith('.local') ||
            host.endsWith('.internal') ||
            host.endsWith('.localhost')
        ) {
            return false;
        }
        if (net.isIP(host)) return false;
        return true;
    } catch (_) {
        return false;
    }
}

async function callCustomAi(aiCfg, userText, cfg) {
    const provider = String((aiCfg && aiCfg.provider) || 'openai').toLowerCase();
    const apiKey = String((aiCfg && aiCfg.api_key) || '').trim();
    if (!apiKey && provider !== 'custom') return '';
    const sysPrompt = [
        'Sen kullanicinin WhatsApp asistanisin.',
        cfg.tone ? `Uslup: ${cfg.tone}.` : '',
        cfg.instructions ? `Kurallar: ${cfg.instructions}` : '',
        'Kisa, dogal ve yardimsever yanit ver.',
    ].filter(Boolean).join(' ');

    try {
        if (provider === 'gemini') {
            const modelsToTry = Array.from(new Set([aiCfg.model || 'gemini-4-hydrogen', 'gemini-3.5-flash-lite']));
            for (const model of modelsToTry) {
                try {
                    const base = aiCfg.endpoint && isSafeCustomHttpsUrl(aiCfg.endpoint)
                        ? aiCfg.endpoint.replace(/\/+$/, '')
                        : 'https://generativelanguage.googleapis.com/v1beta/models';
                    const url = base.toLowerCase().includes(':generatecontent')
                        ? base
                        : `${base}/${encodeURIComponent(model)}:generateContent`;
                    const res = await ipv4PostJson(url, {
                        contents: [{ role: 'user', parts: [{ text: userText }] }],
                        systemInstruction: { parts: [{ text: sysPrompt }] },
                        generationConfig: { temperature: 0.7, maxOutputTokens: 600 },
                    }, { 'x-goog-api-key': apiKey }, 25000);
                    const cand = res.data && res.data.candidates && res.data.candidates[0];
                    const parts = cand && cand.content && cand.content.parts;
                    if (Array.isArray(parts)) {
                        const out = parts.map((p) => p.text || '').join('').trim();
                        if (out) return out;
                    }
                } catch (_) {}
            }
            return '';
        }

        if (provider === 'anthropic') {
            const ep = aiCfg.endpoint && isSafeCustomHttpsUrl(aiCfg.endpoint)
                ? aiCfg.endpoint
                : 'https://api.anthropic.com/v1/messages';
            const res = await ipv4PostJson(ep, {
                model: aiCfg.model || 'claude-3-5-haiku-latest',
                max_tokens: 600,
                system: sysPrompt,
                messages: [{ role: 'user', content: userText }],
            }, {
                'x-api-key': apiKey,
                'anthropic-version': '2023-06-01',
            }, 25000);
            const content = res.data && res.data.content;
            if (Array.isArray(content) && content[0] && content[0].text) {
                return String(content[0].text).trim();
            }
            return '';
        }

        let url = 'https://api.openai.com/v1/chat/completions';
        let model = aiCfg.model || 'gpt-4o-mini';
        if (provider === 'groq') {
            url = aiCfg.endpoint || 'https://api.groq.com/openai/v1/chat/completions';
            model = aiCfg.model || 'llama-3.3-70b-versatile';
        } else if (provider === 'openrouter') {
            url = aiCfg.endpoint || 'https://openrouter.ai/api/v1/chat/completions';
            model = aiCfg.model || 'openai/gpt-4o-mini';
        } else if (provider === 'deepseek') {
            url = aiCfg.endpoint || 'https://api.deepseek.com/v1/chat/completions';
            model = aiCfg.model || 'deepseek-chat';
        } else if (provider === 'mistral') {
            url = aiCfg.endpoint || 'https://api.mistral.ai/v1/chat/completions';
            model = aiCfg.model || 'mistral-small-latest';
        } else if (provider === 'custom') {
            url = String(aiCfg.endpoint || '').trim();
            if (!isSafeCustomHttpsUrl(url)) return '';
            const lowEp = url.toLowerCase();
            if (lowEp.includes(':generatecontent')) {
                const hdrs = apiKey ? { 'x-goog-api-key': apiKey } : {};
                const gRes = await ipv4PostJson(url, {
                    contents: [{ role: 'user', parts: [{ text: userText }] }],
                    systemInstruction: { parts: [{ text: sysPrompt }] },
                    generationConfig: { temperature: 0.7, maxOutputTokens: 600 },
                }, hdrs, 25000);
                const cand = gRes.data && gRes.data.candidates && gRes.data.candidates[0];
                const parts = cand && cand.content && cand.content.parts;
                if (Array.isArray(parts)) {
                    const out = parts.map((p) => p.text || '').join('').trim();
                    if (out) return out;
                }
                return '';
            }
            if (!['/chat/completions', '/completions', '/messages', '/responses', '/generate'].some((s) => lowEp.includes(s))) {
                url = url.replace(/\/+$/, '') + '/chat/completions';
            }
        } else if (aiCfg.endpoint) {
            url = aiCfg.endpoint;
        }

        if (!isSafeCustomHttpsUrl(url)) return '';
        const hdrs = apiKey ? { Authorization: `Bearer ${apiKey}` } : {};
        const res = await ipv4PostJson(url, {
            model,
            messages: [
                { role: 'system', content: sysPrompt },
                { role: 'user', content: userText },
            ],
            temperature: 0.7,
            max_tokens: 600,
        }, hdrs, 25000);

        const choices = res.data && res.data.choices;
        if (Array.isArray(choices) && choices[0] && choices[0].message && choices[0].message.content) {
            return String(choices[0].message.content).trim();
        }
        if (res.data && typeof res.data.reply === 'string') {
            return res.data.reply.trim();
        }
    } catch (_) {}
    return '';
}

function computeHumanDelayMs(seconds) {
    const base = Math.max(0, Math.min(30, Number(seconds) || 0)) * 1000;
    if (base <= 0) return 0;
    const factor = 0.85 + Math.random() * 0.30;
    return Math.round(base * factor);
}

async function evaluateIncomingMessage(instance, opts = {}) {
    const cfg = instance && instance.config;
    if (!cfg || !cfg.enabled) {
        return { skipped: true, replied: false, replyText: '', emoji: '', delayMs: 0, reason: 'disabled' };
    }

    const msgId = String(opts.msgId || '').trim();
    if (msgId && !rememberMessageId(instance, msgId)) {
        return { skipped: true, replied: false, replyText: '', emoji: '', delayMs: 0, reason: 'duplicate' };
    }

    const isGroup = Boolean(opts.isGroup);
    if (isGroup && !cfg.allow_groups) {
        return { skipped: false, replied: false, replyText: '', emoji: '', delayMs: 0, reason: 'group_blocked' };
    }
    if (!isGroup && !cfg.allow_private) {
        return { skipped: false, replied: false, replyText: '', emoji: '', delayMs: 0, reason: 'private_blocked' };
    }

    const cleanText = String(opts.text || '').trim();
    if (!cleanText) {
        return { skipped: false, replied: false, replyText: '', emoji: '', delayMs: 0, reason: 'empty' };
    }
    const textLower = cleanText.toLowerCase();

    if (matchesKeywordList(textLower, cfg.ignored_keywords)) {
        return { skipped: false, replied: false, replyText: '', emoji: '', delayMs: 0, reason: 'ignored_keyword' };
    }

    const isMentioned = Boolean(opts.isMentioned);
    const ruleReply = matchRuleReply(textLower, cfg.rules);
    const hasTriggerMatch = cfg.trigger_keywords.length === 0 || matchesKeywordList(textLower, cfg.trigger_keywords);

    if (isGroup && cfg.group_mode === 'mentions_or_triggers') {
        if (!isMentioned && !ruleReply && !(cfg.trigger_keywords.length > 0 && hasTriggerMatch)) {
            return { skipped: false, replied: false, replyText: '', emoji: '', delayMs: 0, reason: 'group_not_triggered' };
        }
    }

    const emoji = findAutoReaction(textLower, cfg.auto_reactions);

    if (!ruleReply && cfg.trigger_keywords.length > 0 && !hasTriggerMatch) {
        return { skipped: false, replied: false, replyText: '', emoji: emoji || '', delayMs: 0, reason: 'no_trigger' };
    }

    let replyText = ruleReply;
    if (!replyText && cfg.custom_ai && cfg.custom_ai.enabled && (cfg.custom_ai.api_key || (cfg.custom_ai.provider === 'custom' && cfg.custom_ai.endpoint))) {
        replyText = await callCustomAi(cfg.custom_ai, cleanText, cfg);
    }
    if (!replyText && cfg.default_reply) {
        replyText = cfg.default_reply;
    }

    const delayMs = replyText ? computeHumanDelayMs(cfg.typing_delay_seconds) : 0;
    return {
        skipped: false,
        replied: Boolean(replyText),
        replyText: replyText || '',
        emoji: emoji || '',
        delayMs,
    };
}

function lightweightPreFilterMessage(instance, msg) {
    const cfg = instance && instance.config;
    if (!cfg || !cfg.enabled) return false;
    if (!msg || !msg.key || msg.key.fromMe) return false;
    const remoteJid = String(msg.key.remoteJid || '');
    if (!remoteJid || remoteJid === 'status@broadcast') return false;
    const msgId = String(msg.key.id || '').trim();
    if (msgId && Array.isArray(instance.recentMessageIds) && instance.recentMessageIds.includes(msgId)) {
        return false;
    }
    const text = extractMessageText(msg);
    if (!text) return false;
    const isGroup = remoteJid.endsWith('@g.us');
    if (isGroup && !cfg.allow_groups) return false;
    if (!isGroup && !cfg.allow_private) return false;
    if (matchesKeywordList(text.toLowerCase(), cfg.ignored_keywords)) {
        return false;
    }
    return true;
}

async function wakeAndProcessMessage(instance, msg) {
    instance.sleepState = 'awake';
    instance.lastWakeAt = Date.now();
    if (process.env.WA_MOCK_MODE !== '1' && instance.userId) {
        try {
            const remote = await loadSavedSession(instance.userId);
            if (remote && remote.config && Number(remote.updatedAt || 0) > Number(instance.updatedAt || 0)) {
                instance.config = defaultConfig(remote.config);
                instance.updatedAt = Number(remote.updatedAt);
            }
        } catch (_) {}
    }
    return handleIncomingMessage(instance, msg);
}

async function handleIncomingMessage(instance, msg) {
    if (!msg || !msg.key || msg.key.fromMe) {
        return { skipped: true, replied: false };
    }
    const remoteJid = String(msg.key.remoteJid || '');
    if (!remoteJid || remoteJid === 'status@broadcast') {
        return { skipped: true, replied: false };
    }

    const text = extractMessageText(msg);
    if (!text) {
        return { skipped: false, replied: false };
    }

    const isGroup = remoteJid.endsWith('@g.us');
    const mentionedJids = (
        msg.message &&
        msg.message.extendedTextMessage &&
        msg.message.extendedTextMessage.contextInfo &&
        msg.message.extendedTextMessage.contextInfo.mentionedJid
    ) || [];
    const selfRawId = String((instance.sock && instance.sock.user && instance.sock.user.id) || '').split(':')[0];
    const selfJid = selfRawId ? `${selfRawId}@s.whatsapp.net` : '';
    const isMentioned = Boolean(selfJid && Array.isArray(mentionedJids) && mentionedJids.includes(selfJid));

    const res = await evaluateIncomingMessage(instance, {
        text,
        isGroup,
        isMentioned,
        msgId: msg.key.id || '',
    });
    if (res.skipped) return res;

    if (res.emoji && instance.sock) {
        try {
            await instance.sock.sendMessage(remoteJid, { react: { text: res.emoji, key: msg.key } });
        } catch (_) {}
    }

    if (!res.replied || !res.replyText || !instance.sock) {
        return res;
    }

    try {
        if (instance.config && instance.config.show_typing) {
            await instance.sock.sendPresenceUpdate('composing', remoteJid);
        }
        if (res.delayMs > 0) {
            await sleep(res.delayMs);
        }
        if (instance.config && instance.config.show_typing) {
            await instance.sock.sendPresenceUpdate('paused', remoteJid);
        }
        await instance.sock.sendMessage(remoteJid, { text: res.replyText }, { quoted: msg });
    } catch (_) {
        return Object.assign({}, res, { replied: false });
    }
    return res;
}

/* ------------------------------------------------------------------ Blob-Backed Baileys Auth State */
function createBlobAuthBox(instance, initialSession = null) {
    const creds = (initialSession && initialSession.creds) ? initialSession.creds : initAuthCreds();
    const keyStore = (initialSession && initialSession.keys && typeof initialSession.keys === 'object')
        ? initialSession.keys
        : {};
    let flushTimer = null;
    let flushChain = Promise.resolve();

    const doFlush = async () => {
        if (!instance || !instance.userId) return false;
        instance.updatedAt = Date.now();
        const snapshot = {
            userId: instance.userId,
            phone: instance.phone || '',
            status: instance.status || 'idle',
            connected: Boolean(instance.connected),
            loggedOut: Boolean(instance.loggedOut),
            connectedAt: instance.connectedAt || null,
            lastKeepAliveAt: instance.lastKeepAliveAt || 0,
            sleepState: instance.sleepState || (instance.connected ? 'sleeping' : 'idle'),
            lastWakeAt: instance.lastWakeAt || 0,
            lastMessageAt: instance.lastMessageAt || 0,
            messagesHandled: Number(instance.messagesHandled || 0),
            emptyWakeStreak: Number(instance.emptyWakeStreak || 0),
            recentMessageIds: normalizeRecentIds(instance.recentMessageIds),
            contacts: serializeContacts(instance),
            reconnectCount: instance.reconnectCount || 0,
            pairingCode: instance.pairingCode || null,
            expiresAt: instance.expiresAt || null,
            configUpdatesCount: instance.configUpdatesCount || 0,
            config: defaultConfig(instance.config),
            updatedAt: instance.updatedAt,
            creds,
            keys: keyStore,
        };
        const tmpOk = saveUserSessionToTmp(instance.userId, snapshot);
        const blobOk = await saveUserSessionToBlob(instance.userId, snapshot);
        return Boolean(blobOk || tmpOk);
    };

    const flush = (immediate = true) => {
        if (flushTimer) {
            clearTimeout(flushTimer);
            flushTimer = null;
        }
        if (immediate) {
            flushChain = flushChain.then(doFlush).catch(() => false);
            return flushChain;
        }
        flushTimer = setTimeout(() => {
            flushTimer = null;
            flushChain = flushChain.then(doFlush).catch(() => false);
        }, 250);
        return flushChain;
    };

    const state = {
        creds,
        keys: {
            get: async (type, ids) => {
                const data = {};
                for (const id of ids || []) {
                    let value = keyStore[`${type}-${id}`] ?? null;
                    if (type === 'app-state-sync-key' && value) {
                        try {
                            value = proto.Message.AppStateSyncKeyData.fromObject(value);
                        } catch (_) {}
                    }
                    data[id] = value;
                }
                return data;
            },
            set: async (data) => {
                for (const category in data || {}) {
                    for (const id in data[category] || {}) {
                        const value = data[category][id];
                        const k = `${category}-${id}`;
                        if (value) {
                            keyStore[k] = value;
                        } else {
                            delete keyStore[k];
                        }
                    }
                }
                flush(false);
            },
        },
    };

    return { state, creds, keyStore, flush };
}

async function getLatestWaVersion() {
    const now = Date.now();
    if (cachedWaVersion.fetchedAt && (now - cachedWaVersion.fetchedAt) < 6 * 3600 * 1000) {
        return cachedWaVersion.version;
    }
    try {
        const fetched = await fetchLatestBaileysVersion({ timeout: 6000, httpsAgent: ipv4Agent });
        if (fetched && Array.isArray(fetched.version) && fetched.version.length === 3) {
            cachedWaVersion = { version: fetched.version, fetchedAt: now };
            return fetched.version;
        }
    } catch (_) {}
    return cachedWaVersion.version || FALLBACK_WA_VERSION;
}

function closeSocketQuietly(instance) {
    if (!instance) return;
    if (instance.reconnectTimer) {
        clearTimeout(instance.reconnectTimer);
        instance.reconnectTimer = null;
    }
    if (instance.sock) {
        try { instance.sock.ev.removeAllListeners(); } catch (_) {}
        try { instance.sock.ws.close(); } catch (_) {}
        try { instance.sock.end(undefined); } catch (_) {}
        instance.sock = null;
    }
}

async function openRawSocket(instance, authBox) {
    const version = await getLatestWaVersion();
    const sock = makeWASocket({
        version,
        auth: authBox.state,
        printQRInTerminal: false,
        browser: Browsers.ubuntu('Chrome'),
        logger: pino({ level: 'silent' }),
        agent: ipv4Agent,
        fetchAgent: ipv4Agent,
        markOnlineOnConnect: false,
        syncFullHistory: false,
        connectTimeoutMs: 20000,
        defaultQueryTimeoutMs: 20000,
    });
    instance.sock = sock;
    instance.authBox = authBox;
    return sock;
}

function waitForPairingReady(sock, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
        let settled = false;
        let wsCheckTimer = null;
        const cleanup = () => {
            if (wsCheckTimer) clearTimeout(wsCheckTimer);
            clearTimeout(hardTimer);
            try { sock.ev.off('connection.update', onUpdate); } catch (_) {}
        };
        const finishResolve = () => {
            if (settled) return;
            settled = true;
            cleanup();
            resolve();
        };
        const finishReject = (err) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(err);
        };
        const onUpdate = (update) => {
            if (!update) return;
            if (update.qr || update.connection === 'open') {
                finishResolve();
                return;
            }
            if (update.connection === 'close') {
                const statusCode = update.lastDisconnect && update.lastDisconnect.error
                    ? new Boom(update.lastDisconnect.error).output?.statusCode
                    : 0;
                finishReject(new Error(`Handshake closed (${statusCode || 0})`));
            }
        };
        const hardTimer = setTimeout(() => {
            if (isSocketOpen(sock)) {
                finishResolve();
            } else {
                finishReject(new Error('Bağlantı zaman aşımı'));
            }
        }, timeoutMs);
        sock.ev.on('connection.update', onUpdate);
        wsCheckTimer = setTimeout(() => {
            if (!settled && isSocketOpen(sock)) {
                finishResolve();
            }
        }, 5500);
    });
}

function runSocketLifecycleWindow(instance, initialSock, authBox, options = {}) {
    const opts = typeof options === 'number'
        ? { mode: 'wake_cycle', maxWindowMs: options }
        : (options || {});
    const mode = opts.mode || 'wake_cycle';
    const continuous = opts.continuous !== undefined
        ? Boolean(opts.continuous)
        : (CONTINUOUS_ACTIVE_MODE && mode !== 'pairing');
    const maxWindowMs = Math.max(5000, Number(opts.maxWindowMs || (mode === 'pairing' ? DEFAULT_PAIR_WINDOW_MS : DEFAULT_WAKE_MAX_MS)));
    const idleSleepMs = Math.max(1500, Number(opts.idleSleepMs || DEFAULT_IDLE_SLEEP_MS));
    const postReplyGraceMs = Math.max(1500, Number(opts.postReplyGraceMs || DEFAULT_POST_REPLY_GRACE_MS));

    instance.lifecycleActive = true;
    instance.sleepState = 'awake';
    instance.lastWakeAt = Date.now();

    return new Promise((resolve) => {
        let finished = false;
        let windowClosing = false;
        let windowTimer = null;
        let heartbeatTimer = null;
        let idleTimer = null;
        let openFallbackTimer = null;
        let inFlightMessages = 0;
        let messagesHandledInCycle = 0;
        let pendingFlushed = false;

        const clearIdleTimers = () => {
            if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
            if (openFallbackTimer) { clearTimeout(openFallbackTimer); openFallbackTimer = null; }
        };

        const finishWindow = async () => {
            if (finished) return;
            finished = true;
            windowClosing = true;
            if (windowTimer) { clearTimeout(windowTimer); windowTimer = null; }
            if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
            clearIdleTimers();

            if (inFlightMessages > 0) {
                const waitDeadline = Date.now() + 6000;
                while (inFlightMessages > 0 && Date.now() < waitDeadline) {
                    await sleep(200);
                }
            }

            if (!instance.loggedOut && !instance.stopped && (instance.connected || (authBox.creds && authBox.creds.registered))) {
                instance.emptyWakeStreak = messagesHandledInCycle > 0
                    ? 0
                    : ((instance.emptyWakeStreak || 0) + 1);
                instance.sleepState = continuous ? 'awake' : 'sleeping';
            } else if (instance.stopped || instance.loggedOut) {
                instance.sleepState = 'idle';
            }

            try {
                await authBox.flush(true);
            } catch (_) {}
            closeSocketQuietly(instance);
            instance.lifecycleActive = false;

            if (mode === 'pairing' && instance.connected && !instance.loggedOut && !instance.stopped) {
                if (process.env.WA_SELF_WAKE_DISABLED !== '1' && process.env.VERCEL && process.env.WA_MOCK_MODE !== '1') {
                    await triggerRelayHandover({
                        user_id: instance.userId,
                        force: true,
                        handover: true,
                        hop: 0,
                    }).catch(() => {});
                }
            }

            resolve({
                messagesHandled: messagesHandledInCycle,
                emptyWakeStreak: instance.emptyWakeStreak || 0,
            });
        };

        const scheduleIdleSleep = () => {
            if (continuous) return;
            if (finished || windowClosing) return;
            if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
            if (mode === 'pairing' && !instance.connected) return;
            if (!pendingFlushed || inFlightMessages > 0) return;
            const waitMs = messagesHandledInCycle > 0 ? postReplyGraceMs : idleSleepMs;
            idleTimer = setTimeout(() => {
                if (!finished && !windowClosing && inFlightMessages === 0) {
                    finishWindow();
                }
            }, waitMs);
        };

        const isRecentAppendMsg = (m) => {
            if (!m || !m.key || m.key.fromMe) return false;
            const rawTs = Number(m.messageTimestamp || 0);
            if (!rawTs) return false;
            return (rawTs * 1000) >= (Date.now() - 600000);
        };

        let bindSocket;
        const scheduleRelaySocketReconnect = (delayMs = 2000) => {
            if (finished || windowClosing || instance.stopped || instance.loggedOut) return;
            if (instance.reconnectTimer || instance.reconnecting) return;
            instance.reconnecting = true;
            instance.reconnectTimer = setTimeout(() => {
                instance.reconnectTimer = null;
                if (finished || windowClosing || instance.stopped || instance.loggedOut) {
                    instance.reconnecting = false;
                    return;
                }
                openRawSocket(instance, authBox)
                    .then((nextSock) => {
                        instance.reconnecting = false;
                        bindSocket(nextSock);
                    })
                    .catch(() => {
                        instance.reconnecting = false;
                        scheduleRelaySocketReconnect(4000);
                    });
            }, Math.max(250, Number(delayMs) || 2000));
        };

        bindSocket = (sock) => {
            if (!sock || finished || instance.stopped) return;
            bindContactEvents(instance, sock, () => {
                authBox.flush(false).catch(() => {});
            });
            sock.ev.on('creds.update', () => {
                authBox.flush(true).catch(() => {});
            });
            sock.ev.on('messages.upsert', (ev) => {
                if (!ev || !Array.isArray(ev.messages)) return;
                const isNotify = ev.type === 'notify';
                const isAppend = ev.type === 'append';
                if (!isNotify && !isAppend) return;

                for (const m of ev.messages) {
                    if (m && m.key && m.key.remoteJid && m.pushName && !m.key.fromMe) {
                        upsertContactEntry(instance, {
                            jid: m.key.remoteJid,
                            notify: m.pushName,
                            isGroup: String(m.key.remoteJid).endsWith('@g.us'),
                        });
                    }
                    if (isAppend && !isRecentAppendMsg(m)) continue;
                    if (!lightweightPreFilterMessage(instance, m)) continue;
                    inFlightMessages++;
                    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
                    wakeAndProcessMessage(instance, m)
                        .then((res) => {
                            if (res && res.replied) {
                                messagesHandledInCycle++;
                                instance.messagesHandled = (instance.messagesHandled || 0) + 1;
                                instance.lastMessageAt = Date.now();
                                instance.emptyWakeStreak = 0;
                            }
                        })
                        .catch(() => {})
                        .finally(() => {
                            inFlightMessages = Math.max(0, inFlightMessages - 1);
                            if (inFlightMessages === 0) {
                                scheduleIdleSleep();
                            }
                        });
                }
            });
            sock.ev.on('connection.update', (update) => {
                if (!update || finished || windowClosing || instance.stopped) return;
                const { connection, lastDisconnect, receivedPendingNotifications } = update;
                if (connection === 'open') {
                    instance.status = 'connected';
                    instance.connected = true;
                    instance.loggedOut = false;
                    instance.reconnecting = false;
                    instance.sleepState = 'awake';
                    instance.connectedAt = instance.connectedAt || Date.now();
                    instance.lastKeepAliveAt = Date.now();
                    instance.lastWakeAt = Date.now();
                    instance.pairingCode = null;
                    instance.updatedAt = Date.now();
                    syncParticipatingGroups(instance, sock)
                        .then(() => authBox.flush(true))
                        .catch(() => authBox.flush(true).catch(() => {}));
                    if (!continuous && !pendingFlushed && !openFallbackTimer) {
                        openFallbackTimer = setTimeout(() => {
                            openFallbackTimer = null;
                            pendingFlushed = true;
                            scheduleIdleSleep();
                        }, 8000);
                    }
                }
                if (receivedPendingNotifications === true) {
                    pendingFlushed = true;
                    if (openFallbackTimer) { clearTimeout(openFallbackTimer); openFallbackTimer = null; }
                    scheduleIdleSleep();
                }
                if (connection === 'close') {
                    const code = lastDisconnect && lastDisconnect.error
                        ? new Boom(lastDisconnect.error).output?.statusCode
                        : 0;
                    const loggedOut = code === DisconnectReason.loggedOut;
                    const connectionReplaced = code === (DisconnectReason.connectionReplaced || 440);
                    if (loggedOut) {
                        instance.connected = false;
                        instance.loggedOut = true;
                        instance.status = 'disconnected';
                        instance.sleepState = 'idle';
                        instance.updatedAt = Date.now();
                        finishWindow();
                        return;
                    }
                    if (connectionReplaced) {
                        finishWindow();
                        return;
                    }
                    const shouldRestart =
                        code === DisconnectReason.restartRequired ||
                        Boolean(authBox.creds && authBox.creds.registered) ||
                        Boolean(instance.pairingCode) ||
                        Boolean(instance.connected);
                    if (shouldRestart && !instance.reconnectTimer && !instance.reconnecting) {
                        const delayMs = code === DisconnectReason.restartRequired ? 250 : 2000;
                        if (instance.connected) {
                            instance.reconnectCount = (instance.reconnectCount || 0) + 1;
                            instance.status = 'reconnecting';
                        } else if (authBox.creds && authBox.creds.registered) {
                            instance.status = 'reconnecting';
                        }
                        authBox.flush(true).catch(() => {});
                        scheduleRelaySocketReconnect(delayMs);
                    }
                }
            });
        };

        heartbeatTimer = setInterval(() => {
            if (finished || windowClosing || instance.stopped || instance.loggedOut) {
                if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
                return;
            }
            if (isSocketOpen(instance.sock)) {
                pingSocket(instance.sock);
                instance.lastKeepAliveAt = Date.now();
                instance.lastWakeAt = Date.now();
                if (typeof opts.onHeartbeat === 'function') {
                    try { opts.onHeartbeat(); } catch (_) {}
                }
            } else if (
                !instance.reconnectTimer &&
                !instance.reconnecting &&
                (instance.connected || (authBox.creds && authBox.creds.registered))
            ) {
                scheduleRelaySocketReconnect(1200);
            }
        }, SOCKET_HEARTBEAT_MS);
        if (heartbeatTimer && typeof heartbeatTimer.unref === 'function') {
            heartbeatTimer.unref();
        }

        windowTimer = setTimeout(() => {
            finishWindow();
        }, maxWindowMs);

        if (initialSock) {
            bindSocket(initialSock);
        } else {
            openRawSocket(instance, authBox)
                .then((sock) => bindSocket(sock))
                .catch(() => finishWindow());
        }
    });
}

/* ------------------------------------------------------------------ Bridge Operations */
function makeBaseInstance(userId, phone = '', configInput = {}, existing = null) {
    const base = existing && typeof existing === 'object' ? existing : {};
    return {
        userId,
        phone: normalizePhoneDigits(phone) || base.phone || '',
        status: base.status || 'idle',
        connected: Boolean(base.connected),
        loggedOut: Boolean(base.loggedOut),
        reconnecting: false,
        connectedAt: base.connectedAt || null,
        lastKeepAliveAt: base.lastKeepAliveAt || 0,
        sleepState: base.sleepState || (base.connected ? 'sleeping' : 'idle'),
        lastWakeAt: base.lastWakeAt || 0,
        lastMessageAt: base.lastMessageAt || 0,
        messagesHandled: Number(base.messagesHandled || 0),
        emptyWakeStreak: Number(base.emptyWakeStreak || 0),
        recentMessageIds: normalizeRecentIds(base.recentMessageIds),
        contacts: hydrateContacts(base.contacts),
        sentMessages: Array.isArray(base.sentMessages) ? base.sentMessages.slice(-40) : [],
        reconnectCount: base.reconnectCount || 0,
        pairingCode: base.pairingCode || null,
        expiresAt: base.expiresAt || null,
        sock: null,
        creds: base.creds || null,
        authBox: base.authBox || null,
        stopped: false,
        lifecycleActive: Boolean(base.lifecycleActive),
        reconnectTimer: null,
        configUpdatesCount: base.configUpdatesCount || 0,
        updatedAt: base.updatedAt || Date.now(),
        config: defaultConfig(Object.keys(configInput || {}).length ? configInput : base.config),
    };
}

function formatStatusResponse(inst) {
    if (!inst) {
        return {
            success: true,
            status: 'idle',
            connected: false,
            connected_at: null,
            pairing_code_active: false,
            enabled: false,
            sleep_state: 'idle',
            last_wake_at: null,
            last_message_at: null,
            messages_handled: 0,
            config_updates_count: 0,
            reconnect_count: 0,
            config_summary: null,
        };
    }
    const pairActive = Boolean(inst.pairingCode && !inst.connected && inst.expiresAt && Date.now() < inst.expiresAt);
    return {
        success: true,
        status: inst.status || 'idle',
        connected: Boolean(inst.connected),
        connected_at: inst.connectedAt || null,
        pairing_code_active: pairActive,
        enabled: Boolean(inst.config && inst.config.enabled),
        sleep_state: inst.sleepState || (inst.connected ? 'sleeping' : 'idle'),
        last_wake_at: inst.lastWakeAt || null,
        last_message_at: inst.lastMessageAt || null,
        messages_handled: Number(inst.messagesHandled || 0),
        config_updates_count: inst.configUpdatesCount || 0,
        reconnect_count: inst.reconnectCount || 0,
        config_summary: sanitizeConfigSummary(inst.config),
    };
}

async function startPairing(userId, phone, configInput = {}) {
    const cleanUid = String(userId || '').trim();
    const digits = normalizePhoneDigits(phone);
    if (!cleanUid) {
        return { success: false, error: 'Kullanıcı kimliği gerekli.' };
    }
    if (digits.length < 10 || digits.length > 15) {
        return { success: false, error: 'Geçersiz telefon numarası.' };
    }

    const prev = instances.get(cleanUid);
    if (prev) {
        prev.stopped = true;
        closeSocketQuietly(prev);
    }

    const instance = makeBaseInstance(cleanUid, digits, configInput, null);
    instance.status = 'pairing';
    instance.sleepState = 'awake';
    instance.lastWakeAt = Date.now();
    instance.expiresAt = Date.now() + 60000;
    instances.set(cleanUid, instance);

    if (process.env.WA_MOCK_MODE === '1') {
        const digest = crypto.createHash('sha256')
            .update(`${cleanUid}:${digits}:${Date.now()}`)
            .digest('hex')
            .toUpperCase();
        const alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
        let raw8 = '';
        for (let i = 0; i < 8; i++) {
            const idx = parseInt(digest.slice(i * 2, i * 2 + 2), 16) % alphabet.length;
            raw8 += alphabet[idx];
        }
        instance.pairingCode = raw8;
        return {
            success: true,
            pairing_code: raw8,
            formatted_code: `${raw8.slice(0, 4)}-${raw8.slice(4, 8)}`,
            status: 'pairing',
            expires_in: 60,
        };
    }

    const authBox = createBlobAuthBox(instance, null);
    instance.authBox = authBox;

    try {
        const sock = await openRawSocket(instance, authBox);
        await waitForPairingReady(sock, 15000);
        await sleep(300);

        if (sock.authState && sock.authState.creds && sock.authState.creds.registered) {
            instance.status = 'connected';
            instance.connected = true;
            instance.connectedAt = Date.now();
            instance.pairingCode = null;
            await authBox.flush(true);
            safeWaitUntil(runSocketLifecycleWindow(instance, sock, authBox, { mode: 'wake_cycle' }));
            return {
                success: true,
                already_connected: true,
                status: 'connected',
            };
        }

        const raw = await sock.requestPairingCode(digits);
        const cleanCode = String(raw || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase().slice(0, 8);
        if (!cleanCode) {
            throw new Error('Eşleştirme kodu üretilemedi');
        }
        instance.pairingCode = cleanCode;
        await authBox.flush(true);
        safeWaitUntil(runSocketLifecycleWindow(instance, sock, authBox, { mode: 'pairing' }));
        return {
            success: true,
            pairing_code: cleanCode,
            formatted_code: cleanCode.length === 8 ? `${cleanCode.slice(0, 4)}-${cleanCode.slice(4)}` : cleanCode,
            status: 'pairing',
            expires_in: 60,
        };
    } catch (_) {
        instance.stopped = true;
        closeSocketQuietly(instance);
        instance.status = 'error';
        instance.sleepState = 'idle';
        return {
            success: false,
            error: 'Aslan AI WhatsApp eşleştirme kodu şu an oluşturulamadı. Lütfen birkaç saniye sonra tekrar deneyin.',
        };
    }
}

async function getStatus(userId) {
    const cleanUid = String(userId || '').trim();
    if (!cleanUid) return formatStatusResponse(null);

    const warm = instances.get(cleanUid) || null;
    const remote = process.env.WA_MOCK_MODE === '1'
        ? null
        : await loadSavedSession(cleanUid);

    if (!warm && !remote) {
        return formatStatusResponse(null);
    }

    if (warm && remote) {
        const warmTs = Number(warm.updatedAt || 0);
        const remTs = Number(remote.updatedAt || 0);
        const chosen = (remote.connected && !warm.connected && !warm.loggedOut) || remTs > warmTs
            ? {
                ...warm,
                status: remote.status || warm.status,
                connected: Boolean(remote.connected),
                loggedOut: Boolean(remote.loggedOut),
                connectedAt: remote.connectedAt || warm.connectedAt,
                sleepState: warm.lifecycleActive ? 'awake' : (remote.sleepState || warm.sleepState),
                lastWakeAt: Math.max(warm.lastWakeAt || 0, remote.lastWakeAt || 0),
                lastMessageAt: Math.max(warm.lastMessageAt || 0, remote.lastMessageAt || 0),
                messagesHandled: Math.max(warm.messagesHandled || 0, remote.messagesHandled || 0),
                emptyWakeStreak: Number(remote.emptyWakeStreak ?? warm.emptyWakeStreak ?? 0),
                recentMessageIds: normalizeRecentIds(remote.recentMessageIds || warm.recentMessageIds),
                pairingCode: remote.pairingCode ?? warm.pairingCode,
                expiresAt: remote.expiresAt ?? warm.expiresAt,
                creds: remote.creds || warm.creds || null,
                reconnectCount: Math.max(warm.reconnectCount || 0, remote.reconnectCount || 0),
                configUpdatesCount: Math.max(warm.configUpdatesCount || 0, remote.configUpdatesCount || 0),
                config: remTs > warmTs && remote.config ? defaultConfig(remote.config) : warm.config,
                updatedAt: Math.max(warmTs, remTs),
            }
            : warm;
        instances.set(cleanUid, chosen);
        if (
            process.env.WA_MOCK_MODE !== '1' &&
            process.env.VERCEL &&
            process.env.WA_SELF_WAKE_DISABLED !== '1' &&
            CONTINUOUS_ACTIVE_MODE &&
            isSessionEligibleForActiveRelay(chosen) &&
            !chosen.lifecycleActive
        ) {
            try {
                const lease = await loadWakeLease();
                if (evaluateWakeLeaseDecision(lease, { force: false }).allow) {
                    safeWaitUntil(runAutoWakeCycle({ targetUserId: cleanUid, force: false }));
                }
            } catch (_) {}
        }
        return formatStatusResponse(chosen);
    }

    if (warm) {
        if (
            process.env.WA_MOCK_MODE !== '1' &&
            process.env.VERCEL &&
            process.env.WA_SELF_WAKE_DISABLED !== '1' &&
            CONTINUOUS_ACTIVE_MODE &&
            isSessionEligibleForActiveRelay(warm) &&
            !warm.lifecycleActive
        ) {
            try {
                const lease = await loadWakeLease();
                if (evaluateWakeLeaseDecision(lease, { force: false }).allow) {
                    safeWaitUntil(runAutoWakeCycle({ targetUserId: cleanUid, force: false }));
                }
            } catch (_) {}
        }
        return formatStatusResponse(warm);
    }

    const hydrated = makeBaseInstance(cleanUid, remote.phone, remote.config, remote);
    instances.set(cleanUid, hydrated);
    if (
        process.env.WA_MOCK_MODE !== '1' &&
        process.env.VERCEL &&
        process.env.WA_SELF_WAKE_DISABLED !== '1' &&
        CONTINUOUS_ACTIVE_MODE &&
        isSessionEligibleForActiveRelay(hydrated)
    ) {
        try {
            const lease = await loadWakeLease();
            if (evaluateWakeLeaseDecision(lease, { force: false }).allow) {
                safeWaitUntil(runAutoWakeCycle({ targetUserId: cleanUid, force: false }));
            }
        } catch (_) {}
    }
    return formatStatusResponse(hydrated);
}

async function updateConfig(userId, newConfig) {
    const cleanUid = String(userId || '').trim();
    if (!cleanUid) {
        return { success: false, error: 'Kullanıcı kimliği gerekli.' };
    }
    const merged = defaultConfig(newConfig);
    let inst = instances.get(cleanUid) || null;
    const remote = process.env.WA_MOCK_MODE === '1'
        ? null
        : await loadSavedSession(cleanUid);

    if (!inst) {
        inst = makeBaseInstance(cleanUid, remote && remote.phone, merged, remote);
        if (!remote) inst.status = 'configured';
        instances.set(cleanUid, inst);
    } else {
        inst.config = merged;
    }
    inst.configUpdatesCount = (inst.configUpdatesCount || 0) + 1;
    inst.updatedAt = Date.now();

    if (inst.authBox) {
        await inst.authBox.flush(true);
    } else if (process.env.WA_MOCK_MODE !== '1') {
        const box = createBlobAuthBox(inst, remote);
        inst.authBox = box;
        await box.flush(true);
    }

    if (
        process.env.WA_MOCK_MODE !== '1' &&
        inst.connected &&
        inst.config.enabled &&
        !inst.loggedOut &&
        !inst.stopped &&
        !inst.lifecycleActive
    ) {
        safeWaitUntil(runAutoWakeCycle({ targetUserId: cleanUid, force: true }));
    }

    return {
        success: true,
        config_updates_count: inst.configUpdatesCount,
        config: sanitizeConfigSummary(inst.config),
    };
}

async function stopBot(userId, logout = false) {
    const cleanUid = String(userId || '').trim();
    if (!cleanUid) {
        return { success: true, status: 'idle', stopped: true };
    }
    let inst = instances.get(cleanUid) || null;
    const remote = process.env.WA_MOCK_MODE === '1'
        ? null
        : await loadSavedSession(cleanUid);

    if (!inst && !remote) {
        return { success: true, status: 'idle', stopped: true };
    }
    if (!inst) {
        inst = makeBaseInstance(cleanUid, remote.phone, remote.config, remote);
    }
    inst.stopped = true;
    inst.connected = false;
    inst.status = 'stopped';
    inst.sleepState = 'idle';
    if (inst.config) inst.config.enabled = false;
    if (logout && inst.sock && typeof inst.sock.logout === 'function') {
        try { await inst.sock.logout(); } catch (_) {}
    }
    closeSocketQuietly(inst);

    if (logout) {
        instances.delete(cleanUid);
        deleteUserSessionFromTmp(cleanUid);
        await deleteUserSessionFromBlob(cleanUid);
    } else {
        const box = inst.authBox || createBlobAuthBox(inst, remote);
        inst.authBox = box;
        await box.flush(true);
    }
    return { success: true, status: inst.status, stopped: true };
}

function waitForSocketOpen(sock, timeoutMs = 20000) {
    return new Promise((resolve, reject) => {
        if (isSocketOpen(sock)) {
            resolve();
            return;
        }
        let settled = false;
        const done = (fn, arg) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            try { sock.ev.off('connection.update', onUpd); } catch (_) {}
            fn(arg);
        };
        const onUpd = (u) => {
            if (!u) return;
            if (u.connection === 'open') done(resolve);
            else if (u.connection === 'close') done(reject, new Error('Oturum kapandı'));
        };
        const timer = setTimeout(() => {
            if (isSocketOpen(sock)) done(resolve);
            else done(reject, new Error('Bağlantı zaman aşımı'));
        }, timeoutMs);
        sock.ev.on('connection.update', onUpd);
    });
}

async function getContacts(userId) {
    const cleanUid = String(userId || '').trim();
    if (!cleanUid) return [];
    let inst = instances.get(cleanUid) || null;
    if (!inst && process.env.WA_MOCK_MODE !== '1') {
        const remote = await loadSavedSession(cleanUid);
        if (remote) {
            inst = makeBaseInstance(cleanUid, remote.phone, remote.config, remote);
            instances.set(cleanUid, inst);
        }
    }
    if (!inst || !(inst.contacts instanceof Map)) return [];
    return serializeContacts(inst).map((c) => ({
        jid: c.jid,
        name: c.name || c.notify || c.jid,
        notify: c.notify || '',
        is_group: Boolean(c.isGroup || String(c.jid).endsWith('@g.us')),
    }));
}

function setMockContacts(userId, contactsList = []) {
    if (process.env.WA_MOCK_MODE !== '1') {
        return { success: false, error: 'Devre dışı.' };
    }
    const cleanUid = String(userId || '').trim();
    const inst = instances.get(cleanUid);
    if (!inst) {
        return { success: false, error: 'Oturum bulunamadı.' };
    }
    for (const c of (Array.isArray(contactsList) ? contactsList : [])) {
        upsertContactEntry(inst, c);
    }
    return { success: true, count: inst.contacts.size };
}

async function sendDirectMessage(userId, target, message) {
    const cleanUid = String(userId || '').trim();
    const cleanTarget = String(target || '').trim();
    const cleanMsg = String(message || '').trim();
    if (!cleanUid) return { success: false, error: 'Kullanıcı kimliği gerekli.' };
    if (!cleanTarget) return { success: false, error: 'Hedef kişi, grup veya numara belirtilmelidir.' };
    if (!cleanMsg) return { success: false, error: 'Mesaj metni boş olamaz.' };

    let inst = instances.get(cleanUid) || null;
    let remote = null;
    if (process.env.WA_MOCK_MODE !== '1') {
        remote = await loadSavedSession(cleanUid);
        if (!inst && remote) {
            inst = makeBaseInstance(cleanUid, remote.phone, remote.config, remote);
            instances.set(cleanUid, inst);
        }
    }

    if (!inst || !inst.connected || inst.loggedOut || inst.stopped) {
        return { success: false, error: 'Bağlı WhatsApp oturumu bulunamadı.' };
    }

    if (process.env.WA_MOCK_MODE === '1') {
        const resolved = await resolveTargetJid(inst, null, cleanTarget);
        if (!resolved.ok) {
            return {
                success: false,
                error: resolved.error,
                ambiguous: Boolean(resolved.ambiguous),
                not_found: Boolean(resolved.notFound),
                candidates: resolved.candidates || [],
            };
        }
        const msgId = 'mock_' + crypto.randomBytes(6).toString('hex');
        const sentRec = {
            jid: resolved.jid,
            target_label: resolved.target_label,
            is_group: resolved.is_group,
            message: cleanMsg,
            message_id: msgId,
            sentAt: Date.now(),
        };
        if (!Array.isArray(inst.sentMessages)) inst.sentMessages = [];
        inst.sentMessages.push(sentRec);
        inst.lastMessageAt = Date.now();
        return Object.assign({ success: true, sent: true }, sentRec);
    }

    let sock = inst.sock;
    let openedTempSocket = false;
    const authBox = inst.authBox || createBlobAuthBox(inst, remote);
    inst.authBox = authBox;
    try {
        if (!isSocketOpen(sock)) {
            sock = await openRawSocket(inst, authBox);
            openedTempSocket = true;
            bindContactEvents(inst, sock);
            await waitForSocketOpen(sock, 20000);
        }
        const resolved = await resolveTargetJid(inst, sock, cleanTarget);
        if (!resolved.ok) {
            await authBox.flush(true).catch(() => {});
            return {
                success: false,
                error: resolved.error,
                ambiguous: Boolean(resolved.ambiguous),
                not_found: Boolean(resolved.notFound),
                candidates: resolved.candidates || [],
            };
        }
        const sentObj = await sock.sendMessage(resolved.jid, { text: cleanMsg });
        inst.lastMessageAt = Date.now();
        await authBox.flush(true).catch(() => {});
        return {
            success: true,
            sent: true,
            jid: resolved.jid,
            target_label: resolved.target_label,
            is_group: resolved.is_group,
            message: cleanMsg,
            message_id: String((sentObj && sentObj.key && sentObj.key.id) || ''),
        };
    } catch (_) {
        return { success: false, error: 'Aslan AI WhatsApp mesajı gönderilemedi. Lütfen tekrar deneyin.' };
    } finally {
        if (openedTempSocket && !inst.lifecycleActive) {
            closeSocketQuietly(inst);
        }
    }
}

function mockConnect(userId) {
    if (process.env.WA_MOCK_MODE !== '1') {
        return { success: false, error: 'Devre dışı.' };
    }
    const cleanUid = String(userId || '').trim();
    let inst = instances.get(cleanUid);
    if (!inst) {
        inst = makeBaseInstance(cleanUid, '', {}, {
            status: 'connected',
            connected: true,
            sleepState: 'sleeping',
            connectedAt: Date.now(),
            lastKeepAliveAt: Date.now(),
        });
        instances.set(cleanUid, inst);
    } else {
        inst.status = 'connected';
        inst.connected = true;
        inst.loggedOut = false;
        inst.sleepState = 'sleeping';
        inst.connectedAt = Date.now();
        inst.lastKeepAliveAt = Date.now();
        inst.pairingCode = null;
        inst.updatedAt = Date.now();
    }
    return formatStatusResponse(inst);
}

function mockDisconnect(userId) {
    if (process.env.WA_MOCK_MODE !== '1') {
        return { success: false, error: 'Devre dışı.' };
    }
    const cleanUid = String(userId || '').trim();
    const inst = instances.get(cleanUid);
    if (!inst) {
        return { success: false, error: 'Oturum bulunamadı.' };
    }
    inst.status = 'disconnected';
    inst.connected = false;
    inst.sleepState = 'sleeping';
    inst.updatedAt = Date.now();
    return formatStatusResponse(inst);
}

async function mockIncoming(userId, payload = {}) {
    if (process.env.WA_MOCK_MODE !== '1') {
        return { success: false, error: 'Devre dışı.' };
    }
    const cleanUid = String(userId || '').trim();
    const inst = instances.get(cleanUid);
    if (!inst || !inst.connected) {
        return { success: false, error: 'Bağlı oturum bulunamadı.' };
    }
    inst.sleepState = 'awake';
    inst.lastWakeAt = Date.now();
    const evalRes = await evaluateIncomingMessage(inst, {
        text: payload.text || '',
        isGroup: Boolean(payload.is_group),
        isMentioned: Boolean(payload.is_mentioned),
        msgId: payload.msg_id || '',
    });
    if (evalRes.replied) {
        inst.messagesHandled = (inst.messagesHandled || 0) + 1;
        inst.lastMessageAt = Date.now();
        inst.emptyWakeStreak = 0;
    }
    inst.sleepState = 'sleeping';
    inst.updatedAt = Date.now();
    return Object.assign({
        success: true,
        woken: true,
        sleep_state: inst.sleepState,
        messages_handled: inst.messagesHandled || 0,
    }, evalRes);
}

async function runAutoWakeCycle(options = {}) {
    const cycleStartMs = Date.now();
    const targetUserId = String(options.targetUserId || '').trim();
    const force = Boolean(options.force);
    const handover = Boolean(options.handover);
    const chainToken = String(options.chainToken || '').trim();
    const hop = Number(options.hop || 0);

    let checked = 0;
    let alive = 0;
    let reconnected = 0;

    if (process.env.WA_MOCK_MODE === '1') {
        for (const inst of instances.values()) {
            if (!inst || inst.stopped || inst.loggedOut || inst.status === 'idle') continue;
            checked++;
            if (inst.status === 'disconnected') {
                inst.status = 'connected';
                inst.connected = true;
                inst.sleepState = 'sleeping';
                inst.connectedAt = inst.connectedAt || Date.now();
                inst.lastKeepAliveAt = Date.now();
                inst.lastWakeAt = Date.now();
                inst.reconnectCount = (inst.reconnectCount || 0) + 1;
                reconnected++;
            } else if (inst.connected) {
                inst.lastKeepAliveAt = Date.now();
                inst.lastWakeAt = Date.now();
                alive++;
            }
        }
        return { checked, alive, reconnected, selfWakeScheduled: false };
    }

    const existingLease = await loadWakeLease();
    const leaseDecision = evaluateWakeLeaseDecision(existingLease, { force, handover, chainToken });
    if (!leaseDecision.allow) {
        if (leaseDecision.reason === 'active_relay_healthy') {
            for (const inst of instances.values()) {
                if (inst && !inst.stopped && !inst.loggedOut && isSocketOpen(inst.sock)) {
                    pingSocket(inst.sock);
                    inst.lastKeepAliveAt = Date.now();
                    alive++;
                }
            }
        }
        return { checked: 0, alive, reconnected: 0, skipped: leaseDecision.reason };
    }

    const cycleToken = crypto.randomBytes(8).toString('hex');
    await saveWakeLease({
        activeToken: cycleToken,
        lastWakeAt: cycleStartMs,
        lastHeartbeatAt: cycleStartMs,
        leaseExpiresAt: cycleStartMs + DEFAULT_WAKE_MAX_MS + 25000,
        nextDueAt: 0,
        hop,
        mode: CONTINUOUS_ACTIVE_MODE ? 'continuous' : 'sleep_wake',
    });

    const allSessions = await listSavedActiveSessions();
    const activeSessions = allSessions.filter(isSessionEligibleForActiveRelay);

    if (!activeSessions.length) {
        return { checked: 0, alive: 0, reconnected: 0, sleeping: true, selfWakeScheduled: false };
    }

    const targetSessions = targetUserId
        ? activeSessions.filter((s) => String(s.userId) === targetUserId)
        : activeSessions;

    const jobs = [];
    for (const sess of targetSessions) {
        const uid = String(sess.userId);
        checked++;
        let inst = instances.get(uid);
        if (inst && (inst.lifecycleActive || isSocketOpen(inst.sock))) {
            pingSocket(inst.sock);
            inst.lastKeepAliveAt = Date.now();
            inst.lastWakeAt = Date.now();
            alive++;
            continue;
        }
        inst = makeBaseInstance(uid, sess.phone, sess.config, sess);
        inst.connected = true;
        inst.status = 'reconnecting';
        inst.reconnectCount = (sess.reconnectCount || 0) + 1;
        const authBox = createBlobAuthBox(inst, sess);
        inst.authBox = authBox;
        instances.set(uid, inst);
        reconnected++;
        jobs.push(runSocketLifecycleWindow(inst, null, authBox, {
            mode: 'wake_cycle',
            continuous: CONTINUOUS_ACTIVE_MODE,
            onHeartbeat: () => saveWakeLease({
                activeToken: cycleToken,
                lastWakeAt: cycleStartMs,
                lastHeartbeatAt: Date.now(),
                leaseExpiresAt: cycleStartMs + DEFAULT_WAKE_MAX_MS + 25000,
                nextDueAt: 0,
                hop,
                mode: CONTINUOUS_ACTIVE_MODE ? 'continuous' : 'sleep_wake',
            }).catch(() => {}),
        }));
    }

    let totalHandled = 0;
    let minEmptyStreak = 0;
    if (jobs.length) {
        const settled = await Promise.allSettled(jobs);
        const streaks = [];
        for (const r of settled) {
            if (r.status === 'fulfilled' && r.value) {
                totalHandled += Number(r.value.messagesHandled || 0);
                streaks.push(Number(r.value.emptyWakeStreak || 0));
            }
        }
        if (streaks.length) {
            minEmptyStreak = Math.min(...streaks);
        }
    }

    let selfWakeScheduled = false;
    if (process.env.WA_SELF_WAKE_DISABLED !== '1' && process.env.VERCEL) {
        const remainingCheck = await listSavedActiveSessions();
        const stillHasActive = remainingCheck.some(isSessionEligibleForActiveRelay);
        if (stillHasActive) {
            const nextToken = crypto.randomBytes(8).toString('hex');
            if (CONTINUOUS_ACTIVE_MODE) {
                await saveWakeLease({
                    activeToken: nextToken,
                    lastWakeAt: Date.now(),
                    lastHeartbeatAt: Date.now(),
                    leaseExpiresAt: Date.now() + 35000,
                    nextDueAt: Date.now() + RELAY_HANDOVER_GAP_MS,
                    hop: hop + 1,
                    mode: 'continuous',
                });
                await sleep(RELAY_HANDOVER_GAP_MS);
                const latest = await loadWakeLease();
                if (!latest || !latest.activeToken || latest.activeToken === nextToken) {
                    selfWakeScheduled = await triggerRelayHandover({
                        chain_token: nextToken,
                        handover: true,
                        force: false,
                        hop: hop + 1,
                    });
                }
            } else {
                let desiredSleepMs = totalHandled > 0
                    ? ACTIVE_SLEEP_MS
                    : (minEmptyStreak <= 6 ? IDLE_SLEEP_INTERVAL_MS : DEEP_SLEEP_INTERVAL_MS);
                const elapsed = Date.now() - cycleStartMs;
                const maxSleepBudget = Math.max(2000, 240000 - elapsed);
                const sleepMs = Math.min(desiredSleepMs, maxSleepBudget);
                await saveWakeLease({
                    activeToken: nextToken,
                    lastWakeAt: Date.now(),
                    lastHeartbeatAt: Date.now(),
                    nextDueAt: Date.now() + sleepMs,
                    hop: hop + 1,
                    mode: 'sleep_wake',
                });
                selfWakeScheduled = true;
                await sleep(sleepMs);
                const latest = await loadWakeLease();
                if (!latest || !latest.activeToken || latest.activeToken === nextToken) {
                    selfWakeScheduled = await triggerRelayHandover({
                        chain_token: nextToken,
                        hop: hop + 1,
                    });
                }
            }
        }
    }

    return { checked, alive, reconnected, messagesHandled: totalHandled, selfWakeScheduled };
}

/* ------------------------------------------------------------------ Vercel Node.js 24 Fetch Handler */
function resolveSubRoute(url, bodyAction = '') {
    const qPath = url.searchParams.get('__wa_path');
    let route = qPath ? ('/' + qPath.replace(/^\/+/, '')) : '';
    if (!route) {
        route = url.pathname.replace(/^\/api\/wa_bridge(?:\.mjs)?/i, '') || '/';
    }
    route = route.replace(/\/+$/, '') || '/';
    if (route === '/' && bodyAction) {
        const map = {
            health: '/health',
            ping: '/ping',
            wake: '/wa/wake',
            status: '/wa/status',
            pair: '/wa/pair',
            config: '/wa/config',
            stop: '/wa/stop',
            send: '/wa/send',
            contacts: '/wa/contacts',
            'mock-connect': '/wa/mock-connect',
            'mock-disconnect': '/wa/mock-disconnect',
            'mock-incoming': '/wa/mock-incoming',
            'mock-contacts': '/wa/mock-contacts',
        };
        if (map[bodyAction]) return map[bodyAction];
    }
    return route;
}

export default {
    async fetch(request) {
        try {
            if (!isAuthorizedRequest(request)) {
                return jsonResponse(401, { success: false, error: 'Yetkisiz istek.' });
            }

            const method = String(request.method || 'GET').toUpperCase();
            const url = new URL(request.url || 'https://localhost/');

            if (method === 'GET' || method === 'HEAD') {
                const route = resolveSubRoute(url);
                if (route === '/health') {
                    return jsonResponse(200, { status: 'ok', ipv4: true, ready: true, mode: 'vercel-bridge' });
                }
                if (route === '/ping') {
                    safeWaitUntil(runAutoWakeCycle({ force: false }));
                    if (method === 'HEAD') {
                        return new Response(null, {
                            status: 200,
                            headers: {
                                'Content-Type': 'application/json; charset=utf-8',
                                'Cache-Control': 'no-store, no-cache, must-revalidate, private',
                                'X-Engine': 'Aslan AI',
                            },
                        });
                    }
                    return jsonResponse(200, { ok: true, ts: Math.floor(Date.now() / 1000) });
                }
                if (route === '/wa/wake') {
                    const uid = url.searchParams.get('user_id') || '';
                    safeWaitUntil(runAutoWakeCycle({ targetUserId: uid, force: false }));
                    return jsonResponse(200, { ok: true, awake: true, sleep_wake: true, ts: Math.floor(Date.now() / 1000) });
                }
                if (route === '/wa/status') {
                    const uid = url.searchParams.get('user_id') || '';
                    const res = await getStatus(uid);
                    return jsonResponse(200, res);
                }
                if (route === '/wa/contacts') {
                    const uid = url.searchParams.get('user_id') || '';
                    const contacts = await getContacts(uid);
                    return jsonResponse(200, { success: true, contacts });
                }
                return jsonResponse(404, { success: false, error: 'Bilinmeyen yol.' });
            }

            if (method === 'POST') {
                let body = {};
                try {
                    const rawText = await request.text();
                    if (rawText && rawText.trim()) {
                        const parsed = JSON.parse(rawText);
                        if (parsed && typeof parsed === 'object') body = parsed;
                    }
                } catch (_) {
                    return jsonResponse(400, { success: false, error: 'Geçersiz istek gövdesi.' });
                }

                const route = resolveSubRoute(url, String(body.action || '').trim().toLowerCase());
                if (route === '/ping') {
                    safeWaitUntil(runAutoWakeCycle({ force: false }));
                    return jsonResponse(200, { ok: true, ts: Math.floor(Date.now() / 1000) });
                }
                if (route === '/wa/wake') {
                    const force = body.force !== undefined ? Boolean(body.force) : !body.chain_token;
                    safeWaitUntil(runAutoWakeCycle({
                        targetUserId: body.user_id || '',
                        force,
                        handover: Boolean(body.handover),
                        chainToken: String(body.chain_token || ''),
                        hop: Number(body.hop || 0),
                    }));
                    return jsonResponse(200, { ok: true, awake: true, sleep_wake: true, ts: Math.floor(Date.now() / 1000) });
                }
                if (route === '/wa/status') {
                    const uid = body.user_id || url.searchParams.get('user_id') || '';
                    return jsonResponse(200, await getStatus(uid));
                }
                if (route === '/wa/pair') {
                    const res = await startPairing(body.user_id, body.phone, body.config || {});
                    return jsonResponse(res.success ? 200 : 400, res);
                }
                if (route === '/wa/config') {
                    const res = await updateConfig(body.user_id, body.config || {});
                    return jsonResponse(res.success ? 200 : 400, res);
                }
                if (route === '/wa/stop') {
                    const res = await stopBot(body.user_id, Boolean(body.logout));
                    return jsonResponse(200, res);
                }
                if (route === '/wa/send') {
                    const res = await sendDirectMessage(body.user_id, body.target, body.message);
                    return jsonResponse(res.success ? 200 : 400, res);
                }
                if (route === '/wa/contacts') {
                    const uid = body.user_id || url.searchParams.get('user_id') || '';
                    const contacts = await getContacts(uid);
                    return jsonResponse(200, { success: true, contacts });
                }
                if (route === '/wa/mock-connect' && process.env.WA_MOCK_MODE === '1') {
                    const res = mockConnect(body.user_id);
                    return jsonResponse(res.success ? 200 : 400, res);
                }
                if (route === '/wa/mock-disconnect' && process.env.WA_MOCK_MODE === '1') {
                    const res = mockDisconnect(body.user_id);
                    return jsonResponse(res.success ? 200 : 400, res);
                }
                if (route === '/wa/mock-incoming' && process.env.WA_MOCK_MODE === '1') {
                    const res = await mockIncoming(body.user_id, body);
                    return jsonResponse(res.success ? 200 : 400, res);
                }
                if (route === '/wa/mock-contacts' && process.env.WA_MOCK_MODE === '1') {
                    const res = setMockContacts(body.user_id, body.contacts);
                    return jsonResponse(res.success ? 200 : 400, res);
                }
                return jsonResponse(404, { success: false, error: 'Bilinmeyen yol.' });
            }

            return jsonResponse(405, { success: false, error: 'Desteklenmeyen HTTP yöntemi.' });
        } catch (_) {
            return jsonResponse(500, {
                success: false,
                error: 'Aslan AI WhatsApp servisi geçici olarak yanıt veremedi.',
            });
        }
    },
};
