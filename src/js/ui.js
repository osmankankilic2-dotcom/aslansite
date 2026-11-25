/* ============================================================
   THEME, WEB AUDIO SFX, TABS, TOASTS & LEGAL PAGES
   ============================================================ */
function applyTheme(theme) {
    var t = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
    var btn = $('btn-theme-toggle');
    if (btn) btn.textContent = t === 'light' ? '🌙 Koyu Tema' : '☀️ Açık Tema';
}

function toggleTheme() {
    var cur = document.documentElement.getAttribute('data-theme') || 'dark';
    var next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    sfx('click');
}

function applySfxState(muted) {
    state.sfxMuted = !!muted;
    var btn = $('btn-sfx-toggle');
    if (btn) {
        btn.textContent = state.sfxMuted ? '🔇 Sessiz' : '🔊 Efekt';
        btn.setAttribute('aria-pressed', state.sfxMuted ? 'false' : 'true');
    }
}

function toggleSfx() {
    applySfxState(!state.sfxMuted);
    try { localStorage.setItem(SFX_KEY, state.sfxMuted ? '1' : '0'); } catch (e) {}
    if (!state.sfxMuted) sfx('click');
}

function getAudioCtx() {
    if (!state.audioCtx) {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        state.audioCtx = new AC();
    }
    if (state.audioCtx.state === 'suspended') state.audioCtx.resume();
    return state.audioCtx;
}

function tone(freq, dur, type, gain, delay) {
    if (state.sfxMuted || state.batteryLow) return;
    var ctx = getAudioCtx();
    if (!ctx) return;
    var t0 = ctx.currentTime + (delay || 0);
    var osc = ctx.createOscillator();
    var g = ctx.createGain();
    osc.type = type || 'sine';
    osc.frequency.setValueAtTime(freq, t0);
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(gain == null ? 0.08 : gain, t0 + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g);
    g.connect(ctx.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.02);
}

var SFX = {
    click:   function () { tone(660, 0.07, 'triangle', 0.05); },
    success: function () { tone(523, 0.11, 'sine', 0.07); tone(784, 0.13, 'sine', 0.06, 0.09); },
    error:   function () { tone(220, 0.16, 'sawtooth', 0.05); tone(165, 0.2, 'sawtooth', 0.05, 0.1); },
    send:    function () { tone(880, 0.06, 'square', 0.035); },
    receive: function () { tone(587, 0.09, 'sine', 0.05); tone(880, 0.1, 'sine', 0.04, 0.07); },
    ready:   function () { tone(700, 0.08, 'triangle', 0.05); tone(1000, 0.09, 'triangle', 0.04, 0.07); }
};

function sfx(name) {
    if (state.sfxMuted || state.batteryLow) return;
    try { (SFX[name] || SFX.click)(); } catch (e) {}
}

function switchTab(name, btn) {
    document.querySelectorAll('.card').forEach(function (c) { c.classList.remove('active'); });
    document.querySelectorAll('.tab-btn').forEach(function (b) {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
    });
    var panel = $('panel-' + name);
    if (panel) panel.classList.add('active');
    var targetBtn = btn || document.querySelector('.tab-btn[data-tab="' + name + '"]');
    if (targetBtn) {
        targetBtn.classList.add('active');
        targetBtn.setAttribute('aria-selected', 'true');
    }
    sfx('click');
}

function showToast(message, type) {
    var container = $('toast-container');
    if (!container) return;
    var toast = document.createElement('div');
    toast.className = 'toast ' + (type || 'info');
    var span = document.createElement('span');
    span.textContent = String(message || '');
    var close = document.createElement('button');
    close.type = 'button';
    close.setAttribute('aria-label', 'Bildirimi kapat');
    close.textContent = '×';
    close.addEventListener('click', function () { toast.remove(); });
    toast.appendChild(span);
    toast.appendChild(close);
    container.appendChild(toast);
    setTimeout(function () { if (toast.parentNode) toast.remove(); }, 4200);
}

function showLegalPage(pageId, btn) {
    document.querySelectorAll('.legal-page').forEach(function (p) { p.classList.remove('active'); });
    var page = $('legal-page-' + pageId);
    if (page) page.classList.add('active');
    var targetBtn = btn || document.querySelector('.legal-nav button[data-page="' + pageId + '"]');
    if (targetBtn) {
        document.querySelectorAll('.legal-nav button').forEach(function (b) { b.classList.remove('active'); });
        targetBtn.classList.add('active');
    }
    sfx('click');
    if (page) {
        try {
            page.scrollIntoView({ behavior: state.powerMode === 'eco' ? 'auto' : 'smooth', block: 'start' });
        } catch (e) {}
    }
}

function openLegal(pageId) {
    switchTab('legal');
    showLegalPage(pageId);
    try {
        window.scrollTo({ top: 0, behavior: state.powerMode === 'eco' ? 'auto' : 'smooth' });
    } catch (e) {}
}
