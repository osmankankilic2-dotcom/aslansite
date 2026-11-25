/* ============================================================
   AI PROVIDER, CUSTOM ENDPOINT & ACCOUNT MANAGEMENT SETTINGS
   ============================================================ */
var DEFAULT_PROVIDER_FALLBACK = [
    { id: 'default', label: 'Aslan AI (Varsayılan Ücretsiz)', default_model: '', requires_key: false, requires_endpoint: false, model_placeholder: 'Otomatik ücretsiz akıllı zincir' },
    { id: 'gemini', label: 'Google Gemini', default_model: 'gemini-4-hydrogen', requires_key: true, requires_endpoint: false, model_placeholder: 'gemini-4-hydrogen' },
    { id: 'custom', label: 'Özel Sağlayıcı / Özel Uç Nokta (Custom Endpoint)', default_model: '', requires_key: false, requires_endpoint: true, model_placeholder: 'Özel model adı' }
];

function _findProviderSpec(providerId) {
    var pid = String(providerId || 'default').trim().toLowerCase();
    var list = (Array.isArray(state.aiProviders) && state.aiProviders.length)
        ? state.aiProviders
        : DEFAULT_PROVIDER_FALLBACK;
    for (var i = 0; i < list.length; i++) {
        if (list[i] && list[i].id === pid) return list[i];
    }
    return list[0];
}

function renderSettingsProviders(selectedId) {
    var sel = $('settings-ai-provider');
    if (!sel) return;
    var list = (Array.isArray(state.aiProviders) && state.aiProviders.length)
        ? state.aiProviders
        : DEFAULT_PROVIDER_FALLBACK;
    var currentVal = String(selectedId || sel.value || 'default').toLowerCase();
    sel.innerHTML = '';
    list.forEach(function (item) {
        if (!item || !item.id) return;
        var opt = document.createElement('option');
        opt.value = item.id;
        opt.textContent = item.label || item.id;
        sel.appendChild(opt);
    });
    sel.value = _findProviderSpec(currentVal).id;
}

function onSettingsProviderChange(preserveValues) {
    var sel = $('settings-ai-provider');
    var epWrap = $('settings-custom-endpoint-wrap');
    var keyWrap = $('settings-apikey-wrap');
    var modelEl = $('settings-ai-model');
    if (!sel) return;
    var spec = _findProviderSpec(sel.value);
    var isDefault = spec.id === 'default';
    var isCustom = spec.id === 'custom';

    showEl(epWrap, isCustom);
    showEl(keyWrap, !isDefault);
    if (modelEl) {
        modelEl.disabled = isDefault;
        modelEl.placeholder = spec.model_placeholder || 'Model adı';
        if (!preserveValues) {
            modelEl.value = isDefault ? '' : (spec.default_model || '');
        }
    }
}

function renderAiSettingsForm(settingsObj) {
    var st = settingsObj && typeof settingsObj === 'object' ? settingsObj : { provider: 'default' };
    state.aiSettings = st;
    renderSettingsProviders(st.provider || 'default');
    if ($('settings-ai-model')) $('settings-ai-model').value = st.model || '';
    if ($('settings-ai-endpoint')) $('settings-ai-endpoint').value = st.endpoint || '';
    if ($('settings-ai-apikey')) $('settings-ai-apikey').value = '';
    if ($('settings-clear-apikey')) $('settings-clear-apikey').checked = false;
    if ($('settings-apply-chat')) $('settings-apply-chat').checked = st.apply_to_chat !== false;
    if ($('settings-apply-wa')) $('settings-apply-wa').checked = st.apply_to_whatsapp !== false;

    var keyStatus = $('settings-apikey-status');
    if (keyStatus) {
        keyStatus.textContent = st.has_api_key
            ? ('Kayıtlı: ' + (st.api_key_masked || 'şifreli anahtar'))
            : 'Kayıtlı anahtar yok';
    }
    var syncBadge = $('settings-sync-status');
    if (syncBadge) {
        var spec = _findProviderSpec(st.provider || 'default');
        syncBadge.textContent = st.provider && st.provider !== 'default'
            ? (spec.label + (st.model ? (' · ' + st.model) : ''))
            : 'Aslan AI Varsayılan';
    }
    onSettingsProviderChange(true);
}

function resetLocalAiSettingsState() {
    state.aiSettings = null;
    state.aiProviders = [];
    renderAiSettingsForm({ provider: 'default', apply_to_chat: true, apply_to_whatsapp: true });
}

async function loadAiSettings() {
    if (!state.authToken) {
        resetLocalAiSettingsState();
        return;
    }
    try {
        var res = await apiFetch('/api/settings/ai', { method: 'GET' });
        if (!res.ok) return;
        var data = await res.json();
        if (Array.isArray(data.providers) && data.providers.length) {
            state.aiProviders = data.providers;
        }
        renderAiSettingsForm(data.settings || { provider: 'default' });
    } catch (e) {}
}

async function saveAiSettings(e) {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    if (!state.authToken) {
        handleAuthRequired('Ayarları kaydetmek için giriş yapmalısınız.');
        return;
    }
    var btn = $('btn-save-ai-settings');
    var provider = $('settings-ai-provider') ? $('settings-ai-provider').value : 'default';
    var model = $('settings-ai-model') ? $('settings-ai-model').value.trim() : '';
    var endpoint = $('settings-ai-endpoint') ? $('settings-ai-endpoint').value.trim() : '';
    var apiKey = $('settings-ai-apikey') ? $('settings-ai-apikey').value.trim() : '';
    var clearKey = !!($('settings-clear-apikey') && $('settings-clear-apikey').checked);
    var applyChat = !($('settings-apply-chat') && !$('settings-apply-chat').checked);
    var applyWa = !($('settings-apply-wa') && !$('settings-apply-wa').checked);

    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Kaydediliyor...';
    }
    try {
        var res = await apiFetch('/api/settings/ai', {
            method: 'POST',
            body: JSON.stringify({
                provider: provider,
                model: model,
                endpoint: endpoint,
                api_key: apiKey,
                clear_api_key: clearKey,
                apply_to_chat: applyChat,
                apply_to_whatsapp: applyWa
            })
        });
        var data = await parseApiResponse(res);
        if (res.status === 401 || data.auth_required) {
            handleAuthRequired(data.error);
            return;
        }
        if (!res.ok || !data.success) {
            throw new Error(data.error || 'Ayarlar kaydedilemedi.');
        }
        if (Array.isArray(data.providers) && data.providers.length) {
            state.aiProviders = data.providers;
        }
        renderAiSettingsForm(data.settings);
        sfx('success');
        showToast(
            data.wa_synced
                ? 'Ayarlar kaydedildi ve Aslan AI ile WhatsApp botuna otonom uygulandı.'
                : 'Yapay zeka ayarlarınız kaydedildi.',
            'success'
        );
    } catch (err) {
        sfx('error');
        showToast(err.message || 'Ayarlar kaydedilemedi.', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = '💾 Ayarları Kaydet ve Senkronize Et';
        }
    }
}

async function resetAiSettingsToDefault() {
    if ($('settings-ai-provider')) $('settings-ai-provider').value = 'default';
    if ($('settings-ai-model')) $('settings-ai-model').value = '';
    if ($('settings-ai-endpoint')) $('settings-ai-endpoint').value = '';
    if ($('settings-ai-apikey')) $('settings-ai-apikey').value = '';
    if ($('settings-clear-apikey')) $('settings-clear-apikey').checked = true;
    onSettingsProviderChange(false);
    await saveAiSettings();
}

function openDeleteAccountModal() {
    if (!state.authToken) {
        handleAuthRequired('Bu işlem için giriş yapmalısınız.');
        return;
    }
    if ($('delete-account-password')) $('delete-account-password').value = '';
    var modal = $('delete-account-modal');
    if (modal) modal.classList.add('open');
    sfx('click');
}

function closeDeleteAccountModal() {
    if ($('delete-account-password')) $('delete-account-password').value = '';
    var modal = $('delete-account-modal');
    if (modal) modal.classList.remove('open');
}

async function confirmDeleteAccount(e) {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    var pwEl = $('delete-account-password');
    var pw = pwEl ? pwEl.value : '';
    if (!pw) {
        showToast('Onay için mevcut şifrenizi girmelisiniz.', 'warn');
        if (pwEl) pwEl.focus();
        return;
    }
    var btn = $('btn-confirm-delete-account');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Hesap siliniyor...';
    }
    try {
        var res = await apiFetch('/api/auth/delete-account', {
            method: 'POST',
            body: JSON.stringify({ password: pw, confirm: true })
        });
        var data = await parseApiResponse(res);
        if (!res.ok || !data.success) {
            throw new Error(data.error || 'Hesap silme işlemi tamamlanamadı.');
        }
        closeDeleteAccountModal();
        saveStoredAuth(null, '');
        state.chatHistory = [];
        state.activeChatId = '';
        state.savedChats = [];
        if (typeof renderSavedChats === 'function') renderSavedChats();
        resetLocalAiSettingsState();
        sfx('click');
        showToast('Hesabınız ve tüm verileriniz kalıcı olarak silindi.', 'info');
    } catch (err) {
        sfx('error');
        showToast(err.message || 'Hesap silinemedi.', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🗑️ Hesabımı Kalıcı Olarak Sil';
        }
    }
}
