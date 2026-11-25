import crypto from 'node:crypto';
import dns from 'node:dns';
import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
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
const DEFAULT_PAIR_WINDOW_MS = Math.max(15000, Number(process.env.WA_PAIR_WINDOW_MS || 180000));
const DEFAULT_PULSE_WINDOW_MS = Math.max(15000, Number(process.env.WA_PULSE_WINDOW_MS || 120000));
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

function ipv4Request(urlStr, options = {}) {
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
            const chunks = [];
            res.on('data', (c) => chunks.push(c));
            res.on('end', () => {
                const buffer = Buffer.concat(chunks);
                const text = buffer.toString('utf-8');
                let json = null;
                try {
                    json = JSON.parse(text);
                } catch (_) {}
                resolve({ status: res.statusCode || 200, headers: res.headers, buffer, text, json });
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

/* ------------------------------------------------------------------ Encrypted Vercel Blob Session Store */
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

async function loadSavedSession(userId) {
    if (!userId || process.env.WA_MOCK_MODE === '1') return null;
    const localSess = loadUserSessionFromTmp(userId);
    const blobSess = blobEnabled() ? await loadUserSessionFromBlob(userId) : null;
    if (localSess && blobSess) {
        return Number(blobSess.updatedAt || 0) >= Number(localSess.updatedAt || 0) ? blobSess : localSess;
    }
    return blobSess || localSess || null;
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

/* ------------------------------------------------------------------ Config & Bot Message Runtime */
function normalizePhoneDigits(rawPhone) {
    return String(rawPhone || '').replace(/\D/g, '');
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

async function callCustomAi(aiCfg, userText, cfg) {
    const apiKey = String(aiCfg.api_key || '').trim();
    if (!apiKey) return '';
    const provider = String(aiCfg.provider || 'openai').toLowerCase();
    const sysPrompt = [
        'Sen kullanicinin WhatsApp asistanisin.',
        cfg.tone ? `Uslup: ${cfg.tone}.` : '',
        cfg.instructions ? `Kurallar: ${cfg.instructions}` : '',
        'Kisa, dogal ve yardimsever yanit ver.',
    ].filter(Boolean).join(' ');

    try {
        if (provider === 'gemini') {
            const model = aiCfg.model || 'gemini-2.5-flash';
            const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;
            const res = await ipv4PostJson(url, {
                contents: [{ role: 'user', parts: [{ text: userText }] }],
                systemInstruction: { parts: [{ text: sysPrompt }] },
                generationConfig: { temperature: 0.7, maxOutputTokens: 600 },
            }, { 'x-goog-api-key': apiKey }, 25000);
            const cand = res.data && res.data.candidates && res.data.candidates[0];
            const parts = cand && cand.content && cand.content.parts;
            if (Array.isArray(parts)) {
                return parts.map((p) => p.text || '').join('').trim();
            }
            return '';
        }

        if (provider === 'anthropic') {
            const res = await ipv4PostJson('https://api.anthropic.com/v1/messages', {
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

        let url = aiCfg.endpoint || 'https://api.openai.com/v1/chat/completions';
        let model = aiCfg.model || 'gpt-4o-mini';
        if (provider === 'groq') {
            url = aiCfg.endpoint || 'https://api.groq.com/openai/v1/chat/completions';
            model = aiCfg.model || 'llama-3.3-70b-versatile';
        } else if (provider === 'openrouter') {
            url = aiCfg.endpoint || 'https://openrouter.ai/api/v1/chat/completions';
            model = aiCfg.model || 'openai/gpt-4o-mini';
        }

        const res = await ipv4PostJson(url, {
            model,
            messages: [
                { role: 'system', content: sysPrompt },
                { role: 'user', content: userText },
            ],
            temperature: 0.7,
            max_tokens: 600,
        }, { Authorization: `Bearer ${apiKey}` }, 25000);

        const choices = res.data && res.data.choices;
        if (Array.isArray(choices) && choices[0] && choices[0].message && choices[0].message.content) {
            return String(choices[0].message.content).trim();
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

async function handleIncomingMessage(instance, msg) {
    const cfg = instance.config;
    if (!cfg || !cfg.enabled) return;
    if (!msg || !msg.key || msg.key.fromMe) return;

    const remoteJid = String(msg.key.remoteJid || '');
    if (!remoteJid || remoteJid === 'status@broadcast') return;

    const isGroup = remoteJid.endsWith('@g.us');
    if (isGroup && !cfg.allow_groups) return;
    if (!isGroup && !cfg.allow_private) return;

    const text = extractMessageText(msg);
    if (!text) return;
    const textLower = text.toLowerCase();

    if (matchesKeywordList(textLower, cfg.ignored_keywords)) {
        return;
    }

    const mentionedJids = (
        msg.message &&
        msg.message.extendedTextMessage &&
        msg.message.extendedTextMessage.contextInfo &&
        msg.message.extendedTextMessage.contextInfo.mentionedJid
    ) || [];
    const selfRawId = String((instance.sock && instance.sock.user && instance.sock.user.id) || '').split(':')[0];
    const selfJid = selfRawId ? `${selfRawId}@s.whatsapp.net` : '';
    const isMentioned = Boolean(selfJid && Array.isArray(mentionedJids) && mentionedJids.includes(selfJid));

    const ruleReply = matchRuleReply(textLower, cfg.rules);
    const hasTriggerMatch = cfg.trigger_keywords.length === 0 || matchesKeywordList(textLower, cfg.trigger_keywords);

    if (isGroup && cfg.group_mode === 'mentions_or_triggers') {
        if (!isMentioned && !ruleReply && !(cfg.trigger_keywords.length > 0 && hasTriggerMatch)) {
            return;
        }
    }

    const emoji = findAutoReaction(textLower, cfg.auto_reactions);
    if (emoji && instance.sock) {
        try {
            await instance.sock.sendMessage(remoteJid, { react: { text: emoji, key: msg.key } });
        } catch (_) {}
    }

    if (!ruleReply && cfg.trigger_keywords.length > 0 && !hasTriggerMatch) {
        return;
    }

    let replyText = ruleReply;
    if (!replyText && cfg.custom_ai && cfg.custom_ai.enabled && cfg.custom_ai.api_key) {
        replyText = await callCustomAi(cfg.custom_ai, text, cfg);
    }
    if (!replyText && cfg.default_reply) {
        replyText = cfg.default_reply;
    }
    if (!replyText || !instance.sock) return;

    try {
        if (cfg.show_typing) {
            await instance.sock.sendPresenceUpdate('composing', remoteJid);
        }
        const delayMs = computeHumanDelayMs(cfg.typing_delay_seconds);
        if (delayMs > 0) {
            await sleep(delayMs);
        }
        if (cfg.show_typing) {
            await instance.sock.sendPresenceUpdate('paused', remoteJid);
        }
        await instance.sock.sendMessage(remoteJid, { text: replyText }, { quoted: msg });
    } catch (_) {}
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
            if (sock && sock.ws && sock.ws.isOpen) {
                finishResolve();
            } else {
                finishReject(new Error('Bağlantı zaman aşımı'));
            }
        }, timeoutMs);
        sock.ev.on('connection.update', onUpdate);
        wsCheckTimer = setTimeout(() => {
            if (!settled && sock && sock.ws && sock.ws.isOpen) {
                finishResolve();
            }
        }, 5500);
    });
}

function runSocketLifecycleWindow(instance, initialSock, authBox, windowMs) {
    instance.lifecycleActive = true;
    return new Promise((resolve) => {
        let finished = false;
        let windowClosing = false;
        let windowTimer = null;

        const finishWindow = async () => {
            if (finished) return;
            finished = true;
            windowClosing = true;
            if (windowTimer) clearTimeout(windowTimer);
            try {
                await authBox.flush(true);
            } catch (_) {}
            closeSocketQuietly(instance);
            instance.lifecycleActive = false;
            resolve();
        };

        const bindSocket = (sock) => {
            if (!sock || finished || instance.stopped) return;
            sock.ev.on('creds.update', () => {
                authBox.flush(true).catch(() => {});
            });
            sock.ev.on('messages.upsert', (ev) => {
                if (!ev || ev.type !== 'notify' || !Array.isArray(ev.messages)) return;
                for (const m of ev.messages) {
                    handleIncomingMessage(instance, m).catch(() => {});
                }
            });
            sock.ev.on('connection.update', (update) => {
                if (!update || finished || windowClosing || instance.stopped) return;
                const { connection, lastDisconnect } = update;
                if (connection === 'open') {
                    instance.status = 'connected';
                    instance.connected = true;
                    instance.loggedOut = false;
                    instance.reconnecting = false;
                    instance.connectedAt = instance.connectedAt || Date.now();
                    instance.lastKeepAliveAt = Date.now();
                    instance.pairingCode = null;
                    instance.updatedAt = Date.now();
                    authBox.flush(true).catch(() => {});
                } else if (connection === 'close') {
                    const code = lastDisconnect && lastDisconnect.error
                        ? new Boom(lastDisconnect.error).output?.statusCode
                        : 0;
                    const loggedOut = code === DisconnectReason.loggedOut;
                    if (loggedOut) {
                        instance.connected = false;
                        instance.loggedOut = true;
                        instance.status = 'disconnected';
                        instance.updatedAt = Date.now();
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
                        instance.reconnecting = true;
                        authBox.flush(true).catch(() => {});
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
                                });
                        }, delayMs);
                    }
                }
            });
        };

        windowTimer = setTimeout(() => {
            finishWindow();
        }, windowMs);

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
        reconnectCount: base.reconnectCount || 0,
        pairingCode: base.pairingCode || null,
        expiresAt: base.expiresAt || null,
        sock: null,
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
            safeWaitUntil(runSocketLifecycleWindow(instance, sock, authBox, DEFAULT_PULSE_WINDOW_MS));
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
        safeWaitUntil(runSocketLifecycleWindow(instance, sock, authBox, DEFAULT_PAIR_WINDOW_MS));
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
                pairingCode: remote.pairingCode ?? warm.pairingCode,
                expiresAt: remote.expiresAt ?? warm.expiresAt,
                reconnectCount: Math.max(warm.reconnectCount || 0, remote.reconnectCount || 0),
                configUpdatesCount: Math.max(warm.configUpdatesCount || 0, remote.configUpdatesCount || 0),
                config: remTs > warmTs && remote.config ? defaultConfig(remote.config) : warm.config,
                updatedAt: Math.max(warmTs, remTs),
            }
            : warm;
        instances.set(cleanUid, chosen);
        return formatStatusResponse(chosen);
    }

    if (warm) {
        return formatStatusResponse(warm);
    }

    const hydrated = makeBaseInstance(cleanUid, remote.phone, remote.config, remote);
    instances.set(cleanUid, hydrated);
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
        const sessionData = remote || (inst.authBox ? { creds: inst.authBox.creds, keys: inst.authBox.keyStore } : null);
        if (sessionData && sessionData.creds && sessionData.creds.registered) {
            const box = createBlobAuthBox(inst, sessionData);
            inst.authBox = box;
            safeWaitUntil(runSocketLifecycleWindow(inst, null, box, DEFAULT_PULSE_WINDOW_MS));
        }
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

function mockConnect(userId) {
    if (process.env.WA_MOCK_MODE !== '1') {
        return { success: false, error: 'Devre dışı.' };
    }
    const cleanUid = String(userId || '').trim();
    let inst = instances.get(cleanUid);
    if (!inst) {
        inst = makeBaseInstance(cleanUid, '', {}, { status: 'connected', connected: true, connectedAt: Date.now(), lastKeepAliveAt: Date.now() });
        instances.set(cleanUid, inst);
    } else {
        inst.status = 'connected';
        inst.connected = true;
        inst.loggedOut = false;
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
    inst.updatedAt = Date.now();
    return formatStatusResponse(inst);
}

async function pulseActiveBlobBots() {
    let checked = 0;
    let alive = 0;
    let reconnected = 0;

    for (const inst of instances.values()) {
        if (!inst || inst.stopped || inst.loggedOut || inst.status === 'idle') continue;
        checked++;
        if (process.env.WA_MOCK_MODE === '1') {
            if (inst.status === 'disconnected') {
                inst.status = 'connected';
                inst.connected = true;
                inst.connectedAt = Date.now();
                inst.lastKeepAliveAt = Date.now();
                inst.reconnectCount = (inst.reconnectCount || 0) + 1;
                reconnected++;
            } else if (inst.connected) {
                inst.lastKeepAliveAt = Date.now();
                alive++;
            }
            continue;
        }
        const ws = inst.sock && inst.sock.ws;
        if (ws && ws.readyState === 1) {
            try {
                if (typeof ws.ping === 'function') ws.ping();
            } catch (_) {}
            inst.lastKeepAliveAt = Date.now();
            alive++;
        }
    }

    if (process.env.WA_MOCK_MODE === '1' || !blobEnabled()) {
        return { checked, alive, reconnected };
    }

    const sessions = await listActiveBlobSessions();
    const jobs = [];
    for (const sess of sessions) {
        if (!sess || !sess.userId || sess.loggedOut || sess.stopped) continue;
        if (!sess.connected || (sess.config && sess.config.enabled === false)) continue;
        if (!sess.creds || !sess.creds.registered) continue;

        const uid = String(sess.userId);
        let inst = instances.get(uid);
        if (inst && (inst.lifecycleActive || (inst.sock && inst.sock.ws && inst.sock.ws.readyState === 1))) {
            continue;
        }
        inst = makeBaseInstance(uid, sess.phone, sess.config, sess);
        inst.reconnectCount = (sess.reconnectCount || 0) + 1;
        const authBox = createBlobAuthBox(inst, sess);
        inst.authBox = authBox;
        instances.set(uid, inst);
        reconnected++;
        jobs.push(runSocketLifecycleWindow(inst, null, authBox, DEFAULT_PULSE_WINDOW_MS));
    }

    if (jobs.length) {
        await Promise.allSettled(jobs);
    }
    return { checked, alive, reconnected };
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
            status: '/wa/status',
            pair: '/wa/pair',
            config: '/wa/config',
            stop: '/wa/stop',
            'mock-connect': '/wa/mock-connect',
            'mock-disconnect': '/wa/mock-disconnect',
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
                    safeWaitUntil(pulseActiveBlobBots());
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
                if (route === '/wa/status') {
                    const uid = url.searchParams.get('user_id') || '';
                    const res = await getStatus(uid);
                    return jsonResponse(200, res);
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
                    safeWaitUntil(pulseActiveBlobBots());
                    return jsonResponse(200, { ok: true, ts: Math.floor(Date.now() / 1000) });
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
                if (route === '/wa/mock-connect' && process.env.WA_MOCK_MODE === '1') {
                    const res = mockConnect(body.user_id);
                    return jsonResponse(res.success ? 200 : 400, res);
                }
                if (route === '/wa/mock-disconnect' && process.env.WA_MOCK_MODE === '1') {
                    const res = mockDisconnect(body.user_id);
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
