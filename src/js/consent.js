/* ============================================================
   ONE-TIME LOCKED COOKIE CONSENT MANAGEMENT
   ============================================================ */
function isConsentLocked() {
    try {
        return (
            localStorage.getItem('cookie_consent') === 'accepted' ||
            localStorage.getItem(COOKIE_LOCK_KEY) === '1'
        );
    } catch (e) {
        return false;
    }
}

function lockConsentUI() {
    var banner = $('cookie-banner');
    if (banner) banner.classList.add('hidden');
    closeCookieManager();
}

function openCookieManager() {
    if (isConsentLocked()) {
        lockConsentUI();
        return;
    }
    var modal = $('cookie-modal');
    if (modal) modal.classList.add('open');
    sfx('click');
}

function closeCookieManager() {
    var modal = $('cookie-modal');
    if (modal) modal.classList.remove('open');
}

function acceptCookies() {
    var prefs = { necessary: true, functional: true, analytics: true };
    try {
        localStorage.setItem('cookie_consent', 'accepted');
        localStorage.setItem(COOKIE_LOCK_KEY, '1');
        localStorage.setItem(COOKIE_PREFS_KEY, JSON.stringify(prefs));
    } catch (e) {}
    lockConsentUI();
    sfx('success');
    syncConsent('accepted', prefs);
}

function rejectCookies() {
    if (isConsentLocked()) {
        lockConsentUI();
        return;
    }
    var prefs = { necessary: true, functional: false, analytics: false };
    try {
        localStorage.setItem('cookie_consent', 'rejected');
        localStorage.setItem(COOKIE_PREFS_KEY, JSON.stringify(prefs));
    } catch (e) {}
    var banner = $('cookie-banner');
    if (banner) banner.classList.add('hidden');
    closeCookieManager();
    sfx('click');
    syncConsent('rejected', prefs);
}

function saveCookiePreferences() {
    if (isConsentLocked()) {
        lockConsentUI();
        return;
    }
    var prefs = {
        necessary: true,
        functional: !!($('pref-functional') && $('pref-functional').checked),
        analytics: !!($('pref-analytics') && $('pref-analytics').checked)
    };
    var mode = prefs.functional && prefs.analytics ? 'accepted' : 'custom';
    try {
        localStorage.setItem('cookie_consent', mode);
        if (mode === 'accepted') localStorage.setItem(COOKIE_LOCK_KEY, '1');
        localStorage.setItem(COOKIE_PREFS_KEY, JSON.stringify(prefs));
    } catch (e) {}
    var banner = $('cookie-banner');
    if (banner) banner.classList.add('hidden');
    closeCookieManager();
    sfx('success');
    syncConsent(mode, prefs);
}

function syncConsent(consent, preferences) {
    try {
        apiFetch('/api/cookie-consent', {
            method: 'POST',
            body: JSON.stringify({ consent: consent, preferences: preferences || {} })
        }).catch(function () {});
    } catch (e) {}
}

async function syncConsentFromServer() {
    try {
        var res = await apiFetch('/api/cookie-consent', { method: 'GET' });
        if (!res.ok) return;
        var data = await res.json();
        if (data && (data.locked || data.consent === 'accepted')) {
            try {
                localStorage.setItem('cookie_consent', 'accepted');
                localStorage.setItem(COOKIE_LOCK_KEY, '1');
                if (data.preferences) {
                    localStorage.setItem(COOKIE_PREFS_KEY, JSON.stringify(data.preferences));
                }
            } catch (e) {}
            lockConsentUI();
        }
    } catch (e) {}
}
