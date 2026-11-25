/* ============================================================
   COOKIELESS SESSION & AUTHENTICATION (NO window.name STORAGE)
   ============================================================ */
function getDeviceId() {
    if (state.deviceId) return state.deviceId;
    var id = '';
    try { id = localStorage.getItem(DEVICE_KEY) || ''; } catch (e) {}
    if (!id) {
        var rand = '';
        try {
            var buf = new Uint8Array(16);
            (window.crypto || window.msCrypto).getRandomValues(buf);
            for (var i = 0; i < buf.length; i++) {
                rand += ('0' + buf[i].toString(16)).slice(-2);
            }
        } catch (e) {
            rand = Math.random().toString(16).slice(2) + Date.now().toString(16);
        }
        id = 'aslan-' + rand;
        try { localStorage.setItem(DEVICE_KEY, id); } catch (e) {}
    }
    state.deviceId = id;
    return id;
}

function readStoredAuth() {
    var tok = '';
    var usr = null;
    try { tok = localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || ''; } catch (e) {}
    try {
        var rawU = localStorage.getItem(USER_KEY) || sessionStorage.getItem(USER_KEY) || '';
        if (rawU) usr = JSON.parse(rawU);
    } catch (e) {}
    state.authToken = tok;
    state.user = usr;
}

function saveStoredAuth(user, token) {
    state.user = user || null;
    state.authToken = token || '';
    try {
        if (token) {
            localStorage.setItem(TOKEN_KEY, token);
            sessionStorage.setItem(TOKEN_KEY, token);
            localStorage.setItem(USER_KEY, JSON.stringify(user || {}));
            sessionStorage.setItem(USER_KEY, JSON.stringify(user || {}));
        } else {
            localStorage.removeItem(TOKEN_KEY);
            sessionStorage.removeItem(TOKEN_KEY);
            localStorage.removeItem(USER_KEY);
            sessionStorage.removeItem(USER_KEY);
        }
    } catch (e) {}
    renderAuthUI();
}

function renderAuthUI() {
    var loggedIn = !!(state.authToken && state.user && state.user.username);
    var nav = $('nav-auth-area');
    if (nav) {
        nav.innerHTML = '';
        if (loggedIn) {
            var badge = document.createElement('span');
            badge.className = 'pill-badge';
            badge.textContent = '👤 @' + state.user.username;
            var outBtn = document.createElement('button');
            outBtn.type = 'button';
            outBtn.className = 'icon-pill-btn';
            outBtn.setAttribute('data-action', 'logout');
            outBtn.textContent = 'Çıkış';
            nav.appendChild(badge);
            nav.appendChild(outBtn);
        } else {
            var loginBtn = document.createElement('button');
            loginBtn.type = 'button';
            loginBtn.className = 'icon-pill-btn primary';
            loginBtn.setAttribute('data-action', 'open-auth');
            loginBtn.setAttribute('data-mode', 'login');
            loginBtn.setAttribute('data-return-tab', 'ai');
            loginBtn.textContent = '🔑 Giriş Yap';
            var regBtn = document.createElement('button');
            regBtn.type = 'button';
            regBtn.className = 'icon-pill-btn';
            regBtn.setAttribute('data-action', 'open-auth');
            regBtn.setAttribute('data-mode', 'register');
            regBtn.setAttribute('data-return-tab', 'ai');
            regBtn.textContent = '📝 Kayıt Ol';
            nav.appendChild(loginBtn);
            nav.appendChild(regBtn);
        }
    }
    showEl($('tts-auth-lock'), !loggedIn);
    showEl($('tts-workspace'), loggedIn);
    showEl($('ai-auth-lock'), !loggedIn);
    showEl($('ai-workspace'), loggedIn);
}

function applyQuotaSnapshot(snap) {
    if (!snap) return;
    if (snap.ai) state.quota.ai = snap.ai;
    if (snap.image) state.quota.image = snap.image;
    renderQuota();
}

function renderQuota() {
    var ai = state.quota.ai;
    if (ai && typeof ai.remaining === 'number' && $('quota-display')) {
        $('quota-display').textContent = ai.remaining + '/' + (ai.limit || CHAT_MAX);
    }
    var img = state.quota.image;
    var el = $('image-quota-display');
    if (el && img) {
        var lim = typeof img.monthly_limit === 'number' ? img.monthly_limit : IMAGE_MONTHLY_MAX;
        var rem = typeof img.monthly_remaining === 'number' ? img.monthly_remaining : (typeof img.remaining === 'number' ? img.remaining : lim);
        el.textContent = 'Aylık Görsel: ' + rem + '/' + lim + (rem <= 0 ? ' (Bu ayki hak doldu)' : '');
        el.classList.toggle('warn', rem <= 0);
        var genBtn = $('btn-generate-image');
        if (genBtn) {
            genBtn.disabled = rem <= 0;
            genBtn.textContent = rem <= 0 ? ('🎨 Aylık Görsel Hakkı Doldu (0/' + lim + ')') : '🎨 Görsel Üret';
        }
    }
}

function apiFetch(url, options) {
    var opts = Object.assign({ credentials: 'omit' }, options || {});
    var headers = Object.assign({}, opts.headers || {});
    headers['X-Device-Id'] = getDeviceId();
    headers['X-Requested-With'] = 'AslanWeb';
    if (state.authToken) {
        headers['Authorization'] = 'Bearer ' + state.authToken;
        headers['X-Auth-Token'] = state.authToken;
    }
    if (opts.body && !(opts.body instanceof FormData) && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
    }
    opts.headers = headers;
    return fetch(url, opts);
}

function fileToInline(file) {
    return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onload = function () {
            var result = String(reader.result || '');
            var comma = result.indexOf(',');
            resolve({
                name: file.name,
                mime_type: file.type || 'application/octet-stream',
                size: file.size,
                data: comma >= 0 ? result.slice(comma + 1) : result
            });
        };
        reader.onerror = function () { reject(new Error('Dosya okunamadı: ' + file.name)); };
        reader.readAsDataURL(file);
    });
}

async function filesToInline(list) {
    var out = [];
    for (var i = 0; i < list.length; i++) {
        out.push(await fileToInline(list[i]));
    }
    return out;
}

function showAuthError(msg) {
    var box = $('auth-error-box');
    if (!box) return;
    box.textContent = '⚠️ ' + String(msg || 'İşlem başarısız oldu.');
    showEl(box, true);
    try { box.scrollIntoView({ behavior: state.powerMode === 'eco' ? 'auto' : 'smooth', block: 'nearest' }); } catch (e) {}
}

function clearAuthError() {
    var box = $('auth-error-box');
    if (box) {
        box.textContent = '';
        showEl(box, false);
    }
}

function openAuthModal(mode, returnTab) {
    if (returnTab !== undefined) state.authReturnTab = returnTab;
    clearAuthError();
    switchAuthMode(mode || 'login');
    var modal = $('auth-modal');
    if (modal) modal.classList.add('open');
    sfx('click');
}

function closeAuthModal() {
    clearAuthError();
    var modal = $('auth-modal');
    if (modal) modal.classList.remove('open');
}

function switchAuthMode(mode) {
    clearAuthError();
    var isReg = mode === 'register';
    showEl($('form-login'), !isReg);
    showEl($('form-register'), isReg);
    if ($('auth-tab-login')) $('auth-tab-login').classList.toggle('active', !isReg);
    if ($('auth-tab-register')) $('auth-tab-register').classList.toggle('active', isReg);
}

function _setRule(id, ok) {
    var el = $(id);
    if (!el) return;
    el.className = ok ? 'ok' : 'bad';
    el.textContent = (ok ? '✓ ' : '○ ') + el.textContent.slice(2);
}

function updatePasswordChecklist() {
    var u = ($('reg-username') ? $('reg-username').value : '').trim();
    var p1 = $('reg-password') ? $('reg-password').value : '';
    var p2 = $('reg-password-confirm') ? $('reg-password-confirm').value : '';
    var unameOk = u.length >= 3 && u.length <= 32;
    try { unameOk = /^[\p{L}\p{N}_.-]{3,32}$/u.test(u); } catch (e) { unameOk = /^[^\s]{3,32}$/.test(u); }
    _setRule('chk-uname', unameOk);
    _setRule('chk-len', p1.length >= 12);
    _setRule('chk-upper', /[A-ZÇĞİÖŞÜ]/.test(p1));
    _setRule('chk-lower', /[a-zçğıöşü]/.test(p1));
    _setRule('chk-digit', /[0-9]/.test(p1));
    _setRule('chk-sym', /[^A-Za-zÇĞİÖŞÜçğıöşü0-9\s]/.test(p1));
    _setRule('chk-match', p1.length > 0 && p1 === p2);
}

function completeAuthSuccess(data, toastMsg) {
    saveStoredAuth(data.user, data.token);
    if (data.quota) applyQuotaSnapshot(data.quota);
    closeAuthModal();
    var dest = state.authReturnTab || 'ai';
    state.authReturnTab = '';
    if (dest !== 'tts' && dest !== 'ai') dest = 'ai';
    switchTab(dest);
    try { window.scrollTo({ top: 0, behavior: state.powerMode === 'eco' ? 'auto' : 'smooth' }); } catch (e) {}
    try {
        var focusEl = dest === 'tts' ? $('tts-text') : $('ai-input');
        if (focusEl) focusEl.focus();
    } catch (e) {}
    try { sfx('success'); } catch (e) {}
    showToast(toastMsg, 'success');
}

async function submitLogin(e) {
    e.preventDefault();
    clearAuthError();
    var username = ($('login-username') ? $('login-username').value : '').trim();
    var password = $('login-password') ? $('login-password').value : '';
    if (!username) {
        showAuthError('Lütfen kullanıcı adınızı girin.');
        if ($('login-username')) $('login-username').focus();
        return;
    }
    if (!password) {
        showAuthError('Lütfen şifrenizi girin.');
        if ($('login-password')) $('login-password').focus();
        return;
    }
    var btn = $('btn-login-submit');
    btn.disabled = true;
    btn.textContent = 'Giriş yapılıyor...';
    try {
        var res = await apiFetch('/api/auth/login', {
            method: 'POST',
            body: JSON.stringify({ username: username, password: password })
        });
        var data = await parseApiResponse(res);
        if (!res.ok || !data.token) throw new Error(data.error || 'Giriş başarısız.');
        $('login-password').value = '';
        completeAuthSuccess(data, 'Hoş geldiniz, @' + data.user.username + '!');
    } catch (err) {
        sfx('error');
        showAuthError(err.message);
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Giriş Yap';
    }
}

async function submitRegister(e) {
    e.preventDefault();
    clearAuthError();
    var username = ($('reg-username') ? $('reg-username').value : '').trim();
    var password = $('reg-password') ? $('reg-password').value : '';
    var password_confirm = $('reg-password-confirm') ? $('reg-password-confirm').value : '';
    updatePasswordChecklist();

    if (username.length < 3) {
        showAuthError('Kullanıcı adı en az 3 karakter olmalıdır.');
        if ($('reg-username')) $('reg-username').focus();
        return;
    }
    if (username.length > 32) {
        showAuthError('Kullanıcı adı en fazla 32 karakter olabilir.');
        if ($('reg-username')) $('reg-username').focus();
        return;
    }
    var unameValid = false;
    try { unameValid = /^[\p{L}\p{N}_.-]{3,32}$/u.test(username); } catch (ex) { unameValid = /^[^\s]{3,32}$/.test(username); }
    if (!unameValid) {
        showAuthError('Kullanıcı adı boşluk içeremez; yalnızca harf, rakam, nokta (.), tire (-) ve alt çizgi (_) kullanılabilir.');
        if ($('reg-username')) $('reg-username').focus();
        return;
    }
    if (password.length < 12) {
        showAuthError('Şifre en az 12 karakter uzunluğunda olmalıdır.');
        if ($('reg-password')) $('reg-password').focus();
        return;
    }
    if (!/[A-ZÇĞİÖŞÜ]/.test(password)) {
        showAuthError('Şifre en az 1 büyük harf (A-Z) içermelidir.');
        if ($('reg-password')) $('reg-password').focus();
        return;
    }
    if (!/[a-zçğıöşü]/.test(password)) {
        showAuthError('Şifre en az 1 küçük harf (a-z) içermelidir.');
        if ($('reg-password')) $('reg-password').focus();
        return;
    }
    if (!/[0-9]/.test(password)) {
        showAuthError('Şifre en az 1 rakam (0-9) içermelidir.');
        if ($('reg-password')) $('reg-password').focus();
        return;
    }
    if (!/[^A-Za-zÇĞİÖŞÜçğıöşü0-9\s]/.test(password)) {
        showAuthError('Şifre en az 1 nokta veya özel sembol (. ! @ # $ % _ - vb.) içermelidir.');
        if ($('reg-password')) $('reg-password').focus();
        return;
    }
    if (password !== password_confirm) {
        showAuthError('Girilen şifreler birbiriyle eşleşmiyor.');
        if ($('reg-password-confirm')) $('reg-password-confirm').focus();
        return;
    }

    var btn = $('btn-register-submit');
    btn.disabled = true;
    btn.textContent = 'Hesap oluşturuluyor...';
    try {
        var res = await apiFetch('/api/auth/register', {
            method: 'POST',
            body: JSON.stringify({
                username: username,
                password: password,
                password_confirm: password_confirm
            })
        });
        var data = await parseApiResponse(res);
        if (!res.ok || !data.token) throw new Error(data.error || 'Kayıt başarısız.');
        $('reg-password').value = '';
        $('reg-password-confirm').value = '';
        completeAuthSuccess(data, 'Hesabınız oluşturuldu ve otomatik giriş yapıldı, @' + data.user.username + '!');
    } catch (err) {
        sfx('error');
        showAuthError(err.message);
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Hesap Oluştur ve Giriş Yap';
    }
}

async function logoutUser() {
    try {
        await apiFetch('/api/auth/logout', { method: 'POST', body: '{}' });
    } catch (e) {}
    saveStoredAuth(null, '');
    state.chatHistory = [];
    sfx('click');
    showToast('Oturum kapatıldı.', 'info');
}

function handleAuthRequired(errMsg) {
    saveStoredAuth(null, '');
    openAuthModal('login');
    showToast(errMsg || 'Bu özelliği kullanmak için giriş yapmalısınız.', 'warn');
}
