/* ============================================================
   GLOBAL STATE, CONSTANTS & SAFE UTILITIES
   ============================================================ */
var CHAT_MAX = 5;
var IMAGE_MONTHLY_MAX = 2;
var TOKEN_KEY = 'aslan_auth_token_v1';
var USER_KEY = 'aslan_auth_user_v1';
var DEVICE_KEY = 'aslan_device_id_v1';
var THEME_KEY = 'aslan_theme';
var SFX_KEY = 'aslan_sfx_muted';
var COOKIE_PREFS_KEY = 'aslan_cookie_prefs_v1';
var COOKIE_LOCK_KEY = 'aslan_cookie_locked_v1';
var SERVERLESS_INLINE_WARN_BYTES = 3 * 1024 * 1024;

var state = {
    attachments: [],
    sessionMedia: [],
    mediaPreviewUrls: [],
    attachPreviewUrls: [],
    chatHistory: [],
    activeChatId: '',
    savedChats: [],
    pinnedCount: 0,
    maxPinnedChats: 2,
    audioCtx: null,
    sfxMuted: false,
    batteryLow: false,
    lastAudioUrl: '',
    deviceId: '',
    authToken: '',
    authReturnTab: '',
    user: null,
    quota: { ai: null, image: null, wa_bot: null },
    aiSettings: null,
    aiProviders: [],
    systemProfile: null,
    powerMode: 'high',
    perfTier: 'high',
    pwaDeferredPrompt: null
};

function $(id) {
    return document.getElementById(id);
}

function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
}

function safeMediaUrl(rawUrl) {
    var s = String(rawUrl || '').trim();
    if (!s) return '';
    if (s.indexOf('/api/') === 0) return s;
    if (s.indexOf('blob:') === 0) return s;
    if (/^https:\/\//i.test(s)) return s;
    if (/^data:(?:image\/(?:png|jpeg|jpg|webp|gif|bmp)|audio\/[a-z0-9.+-]+|video\/[a-z0-9.+-]+|application\/octet-stream);base64,[A-Za-z0-9+/=\s]+$/i.test(s)) {
        return s;
    }
    return '';
}

function showEl(el, visible) {
    if (!el) return;
    el.classList.toggle('is-hidden', !visible);
}

function fmtSize(b) {
    if (!b && b !== 0) return '';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    return (b / 1048576).toFixed(2) + ' MB';
}

function fileIcon(t) {
    t = String(t || '');
    if (t.indexOf('image') === 0) return '🖼️';
    if (t.indexOf('video') === 0) return '🎬';
    if (t.indexOf('audio') === 0) return '🎵';
    if (t === 'application/pdf') return '📕';
    return '📄';
}

async function parseApiResponse(res) {
    var ctype = (res.headers.get('content-type') || '').toLowerCase();
    var raw = await res.text();
    if (ctype.indexOf('application/json') !== -1) {
        try { return JSON.parse(raw); } catch (e) {}
    }
    if (res.status === 413) {
        return { error: 'Gönderilen dosya veya istek boyut sınırını aştı (maks. 4.5 MB).' };
    }
    if (res.status === 502 || res.status === 504) {
        return { error: 'Sunucu geçici olarak yanıt veremedi veya zaman aşımına uğradı.' };
    }
    return { error: raw ? raw.slice(0, 180) : ('Sunucu hatası: HTTP ' + res.status) };
}
