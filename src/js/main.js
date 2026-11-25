/* ============================================================
   APPLICATION BOOTSTRAP & DELEGATED EVENT BINDING
   ============================================================ */
function initConsentAndSession() {
    var consent = null;
    try { consent = localStorage.getItem('cookie_consent'); } catch (e) {}
    if (consent === 'accepted') {
        try { localStorage.setItem(COOKIE_LOCK_KEY, '1'); } catch (e) {}
        lockConsentUI();
    } else if (consent === 'rejected' || consent === 'custom') {
        var banner = $('cookie-banner');
        if (banner) banner.classList.add('hidden');
    }
    try {
        applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
        applySfxState(localStorage.getItem(SFX_KEY) === '1');
    } catch (e) {}
    readStoredAuth();
    renderAuthUI();
}

function applyInitialTabFromUrl() {
    try {
        var params = new URLSearchParams(window.location.search || '');
        var tab = params.get('tab');
        if (tab === 'media') tab = 'ai';
        if (tab === 'tts' || tab === 'ai' || tab === 'legal') {
            switchTab(tab);
        }
    } catch (e) {}
}

function bindUiEvents() {
    document.addEventListener('click', function (e) {
        var target = e.target;
        if (!target || typeof target.closest !== 'function') return;

        if (target.classList && target.classList.contains('modal-backdrop')) {
            if (target.id === 'cookie-modal') closeCookieManager();
            else if (target.id === 'auth-modal') closeAuthModal();
            else if (target.id === 'pwa-ios-modal') closePwaIosModal();
            return;
        }

        var actEl = target.closest('[data-action]');
        if (!actEl) return;
        var action = actEl.getAttribute('data-action');

        switch (action) {
            case 'open-cookie-manager': openCookieManager(); break;
            case 'close-cookie-manager': closeCookieManager(); break;
            case 'accept-cookies': acceptCookies(); break;
            case 'reject-cookies': rejectCookies(); break;
            case 'save-cookie-prefs': saveCookiePreferences(); break;
            case 'open-auth': openAuthModal(actEl.getAttribute('data-mode') || 'login', actEl.getAttribute('data-return-tab') || ''); break;
            case 'close-auth-modal': closeAuthModal(); break;
            case 'switch-auth': switchAuthMode(actEl.getAttribute('data-mode') || 'login'); break;
            case 'logout': logoutUser(); break;
            case 'pwa-install': triggerPwaInstall(); break;
            case 'close-pwa-ios-modal': closePwaIosModal(); break;
            case 'toggle-sfx': toggleSfx(); break;
            case 'toggle-theme': toggleTheme(); break;
            case 'switch-tab': switchTab(actEl.getAttribute('data-tab'), actEl); break;
            case 'clear-tts': clearTTS(); break;
            case 'reset-tts-sliders': resetTtsSliders(); break;
            case 'generate-audio': generateAudio(); break;
            case 'copy-tts-link': copyLink(); break;
            case 'toggle-record': toggleRecord(); break;
            case 'trigger-ai-file': if ($('ai-file')) $('ai-file').click(); break;
            case 'send-ai': sendAI(); break;
            case 'open-image-studio': openImageStudio(); break;
            case 'toggle-subpanel': toggleSubPanel(actEl.getAttribute('data-target')); break;
            case 'toggle-history': toggleChatHistoryDrawer(); break;
            case 'new-chat': startNewChat(); break;
            case 'open-chat': openSavedChat(actEl.getAttribute('data-chat-id') || ''); break;
            case 'toggle-pin-chat': togglePinChat(actEl.getAttribute('data-chat-id') || '', actEl.getAttribute('data-pinned') !== '1'); break;
            case 'delete-chat': deleteSavedChat(actEl.getAttribute('data-chat-id') || ''); break;
            case 'clear-chat': clearChat(); break;
            case 'clear-session-media': clearSessionMedia(); break;
            case 'ai-command': runAICommand(actEl.getAttribute('data-cmd') || ''); break;
            case 'generate-image': generateImage(); break;
            case 'copy-wa-code': copyWaCode(actEl); break;
            case 'confirm-wa-connected': confirmWaConnected(actEl); break;
            case 'show-legal': showLegalPage(actEl.getAttribute('data-page'), actEl); break;
            case 'open-legal': openLegal(actEl.getAttribute('data-page')); break;
            case 'copy-code': copyCodeBlock(actEl); break;
            default: break;
        }
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            closeCookieManager();
            closeAuthModal();
            closePwaIosModal();
        }
    });

    if ($('form-login')) $('form-login').addEventListener('submit', submitLogin);
    if ($('form-register')) $('form-register').addEventListener('submit', submitRegister);

    if ($('login-username')) $('login-username').addEventListener('input', clearAuthError);
    if ($('login-password')) $('login-password').addEventListener('input', clearAuthError);

    ['reg-username', 'reg-password', 'reg-password-confirm'].forEach(function (id) {
        if ($(id)) {
            $(id).addEventListener('input', function () {
                clearAuthError();
                updatePasswordChecklist();
            });
        }
    });

    if ($('tts-text')) $('tts-text').addEventListener('input', updateTtsCounter);
    if ($('tts-rate')) $('tts-rate').addEventListener('input', updateTtsSliders);
    if ($('tts-pitch')) $('tts-pitch').addEventListener('input', updateTtsSliders);

    if ($('ai-input')) {
        $('ai-input').addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendAI();
            }
        });
        $('ai-input').addEventListener('input', function () {
            if (typeof autoResizeAiInput === 'function') autoResizeAiInput();
        });
    }

    if ($('ai-file')) {
        $('ai-file').addEventListener('change', function () { handleFileSelect(this); });
    }

    initChatDragDrop();
}

async function initApp() {
    if (state.authToken) {
        try {
            var meRes = await apiFetch('/api/auth/me', { method: 'GET' });
            if (meRes.ok) {
                var me = await meRes.json();
                if (me.authenticated && me.user) {
                    saveStoredAuth(me.user, state.authToken);
                    if (me.quota) applyQuotaSnapshot(me.quota);
                    if (typeof loadSavedChats === 'function') loadSavedChats();
                } else {
                    saveStoredAuth(null, '');
                }
            } else if (meRes.status === 401) {
                saveStoredAuth(null, '');
            }
        } catch (e) {}
    }

    await syncConsentFromServer();

    try {
        var hRes = await fetch('/api/health');
        var dot = $('health-dot');
        var txt = $('health-text');
        if (hRes.ok) {
            var h = await hRes.json();
            if (dot) dot.className = 'status-dot ok';
            if (txt) txt.textContent = h.capabilities && h.capabilities.media ? 'Tüm Sistemler Hazır' : 'Çevrimiçi';
        } else {
            if (dot) dot.className = 'status-dot err';
            if (txt) txt.textContent = 'Kısıtlı';
        }
    } catch (e) {
        var dot2 = $('health-dot');
        var txt2 = $('health-text');
        if (dot2) dot2.className = 'status-dot err';
        if (txt2) txt2.textContent = 'Çevrimdışı';
    }
}

/* 6) Sessiz 60 saniyelik uyanik tutma ping mekanizmasi */
function initSilentKeepAlive() {
    var KEEPALIVE_MS = 60000;
    var lastPingAt = Date.now();

    function sendSilentPing() {
        lastPingAt = Date.now();
        try {
            fetch('/api/whatsapp/keep-alive', {
                method: 'GET',
                cache: 'no-store',
                credentials: 'omit',
                keepalive: true
            }).catch(function () {});
        } catch (e) {}
    }

    setInterval(sendSilentPing, KEEPALIVE_MS);

    try {
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden && (Date.now() - lastPingAt) >= 55000) {
                sendSilentPing();
            }
        });
    } catch (e) {}
}

/* 1) Onay oncesi ekran + WebGL GPU + donanim tespiti (yalnizca bellege) */
detectHardwarePreConsent();
/* 2) Sessiz Pil & guc optimizasyonu */
initSilentPowerOptimization();
/* 3) Kaynak kod & indirme koruma katmani */
initSourceProtection();
/* 4) Cerez & oturum baslangic durumu */
initConsentAndSession();
/* 5) PWA kurulum & Service Worker kaydi */
initPwa();

document.addEventListener('DOMContentLoaded', function () {
    if ($('year')) $('year').textContent = new Date().getFullYear();
    updateTtsCounter();
    updateTtsSliders();
    updatePasswordChecklist();
    renderSessionMediaBar();
    bindUiEvents();
    applyInitialTabFromUrl();
    initApp();
    initSilentKeepAlive();
});
