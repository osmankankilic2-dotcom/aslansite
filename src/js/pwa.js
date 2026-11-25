/* ============================================================
   PWA SERVICE WORKER & INSTALL PROMPT MANAGEMENT
   ============================================================ */
function isIosSafari() {
    var ua = String((window.navigator && navigator.userAgent) || '').toLowerCase();
    return /iphone|ipad|ipod/.test(ua) && !window.MSStream;
}

function isStandaloneMode() {
    try {
        if (window.navigator && window.navigator.standalone === true) return true;
        if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return true;
    } catch (e) {}
    return false;
}

function updatePwaInstallBtn() {
    var btn = $('btn-pwa-install');
    if (!btn) return;
    if (isStandaloneMode()) {
        showEl(btn, false);
        return;
    }
    var canShow = !!state.pwaDeferredPrompt || isIosSafari();
    showEl(btn, canShow);
}

function openPwaIosModal() {
    var modal = $('pwa-ios-modal');
    if (modal) modal.classList.add('open');
    sfx('click');
}

function closePwaIosModal() {
    var modal = $('pwa-ios-modal');
    if (modal) modal.classList.remove('open');
}

async function triggerPwaInstall() {
    if (state.pwaDeferredPrompt) {
        var promptEvt = state.pwaDeferredPrompt;
        state.pwaDeferredPrompt = null;
        try {
            await promptEvt.prompt();
            var choice = await promptEvt.userChoice;
            if (choice && choice.outcome === 'accepted') {
                showToast('Aslan AI uygulaması ana ekrana kuruluyor.', 'success');
                sfx('success');
            }
        } catch (e) {}
        updatePwaInstallBtn();
        return;
    }
    if (isIosSafari() && !isStandaloneMode()) {
        openPwaIosModal();
    }
}

function initPwa() {
    window.addEventListener('beforeinstallprompt', function (e) {
        e.preventDefault();
        state.pwaDeferredPrompt = e;
        updatePwaInstallBtn();
    });

    window.addEventListener('appinstalled', function () {
        state.pwaDeferredPrompt = null;
        updatePwaInstallBtn();
        showToast('Aslan AI uygulama olarak kuruldu!', 'success');
    });

    updatePwaInstallBtn();

    if ('serviceWorker' in navigator) {
        try {
            navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
        } catch (e) {}
    }
}
