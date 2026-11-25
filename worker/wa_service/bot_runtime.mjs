import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, URL } from 'node:url';
import makeWASocketImport, {
    Browsers,
    DisconnectReason,
    fetchLatestBaileysVersion,
    useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import { ipv4Agent, ipv4PostJson } from './ipv4.mjs';

const makeWASocket = typeof makeWASocketImport === 'function'
    ? makeWASocketImport
    : (makeWASocketImport && makeWASocketImport.default);

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const _defaultAuthDir = process.env.VERCEL
    ? path.join(os.tmpdir(), 'aslan_wa_sessions')
    : path.join(__dirname, 'auth_sessions');
const AUTH_BASE_DIR = process.env.WA_AUTH_DIR || _defaultAuthDir;
const FALLBACK_WA_VERSION = [2, 3000, 1043857760];

const instances = new Map();
let cachedWaVersion = { version: FALLBACK_WA_VERSION, fetchedAt: 0 };

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

function ensureDir(dirPath) {
    fs.mkdirSync(dirPath, { recursive: true });
}

function userSessionDir(userId) {
    const tag = crypto.createHash('sha256').update(String(userId || '')).digest('hex').slice(0, 24);
    return path.join(AUTH_BASE_DIR, tag);
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function persistInstanceMeta(instance) {
    if (!instance || !instance.userId || process.env.WA_MOCK_MODE === '1') return;
    try {
        const dir = userSessionDir(instance.userId);
        ensureDir(dir);
        const metaPath = path.join(dir, 'aslan_meta.json');
        fs.writeFileSync(metaPath, JSON.stringify({
            userId: instance.userId,
            phone: instance.phone || '',
            contacts: serializeContacts(instance),
            config: defaultConfig(instance.config),
            updatedAt: Date.now(),
        }), { encoding: 'utf-8', mode: 0o600 });
    } catch (_) {}
}

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

export function sanitizeConfigSummary(rawCfg) {
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

function matchRuleReply(text, textLower, rules) {
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
    for (const item of (Array.isArray(raw) ? raw : [])) {
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
    if (!instance || !(instance.contacts instanceof Map)) return [];
    return Array.from(instance.contacts.values())
        .sort((a, b) => Number(b.updatedAt || 0) - Number(a.updatedAt || 0))
        .slice(0, 500);
}

function upsertContactEntry(instance, entry) {
    if (!instance) return;
    if (!(instance.contacts instanceof Map)) instance.contacts = new Map();
    if (!entry || typeof entry !== 'object') return;
    const jid = String(entry.jid || entry.id || '').trim();
    if (!jid || jid === 'status@broadcast') return;
    const prev = instance.contacts.get(jid) || {};
    instance.contacts.set(jid, {
        jid,
        name: String(entry.name || entry.subject || prev.name || '').trim(),
        notify: String(entry.notify || prev.notify || '').trim(),
        isGroup: Boolean(entry.isGroup || entry.is_group || jid.endsWith('@g.us')),
        updatedAt: Date.now(),
    });
    if (instance.contacts.size > 500) {
        instance.contacts = hydrateContacts(serializeContacts(instance));
    }
}

async function syncParticipatingGroups(instance, sock) {
    if (!sock || typeof sock.groupFetchAllParticipating !== 'function') return;
    try {
        const groups = await sock.groupFetchAllParticipating();
        if (groups && typeof groups === 'object') {
            for (const [jid, meta] of Object.entries(groups)) {
                if (!jid) continue;
                upsertContactEntry(instance, { jid, name: (meta && meta.subject) || '', isGroup: true });
            }
        }
    } catch (_) {}
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
        return { ok: true, jid: clean, target_label: (hit && (hit.name || hit.notify)) || clean, is_group: clean.endsWith('@g.us') };
    }
    const digits = normalizePhoneTarget(clean);
    if (digits) {
        let jid = `${digits}@s.whatsapp.net`;
        if (sock && typeof sock.onWhatsApp === 'function') {
            try {
                const res = await sock.onWhatsApp(digits);
                if (Array.isArray(res) && res[0]) {
                    if (res[0].exists === false) return { ok: false, notFound: true, error: 'Bu numara WhatsApp üzerinde kayıtlı görünmüyor.' };
                    if (res[0].jid) jid = res[0].jid;
                }
            } catch (_) {}
        }
        const hit = instance && instance.contacts ? instance.contacts.get(jid) : null;
        return { ok: true, jid, target_label: (hit && (hit.name || hit.notify)) || `+${digits}`, is_group: false };
    }
    if (sock) await syncParticipatingGroups(instance, sock);
    const queryFold = normalizeTurkishFold(clean);
    const entries = instance && instance.contacts instanceof Map ? Array.from(instance.contacts.values()) : [];
    const exact = [], pref = [], sub = [];
    for (const c of entries) {
        const names = [normalizeTurkishFold(c.name || ''), normalizeTurkishFold(c.notify || '')].filter(Boolean);
        if (!names.length) continue;
        if (names.some((n) => n === queryFold)) exact.push(c);
        else if (names.some((n) => n.startsWith(queryFold))) pref.push(c);
        else if (names.some((n) => n.includes(queryFold))) sub.push(c);
    }
    const bucket = exact.length ? exact : (pref.length ? pref : sub);
    if (!bucket.length) {
        return { ok: false, notFound: true, error: `"${clean}" adıyla eşleşen kişi veya grup bulunamadı.` };
    }
    if (bucket.length > 1) {
        return {
            ok: false,
            ambiguous: true,
            candidates: bucket.slice(0, 6).map((c) => ({ jid: c.jid, name: c.name || c.notify || c.jid, is_group: Boolean(c.isGroup || String(c.jid).endsWith('@g.us')) })),
            error: `"${clean}" için birden fazla eşleşme bulundu. Lütfen tam adını veya numarasını belirtin.`,
        };
    }
    const chosen = bucket[0];
    return { ok: true, jid: chosen.jid, target_label: chosen.name || chosen.notify || chosen.jid, is_group: Boolean(chosen.isGroup || String(chosen.jid).endsWith('@g.us')) };
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
            const ep = aiCfg.endpoint && isSafeCustomHttpsUrl(aiCfg.endpoint) ? aiCfg.endpoint : 'https://api.anthropic.com/v1/messages';
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

    const ruleReply = matchRuleReply(text, textLower, cfg.rules);
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
    if (!replyText && cfg.custom_ai && cfg.custom_ai.enabled && (cfg.custom_ai.api_key || (cfg.custom_ai.provider === 'custom' && cfg.custom_ai.endpoint))) {
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

function closeSocketOnly(instance) {
    if (!instance) return;
    if (instance.heartbeatTimer) {
        clearInterval(instance.heartbeatTimer);
        instance.heartbeatTimer = null;
    }
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

function scheduleWorkerReconnect(instance, delayMs = 3000) {
    if (!instance || instance.stopped || instance.loggedOut) return;
    if (instance.reconnectTimer || instance.reconnecting) return;
    const waitMs = Math.max(400, Number(delayMs) || 3000);
    instance.reconnectTimer = setTimeout(() => {
        instance.reconnectTimer = null;
        if (instance.stopped || instance.loggedOut) return;
        instance.reconnecting = true;
        instance.reconnectAttempts = (instance.reconnectAttempts || 0) + 1;
        instance.reconnectCount = (instance.reconnectCount || 0) + 1;
        openLiveSocket(instance)
            .catch(() => {
                const nextBackoff = Math.min(30000, 1500 * (2 ** Math.min(instance.reconnectAttempts || 1, 4)));
                scheduleWorkerReconnect(instance, nextBackoff);
            })
            .finally(() => { instance.reconnecting = false; });
    }, waitMs);
}

async function openLiveSocket(instance) {
    if (instance.heartbeatTimer) {
        clearInterval(instance.heartbeatTimer);
        instance.heartbeatTimer = null;
    }
    const sessionDir = userSessionDir(instance.userId);
    ensureDir(sessionDir);
    const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
    const version = await getLatestWaVersion();

    const sock = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: false,
        browser: Browsers.ubuntu('Chrome'),
        logger: pino({ level: 'silent' }),
        agent: ipv4Agent,
        fetchAgent: ipv4Agent,
        markOnlineOnConnect: false,
        syncFullHistory: false,
        connectTimeoutMs: 30000,
    });

    instance.sock = sock;

    // 7/24 aktiflik icin 25 saniyelik WebSocket nabiz & kopma denetimi
    instance.heartbeatTimer = setInterval(() => {
        if (!instance || instance.stopped || instance.loggedOut) {
            if (instance && instance.heartbeatTimer) {
                clearInterval(instance.heartbeatTimer);
                instance.heartbeatTimer = null;
            }
            return;
        }
        if (isSocketOpen(instance.sock)) {
            pingSocket(instance.sock);
            instance.lastKeepAliveAt = Date.now();
        } else if (
            !instance.reconnectTimer &&
            !instance.reconnecting &&
            (instance.connected || instance.status === 'reconnecting' || instance.status === 'disconnected' || (state && state.creds && state.creds.registered))
        ) {
            scheduleWorkerReconnect(instance, 1500);
        }
    }, 25000);
    if (typeof instance.heartbeatTimer.unref === 'function') {
        instance.heartbeatTimer.unref();
    }

    sock.ev.on('creds.update', saveCreds);
    sock.ev.on('contacts.upsert', (list) => {
        for (const c of (list || [])) upsertContactEntry(instance, { jid: c.id || c.jid, name: c.name || c.verifiedName || '', notify: c.notify || '', isGroup: false });
        persistInstanceMeta(instance);
    });
    sock.ev.on('chats.upsert', (list) => {
        for (const c of (list || [])) {
            const jid = c.id || '';
            upsertContactEntry(instance, { jid, name: c.name || c.subject || '', isGroup: jid.endsWith('@g.us') });
        }
        persistInstanceMeta(instance);
    });
    sock.ev.on('groups.upsert', (list) => {
        for (const g of (list || [])) upsertContactEntry(instance, { jid: g.id, name: g.subject || '', isGroup: true });
        persistInstanceMeta(instance);
    });

    sock.ev.on('connection.update', (update) => {
        if (instance.stopped) return;
        const { connection, lastDisconnect } = update || {};
        if (connection === 'open') {
            instance.status = 'connected';
            instance.connected = true;
            instance.loggedOut = false;
            instance.reconnecting = false;
            instance.reconnectAttempts = 0;
            instance.connectedAt = Date.now();
            instance.lastKeepAliveAt = Date.now();
            instance.pairingCode = null;
            syncParticipatingGroups(instance, sock).finally(() => persistInstanceMeta(instance));
        } else if (connection === 'close') {
            const code = lastDisconnect && lastDisconnect.error
                ? new Boom(lastDisconnect.error).output?.statusCode
                : 0;
            const loggedOut = code === DisconnectReason.loggedOut;
            instance.connected = false;
            instance.loggedOut = Boolean(loggedOut);
            if (loggedOut || instance.stopped) {
                instance.status = 'disconnected';
                closeSocketOnly(instance);
            } else {
                instance.status = instance.pairingCode ? 'pairing' : 'reconnecting';
                const attempts = instance.reconnectAttempts || 0;
                const backoffMs = code === DisconnectReason.restartRequired
                    ? 500
                    : (code === DisconnectReason.connectionReplaced
                        ? 10000
                        : Math.min(30000, 1000 * (2 ** Math.min(attempts, 5))));
                scheduleWorkerReconnect(instance, backoffMs);
            }
        }
    });

    sock.ev.on('messages.upsert', (ev) => {
        if (!ev || ev.type !== 'notify' || !Array.isArray(ev.messages)) return;
        for (const m of ev.messages) {
            if (m && m.key && m.key.remoteJid && m.pushName && !m.key.fromMe) {
                upsertContactEntry(instance, {
                    jid: m.key.remoteJid,
                    notify: m.pushName,
                    isGroup: String(m.key.remoteJid).endsWith('@g.us'),
                });
            }
            handleIncomingMessage(instance, m).catch(() => {});
        }
    });

    return sock;
}

export async function startPairing(userId, phone, configInput = {}) {
    const cleanUid = String(userId || '').trim();
    const digits = normalizePhoneDigits(phone);
    if (!cleanUid) {
        return { success: false, error: 'Kullanıcı kimliği gerekli.' };
    }
    if (digits.length < 10 || digits.length > 15) {
        return { success: false, error: 'Geçersiz telefon numarası.' };
    }

    const existing = instances.get(cleanUid);
    if (existing) {
        existing.stopped = true;
        closeSocketOnly(existing);
    }

    const sessionDir = userSessionDir(cleanUid);
    try {
        fs.rmSync(sessionDir, { recursive: true, force: true });
    } catch (_) {}

    const instance = {
        userId: cleanUid,
        phone: digits,
        status: 'pairing',
        connected: false,
        loggedOut: false,
        reconnecting: false,
        connectedAt: null,
        lastKeepAliveAt: 0,
        reconnectCount: 0,
        pairingCode: null,
        expiresAt: Date.now() + 60000,
        sock: null,
        stopped: false,
        reconnectTimer: null,
        configUpdatesCount: 0,
        config: defaultConfig(configInput),
    };
    instances.set(cleanUid, instance);

    if (process.env.WA_MOCK_MODE === '1') {
        const digest = crypto.createHash('sha256').update(`${cleanUid}:${digits}:${Date.now()}`).digest('hex').toUpperCase();
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

    try {
        const sock = await openLiveSocket(instance);
        await waitForPairingReady(sock, 15000);
        await sleep(300);
        if (sock.authState && sock.authState.creds && sock.authState.creds.registered) {
            instance.status = 'connected';
            instance.connected = true;
            instance.connectedAt = Date.now();
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
        return {
            success: true,
            pairing_code: cleanCode,
            formatted_code: cleanCode.length === 8 ? `${cleanCode.slice(0, 4)}-${cleanCode.slice(4)}` : cleanCode,
            status: 'pairing',
            expires_in: 60,
        };
    } catch (_) {
        instance.stopped = true;
        closeSocketOnly(instance);
        instance.status = 'error';
        return {
            success: false,
            error: 'Aslan AI WhatsApp eşleştirme kodu şu an oluşturulamadı. Lütfen tekrar deneyin.',
        };
    }
}

export function updateConfig(userId, newConfig) {
    const cleanUid = String(userId || '').trim();
    if (!cleanUid) {
        return { success: false, error: 'Kullanıcı kimliği gerekli.' };
    }
    const merged = defaultConfig(newConfig);
    let inst = instances.get(cleanUid);
    if (!inst) {
        inst = {
            userId: cleanUid,
            phone: '',
            status: 'configured',
            connected: false,
            loggedOut: false,
            reconnecting: false,
            connectedAt: null,
            lastKeepAliveAt: 0,
            reconnectCount: 0,
            pairingCode: null,
            expiresAt: null,
            sock: null,
            stopped: false,
            reconnectTimer: null,
            configUpdatesCount: 1,
            config: merged,
        };
        instances.set(cleanUid, inst);
    } else {
        inst.config = merged;
        inst.configUpdatesCount = (inst.configUpdatesCount || 0) + 1;
    }
    persistInstanceMeta(inst);
    return {
        success: true,
        config_updates_count: inst.configUpdatesCount,
        config: sanitizeConfigSummary(inst.config),
    };
}

export function getStatus(userId) {
    const cleanUid = String(userId || '').trim();
    const inst = instances.get(cleanUid);
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
        status: inst.status,
        connected: Boolean(inst.connected),
        connected_at: inst.connectedAt,
        pairing_code_active: pairActive,
        enabled: Boolean(inst.config && inst.config.enabled),
        config_updates_count: inst.configUpdatesCount || 0,
        reconnect_count: inst.reconnectCount || 0,
        config_summary: sanitizeConfigSummary(inst.config),
    };
}

export async function stopBot(userId, logout = false) {
    const cleanUid = String(userId || '').trim();
    const inst = instances.get(cleanUid);
    if (!inst) {
        return { success: true, status: 'idle', stopped: true };
    }
    inst.stopped = true;
    inst.connected = false;
    inst.status = 'stopped';
    if (inst.config) inst.config.enabled = false;
    if (logout && inst.sock && typeof inst.sock.logout === 'function') {
        try { await inst.sock.logout(); } catch (_) {}
    }
    closeSocketOnly(inst);
    if (logout) {
        try { fs.rmSync(userSessionDir(cleanUid), { recursive: true, force: true }); } catch (_) {}
        instances.delete(cleanUid);
    }
    return { success: true, status: inst.status, stopped: true };
}

export function getContacts(userId) {
    const cleanUid = String(userId || '').trim();
    const inst = instances.get(cleanUid);
    if (!inst || !(inst.contacts instanceof Map)) return [];
    return serializeContacts(inst).map((c) => ({
        jid: c.jid,
        name: c.name || c.notify || c.jid,
        notify: c.notify || '',
        is_group: Boolean(c.isGroup || String(c.jid).endsWith('@g.us')),
    }));
}

export function setMockContacts(userId, contactsList = []) {
    if (process.env.WA_MOCK_MODE !== '1') return { success: false, error: 'Devre dışı.' };
    const cleanUid = String(userId || '').trim();
    const inst = instances.get(cleanUid);
    if (!inst) return { success: false, error: 'Oturum bulunamadı.' };
    for (const c of (Array.isArray(contactsList) ? contactsList : [])) {
        upsertContactEntry(inst, c);
    }
    return { success: true, count: inst.contacts.size };
}

export async function sendDirectMessage(userId, target, message) {
    const cleanUid = String(userId || '').trim();
    const cleanTarget = String(target || '').trim();
    const cleanMsg = String(message || '').trim();
    if (!cleanUid) return { success: false, error: 'Kullanıcı kimliği gerekli.' };
    if (!cleanTarget) return { success: false, error: 'Hedef kişi, grup veya numara belirtilmelidir.' };
    if (!cleanMsg) return { success: false, error: 'Mesaj metni boş olamaz.' };

    const inst = instances.get(cleanUid);
    if (!inst || !inst.connected || inst.loggedOut || inst.stopped) {
        return { success: false, error: 'Bağlı WhatsApp oturumu bulunamadı.' };
    }
    const resolved = await resolveTargetJid(inst, inst.sock, cleanTarget);
    if (!resolved.ok) {
        return {
            success: false,
            error: resolved.error,
            ambiguous: Boolean(resolved.ambiguous),
            not_found: Boolean(resolved.notFound),
            candidates: resolved.candidates || [],
        };
    }

    if (process.env.WA_MOCK_MODE === '1') {
        const msgId = 'mock_' + crypto.randomBytes(6).toString('hex');
        if (!Array.isArray(inst.sentMessages)) inst.sentMessages = [];
        const sentRec = {
            jid: resolved.jid,
            target_label: resolved.target_label,
            is_group: resolved.is_group,
            message: cleanMsg,
            message_id: msgId,
            sentAt: Date.now(),
        };
        inst.sentMessages.push(sentRec);
        inst.lastMessageAt = Date.now();
        return Object.assign({ success: true, sent: true }, sentRec);
    }

    if (!isSocketOpen(inst.sock)) {
        return { success: false, error: 'WhatsApp soketi şu an açık değil, yeniden bağlanıyor.' };
    }
    try {
        const sentObj = await inst.sock.sendMessage(resolved.jid, { text: cleanMsg });
        inst.lastMessageAt = Date.now();
        persistInstanceMeta(inst);
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
        return { success: false, error: 'WhatsApp mesajı gönderilemedi.' };
    }
}

export function mockConnect(userId) {
    if (process.env.WA_MOCK_MODE !== '1') {
        return { success: false, error: 'Devre dışı.' };
    }
    const cleanUid = String(userId || '').trim();
    let inst = instances.get(cleanUid);
    if (!inst) {
        inst = {
            userId: cleanUid,
            phone: '',
            status: 'connected',
            connected: true,
            loggedOut: false,
            reconnecting: false,
            connectedAt: Date.now(),
            lastKeepAliveAt: Date.now(),
            reconnectCount: 0,
            pairingCode: null,
            expiresAt: null,
            sock: null,
            stopped: false,
            reconnectTimer: null,
            configUpdatesCount: 0,
            config: defaultConfig({}),
        };
        instances.set(cleanUid, inst);
    } else {
        inst.status = 'connected';
        inst.connected = true;
        inst.loggedOut = false;
        inst.connectedAt = Date.now();
        inst.lastKeepAliveAt = Date.now();
        inst.pairingCode = null;
    }
    return getStatus(cleanUid);
}

export function mockDisconnect(userId) {
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
    return getStatus(cleanUid);
}

export async function restorePersistedBots() {
    if (process.env.WA_MOCK_MODE === '1') return { restored: 0 };
    if (!fs.existsSync(AUTH_BASE_DIR)) return { restored: 0 };
    let restored = 0;
    try {
        const entries = fs.readdirSync(AUTH_BASE_DIR, { withFileTypes: true });
        for (const ent of entries) {
            if (!ent.isDirectory()) continue;
            const dir = path.join(AUTH_BASE_DIR, ent.name);
            const credsFile = path.join(dir, 'creds.json');
            const metaFile = path.join(dir, 'aslan_meta.json');
            if (!fs.existsSync(credsFile) || !fs.existsSync(metaFile)) continue;
            try {
                const meta = JSON.parse(fs.readFileSync(metaFile, 'utf-8'));
                const uid = String((meta && meta.userId) || '').trim();
                if (!uid || instances.has(uid)) continue;
                const inst = {
                    userId: uid,
                    phone: String((meta && meta.phone) || ''),
                    status: 'reconnecting',
                    connected: false,
                    loggedOut: false,
                    reconnecting: false,
                    connectedAt: null,
                    lastKeepAliveAt: 0,
                    reconnectCount: 0,
                    pairingCode: null,
                    expiresAt: null,
                    sock: null,
                    stopped: false,
                    reconnectTimer: null,
                    configUpdatesCount: 0,
                    contacts: hydrateContacts(meta && meta.contacts),
                    config: defaultConfig(meta && meta.config),
                };
                instances.set(uid, inst);
                openLiveSocket(inst).catch(() => {});
                restored++;
            } catch (_) {}
        }
    } catch (_) {}
    return { restored };
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

/**
 * Checks all non-stopped bot sockets cleanly and triggers IPv4 heartbeat/reconnect when needed.
 */
export function keepAliveActiveBots() {
    let checked = 0;
    let alive = 0;
    let reconnected = 0;
    for (const inst of instances.values()) {
        if (!inst || inst.stopped || inst.loggedOut || inst.status === 'idle') continue;
        checked++;
        try {
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
            if (isSocketOpen(inst.sock)) {
                pingSocket(inst.sock);
                inst.lastKeepAliveAt = Date.now();
                alive++;
            } else if (
                !inst.reconnectTimer &&
                !inst.reconnecting &&
                (inst.connected || inst.status === 'reconnecting' || inst.status === 'disconnected' || inst.status === 'error')
            ) {
                inst.reconnecting = true;
                inst.reconnectCount = (inst.reconnectCount || 0) + 1;
                reconnected++;
                openLiveSocket(inst)
                    .catch(() => {})
                    .finally(() => { inst.reconnecting = false; });
            }
        } catch (_) {}
    }
    return { checked, alive, reconnected };
}
