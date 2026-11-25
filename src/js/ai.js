/* ============================================================
   SAFE MARKDOWN RENDERER, SESSION MEDIA CONTEXT, STT & ASLAN AI CHAT
   ============================================================ */
function renderSafeMarkdown(rawText) {
    var text = String(rawText == null ? '' : rawText);
    var codeBlocks = [];
    var withPlaceholders = text.replace(/```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g, function (_, lang, code) {
        var idx = codeBlocks.length;
        codeBlocks.push({ lang: lang || 'kod', code: code.replace(/\n$/, '') });
        return '@@CODEBLOCK_' + idx + '@@';
    });

    var safe = esc(withPlaceholders);
    safe = safe.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');
    safe = safe.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    safe = safe.replace(/(https?:\/\/[^\s<>"']+)/g, function (rawUrl) {
        var cleanUrl = rawUrl.replace(/[.,;:!?)]+$/, '');
        var trailing = rawUrl.slice(cleanUrl.length);
        return '<a href="' + cleanUrl + '" target="_blank" rel="noopener noreferrer">' + cleanUrl + '</a>' + trailing;
    });

    var lines = safe.split('\n');
    var out = '';
    var inList = false;
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        var m = line.match(/^@@CODEBLOCK_(\d+)@@$/);
        if (m) {
            if (inList) { out += '</ul>'; inList = false; }
            var blk = codeBlocks[Number(m[1])];
            out += '<div class="code-wrap"><div class="code-head"><span>' + esc(blk.lang) +
                   '</span><button type="button" class="code-copy-btn" data-action="copy-code">Kopyala</button></div>' +
                   '<pre><code>' + esc(blk.code) + '</code></pre></div>';
            continue;
        }
        var li = line.match(/^\s*[-*]\s+(.*)$/);
        if (li) {
            if (!inList) { out += '<ul>'; inList = true; }
            out += '<li>' + li[1] + '</li>';
        } else {
            if (inList) { out += '</ul>'; inList = false; }
            if (line.trim()) out += '<p>' + line + '</p>';
        }
    }
    if (inList) out += '</ul>';
    return '<div class="md-body">' + (out || '<p>' + esc(text) + '</p>') + '</div>';
}

function copyCodeBlock(btn) {
    var wrap = btn ? btn.closest('.code-wrap') : null;
    var codeEl = wrap ? wrap.querySelector('code') : null;
    if (!codeEl) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(codeEl.textContent).then(function () {
            showToast('Kod panoya kopyalandı.', 'success');
        });
    }
}

/* ---------------- OTURUM MEDYA BAGLAMI ---------------- */
function registerSessionMedia(entry) {
    if (!entry || (!entry.data_url && !entry.download_url)) return;
    var name = String(entry.name || 'medya').trim();
    var mime = String(entry.mime_type || 'application/octet-stream');
    var item = {
        id: 'ctx_' + Date.now() + '_' + Math.random().toString(16).slice(2, 6),
        name: name,
        mime_type: mime,
        data_url: entry.data_url || '',
        download_url: entry.download_url || '',
        size: Number(entry.size || 0)
    };
    state.sessionMedia.push(item);
    if (state.sessionMedia.length > 6) {
        state.sessionMedia = state.sessionMedia.slice(-6);
    }
    renderSessionMediaBar();
}

function registerSessionMediaBlob(blob, name, mimeType) {
    if (!blob) return;
    try {
        var reader = new FileReader();
        reader.onload = function () {
            var dataUrl = String(reader.result || '');
            if (dataUrl) {
                registerSessionMedia({
                    name: name || 'ses.mp3',
                    mime_type: mimeType || blob.type || 'audio/mpeg',
                    data_url: dataUrl,
                    size: blob.size || 0
                });
            }
        };
        reader.readAsDataURL(blob);
    } catch (e) {}
}

function renderSessionMediaBar() {
    var bar = $('session-media-bar');
    var chips = $('session-media-chips');
    if (!bar || !chips) return;
    chips.innerHTML = '';
    if (!state.sessionMedia.length) {
        showEl(bar, false);
        return;
    }
    state.sessionMedia.forEach(function (item) {
        var chip = document.createElement('span');
        chip.className = 'session-chip';
        chip.title = item.name;
        chip.textContent = fileIcon(item.mime_type) + ' ' + item.name;
        chips.appendChild(chip);
    });
    showEl(bar, true);
}

function clearSessionMedia() {
    state.sessionMedia = [];
    renderSessionMediaBar();
    sfx('click');
}

function buildContextFilesPayload() {
    var out = [];
    var totalChars = 0;
    var maxChars = 2800000;
    for (var i = state.sessionMedia.length - 1; i >= 0; i--) {
        var item = state.sessionMedia[i];
        var durl = String(item.data_url || '');
        var dlUrl = String(item.download_url || '');
        var rawB64 = '';
        if (durl.indexOf('data:') === 0 && durl.indexOf(',') !== -1) {
            rawB64 = durl.split(',', 2)[1] || '';
        }
        if (rawB64 && (totalChars + rawB64.length) <= maxChars) {
            totalChars += rawB64.length;
            out.unshift({
                name: item.name,
                mime_type: item.mime_type,
                data: rawB64
            });
        } else if (dlUrl) {
            out.unshift({
                name: item.name,
                mime_type: item.mime_type,
                source_url: dlUrl
            });
        }
        if (out.length >= 4) break;
    }
    return out;
}

/* ---------------- DOSYA EKLERI ---------------- */
function handleFileSelect(input) {
    var files = Array.prototype.slice.call(input.files || []);
    if (!files.length) return;
    files.forEach(function (f) {
        if (f.size > SERVERLESS_INLINE_WARN_BYTES) {
            showToast(f.name + ' (' + fmtSize(f.size) + ') 3 MB üzerinde; yükleme boyut sınırına takılabilir.', 'warn');
        }
        state.attachments.push(f);
    });
    input.value = '';
    renderAttachTray();
    sfx('ready');
}

function renderAttachTray() {
    state.attachPreviewUrls.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (e) {} });
    state.attachPreviewUrls = [];
    var tray = $('attach-tray');
    if (!tray) return;
    tray.innerHTML = '';
    state.attachments.forEach(function (f, i) {
        var item = document.createElement('div');
        item.className = 'attach-item';
        var t = f.type || '';
        if (t.indexOf('image') === 0) {
            var img = document.createElement('img');
            var u = URL.createObjectURL(f);
            state.attachPreviewUrls.push(u);
            img.src = u;
            img.alt = f.name;
            item.appendChild(img);
        } else {
            var sp = document.createElement('div');
            sp.className = 'thumb';
            sp.textContent = fileIcon(t);
            item.appendChild(sp);
        }
        var lbl = document.createElement('div');
        lbl.textContent = f.name + ' (' + fmtSize(f.size) + ')';
        lbl.title = f.name;
        item.appendChild(lbl);
        var rm = document.createElement('button');
        rm.className = 'rm';
        rm.type = 'button';
        rm.textContent = '×';
        rm.addEventListener('click', function () {
            state.attachments.splice(i, 1);
            renderAttachTray();
            sfx('click');
        });
        item.appendChild(rm);
        tray.appendChild(item);
    });
    tray.classList.toggle('active', state.attachments.length > 0);
}

function appendMsg(text, type, extraNode) {
    var box = $('chat-box');
    if (!box) return null;
    var div = document.createElement('div');
    div.className = 'msg ' + (type || 'bot');
    if (type === 'bot') {
        div.innerHTML = renderSafeMarkdown(text || '');
        var actBar = document.createElement('div');
        actBar.className = 'msg-actions';
        var copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'msg-act-btn';
        copyBtn.textContent = '📋 Kopyala';
        copyBtn.addEventListener('click', function () {
            if (navigator.clipboard) {
                navigator.clipboard.writeText(text || '').then(function () {
                    showToast('Yanıt kopyalandı.', 'success');
                });
            }
        });
        var speakBtn = document.createElement('button');
        speakBtn.type = 'button';
        speakBtn.className = 'msg-act-btn';
        speakBtn.textContent = '🔊 Seslendir';
        speakBtn.addEventListener('click', function () {
            $('tts-text').value = (text || '').slice(0, 5000);
            updateTtsCounter();
            switchTab('tts');
            generateAudio();
        });
        actBar.appendChild(copyBtn);
        actBar.appendChild(speakBtn);
        div.appendChild(actBar);
    } else {
        div.textContent = text == null ? '' : String(text);
    }
    if (extraNode && extraNode.nodeType === 1) {
        div.appendChild(extraNode);
    }
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return div;
}

function clearChat() {
    state.chatHistory = [];
    stopWaStatusPolling();
    if ($('chat-box')) {
        $('chat-box').innerHTML = '<div class="msg bot"><div class="md-body"><p>Sohbet temizlendi. Yeni bir konuyla devam edebilirsiniz! 🤖</p></div></div>';
    }
    sfx('click');
}

function updateQuota(remaining) {
    if (state.quota.ai) {
        state.quota.ai.remaining = remaining;
        if (typeof state.quota.ai.used === 'number') {
            state.quota.ai.used = Math.max(0, CHAT_MAX - remaining);
        }
    }
    if ($('quota-display')) $('quota-display').textContent = remaining + '/' + CHAT_MAX;
}

function toggleSubPanel(id) {
    var el = $(id);
    if (!el) return;
    el.classList.toggle('open');
    sfx('click');
}

function appendImageResult(res) {
    if (!res) return;
    var box = $('chat-box');
    var div = document.createElement('div');
    div.className = 'msg bot';
    var rawSrc = res.data_url || (res.image_base64 ? ('data:' + (res.mime_type || 'image/png') + ';base64,' + res.image_base64) : '');
    var src = safeMediaUrl(rawSrc);
    if (res.success && src) {
        var dlName = res.output_file || 'aslan-gorsel.png';
        registerSessionMedia({
            name: dlName,
            mime_type: res.mime_type || 'image/png',
            data_url: src,
            download_url: res.download_url || ''
        });
        div.innerHTML =
            '<div>🎨 <strong>Görsel üretildi</strong></div>' +
            '<img src="' + esc(src) + '" alt="Üretilen görsel" class="media-preview">' +
            '<div class="media-card">' +
                '<div class="mc-icon">🖼️</div>' +
                '<div class="mc-info">' +
                    '<div class="mc-name">' + esc(res.prompt || dlName) + '</div>' +
                    '<div class="mc-size">Aslan AI</div>' +
                '</div>' +
                '<a class="download-btn" href="' + esc(src) + '" download="' + esc(dlName) + '">⬇️ İndir</a>' +
            '</div>';
        sfx('success');
    } else {
        div.innerHTML = '<div>⚠️ Görsel üretilemedi: ' + esc(res.error || 'geçersiz medya kaynağı') + '</div>';
        sfx('error');
    }
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
}

function openImageStudio() {
    var box = $('image-studio');
    if (!box) return;
    var open = box.classList.toggle('open');
    if (open && $('image-prompt')) $('image-prompt').focus();
    sfx('click');
}

async function generateImage() {
    var promptEl = $('image-prompt');
    var prompt = promptEl ? promptEl.value.trim() : '';
    if (!prompt) { sfx('error'); showToast('Görsel için bir açıklama yazın.', 'warn'); return; }
    var btn = $('btn-generate-image');
    btn.disabled = true;
    btn.innerText = 'Üretiliyor...';
    appendMsg('🎨 Görsel üretiliyor: ' + prompt, 'user');
    try {
        var res = await apiFetch('/api/image', {
            method: 'POST',
            body: JSON.stringify({ prompt: prompt })
        });
        var data = await parseApiResponse(res);
        if (res.status === 401 || data.auth_required) {
            handleAuthRequired(data.error);
            return;
        }
        if (data.quota) applyQuotaSnapshot(data.quota);
        if (!res.ok) throw new Error(data.error || 'Görsel üretilemedi');
        appendImageResult(data);
        promptEl.value = '';
    } catch (e) {
        appendMsg('⚠️ Görsel üretilemedi: ' + e.message, 'error');
        sfx('error');
    } finally {
        renderQuota();
        if (!btn.disabled) btn.innerText = '🎨 Görsel Üret';
    }
}

/* ---------------- ASLAN AI WHATSAPP BOT EŞLEŞTİRME & DURUM ---------------- */
var waPollTimer = null;

function stopWaStatusPolling() {
    if (waPollTimer) {
        clearInterval(waPollTimer);
        waPollTimer = null;
    }
}

function _copyPlainText(text, onDone) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(onDone).catch(function () {
            _legacyCopy(text, onDone);
        });
    } else {
        _legacyCopy(text, onDone);
    }
}

function _legacyCopy(text, onDone) {
    try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.className = 'is-hidden';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        if (onDone) onDone();
    } catch (e) {}
}

function copyWaCode(btn) {
    var code = btn ? (btn.getAttribute('data-code') || '') : '';
    if (!code) return;
    _copyPlainText(code, function () {
        var old = btn.textContent;
        btn.textContent = '✓ Kopyalandı';
        showToast('8 haneli eşleştirme kodu kopyalandı.', 'success');
        sfx('success');
        setTimeout(function () { btn.textContent = old; }, 2200);
    });
}

async function confirmWaConnected(btn) {
    if (btn) btn.disabled = true;
    try {
        var res = await apiFetch('/api/wa/confirm-connected', { method: 'POST', body: '{}' });
        var data = await parseApiResponse(res);
        if (!res.ok || !data.success) {
            throw new Error(data.error || 'Bağlantı onayı tamamlanamadı.');
        }
        if (data.reply) {
            appendMsg(data.reply, 'bot');
            sfx('receive');
        }
    } catch (e) {
        if (btn) btn.disabled = false;
        showToast(e.message, 'warn');
        sfx('error');
    }
}

function markWaCardConnected() {
    stopWaStatusPolling();
    document.querySelectorAll('.wa-status[data-wa-pending="1"]').forEach(function (el) {
        el.setAttribute('data-wa-pending', '0');
        el.classList.add('connected');
        el.textContent = '✅ Bağlantınız başarılı';
    });
    document.querySelectorAll('.wa-confirm-btn.is-hidden').forEach(function (btn) {
        btn.classList.remove('is-hidden');
    });
    showToast('Bağlantınız başarılı!', 'success');
    sfx('success');
}

function startWaStatusPolling() {
    stopWaStatusPolling();
    waPollTimer = setInterval(async function () {
        if (!state.authToken) {
            stopWaStatusPolling();
            return;
        }
        try {
            var res = await apiFetch('/api/wa/status', { method: 'GET' });
            if (!res.ok) return;
            var data = await res.json();
            if (data && data.connected) {
                markWaCardConnected();
            }
        } catch (e) {}
    }, 3000);
}

function appendWaEventCard(ev) {
    if (!ev || typeof ev !== 'object') return;
    var box = $('chat-box');
    if (!box) return;

    if (ev.type === 'pairing_code' && ev.code) {
        var wrap = document.createElement('div');
        wrap.className = 'msg bot';
        var rawCode = String(ev.code || '');
        var fmtCode = String(ev.formatted_code || rawCode);
        wrap.innerHTML =
            '<div class="wa-card">' +
                '<div class="wa-card-head">🔐 Aslan AI Eşleştirme Kodu</div>' +
                '<div class="wa-pair-row">' +
                    '<code class="wa-pair-code">' + esc(fmtCode) + '</code>' +
                    '<button type="button" class="wa-copy-btn" data-action="copy-wa-code" data-code="' + esc(rawCode) + '">📋 Kodu Kopyala</button>' +
                '</div>' +
                '<ol class="wa-steps">' +
                    '<li>Telefonunuzda <strong>WhatsApp &gt; Bağlı Cihazlar &gt; Cihaz Bağla &gt; Telefon numarasıyla bağla</strong> adımını açın.</li>' +
                    '<li>Yukarıdaki 8 haneli kodu yapıştırıp onaylayın.</li>' +
                '</ol>' +
                '<div class="wa-status" data-wa-pending="1">⏳ Bağlantı onayı bekleniyor...</div>' +
                '<button type="button" class="action-btn success small wa-confirm-btn is-hidden" data-action="confirm-wa-connected">✅ Onayla ve Botu Özelleştir</button>' +
            '</div>';
        box.appendChild(wrap);
        box.scrollTop = box.scrollHeight;
        startWaStatusPolling();
        return;
    }

    if (ev.type === 'connected') {
        var cWrap = document.createElement('div');
        cWrap.className = 'msg bot';
        cWrap.innerHTML =
            '<div class="wa-card">' +
                '<div class="wa-status connected">✅ Bağlantınız başarılı</div>' +
                '<button type="button" class="action-btn success small wa-confirm-btn" data-action="confirm-wa-connected">✅ Onayla ve Botu Özelleştir</button>' +
            '</div>';
        box.appendChild(cWrap);
        box.scrollTop = box.scrollHeight;
        sfx('success');
        return;
    }

    if ((ev.type === 'bot_ready' || ev.type === 'config_updated') && ev.config_summary) {
        var s = ev.config_summary;
        var sWrap = document.createElement('div');
        sWrap.className = 'msg bot';
        var tgs = Array.isArray(s.trigger_keywords) && s.trigger_keywords.length
            ? s.trigger_keywords.join(', ')
            : 'Tüm mesajlar';
        sWrap.innerHTML =
            '<div class="wa-card">' +
                '<div class="wa-card-head">' + (s.enabled ? '🤖 Aslan AI WhatsApp Botu Aktif' : '⏸️ Aslan AI WhatsApp Botu Durduruldu') + '</div>' +
                '<div class="wa-summary-list">' +
                    '<span>Özel: ' + (s.allow_private ? 'Açık' : 'Kapalı') + '</span> · ' +
                    '<span>Grup: ' + (s.allow_groups ? 'Açık' : 'Kapalı') + '</span> · ' +
                    '<span>Gecikme: ' + esc(String(s.typing_delay_seconds)) + ' sn</span> · ' +
                    '<span>Yazıyor: ' + (s.show_typing ? 'Açık' : 'Kapalı') + '</span> · ' +
                    '<span>Tetikleyici: ' + esc(tgs) + '</span>' +
                '</div>' +
            '</div>';
        box.appendChild(sWrap);
        box.scrollTop = box.scrollHeight;
    }
}

function buildMediaCardHtml(res) {
    if (!res || !res.success) {
        return '<div>⚠️ İşlem tamamlanamadı: ' + esc((res && res.error) || 'bilinmeyen hata') + '</div>';
    }
    var label = esc(res.label || res.operation || 'Medya İşlemi');
    var mime = res.mime_type || '';
    var isAudio = mime.indexOf('audio') === 0;
    var isVideo = mime.indexOf('video') === 0;
    var isImage = mime.indexOf('image') === 0;
    var rawSrc = res.data_url || (res.image_base64 ? ('data:' + (mime || 'image/png') + ';base64,' + res.image_base64) : '') || res.download_url || '';
    var src = safeMediaUrl(rawSrc);
    var dlName = esc(res.output_file || (isImage ? 'aslan-gorsel.png' : isAudio ? 'aslan-ses.mp3' : 'aslan-video.mp4'));
    var preview = '';
    if (src) {
        if (isAudio) {
            preview = '<audio controls preload="none" src="' + esc(src) + '" class="audio-embed"></audio>';
        } else if (isImage) {
            preview = '<img src="' + esc(src) + '" alt="' + label + '" class="media-preview">';
        } else if (isVideo) {
            preview = '<video controls preload="none" src="' + esc(src) + '" class="media-preview"></video>';
        }
    }
    var sum = (res.summary && res.summary.duration) ? (' · ' + res.summary.duration + 's') : '';
    return '<div>✅ <strong>' + label + '</strong> tamamlandı.</div>' + preview +
        '<div class="media-card">' +
            '<div class="mc-icon">' + (isAudio ? '🎵' : isImage ? '🖼️' : '🎬') + '</div>' +
            '<div class="mc-info">' +
                '<div class="mc-name">' + dlName + '</div>' +
                '<div class="mc-size">' + fmtSize(res.size) + esc(sum) + '</div>' +
            '</div>' +
            (src ? '<a class="download-btn" href="' + esc(src) + '" download="' + dlName + '">⬇️ İndir</a>' : '') +
        '</div>';
}

function appendMediaResult(res) {
    if (!res) return;
    if (res.operation === 'tts_control') {
        handleTtsControlAction(res);
        return;
    }
    if (res.operation === 'tts' && res.success) {
        syncTtsControlsFromAction(res);
        var ttsSrc = safeMediaUrl(res.data_url || res.download_url || '');
        if (ttsSrc && $('player')) {
            $('player').src = ttsSrc;
            if ($('dl-link')) $('dl-link').href = ttsSrc;
            if ($('audio-box')) showEl($('audio-box'), true);
            if (res.autoplay !== false) $('player').play().catch(function () {});
        }
    }
    if (res.success) {
        var mediaSrc = safeMediaUrl(res.data_url || '');
        var dlUrl = safeMediaUrl(res.download_url || '');
        if (mediaSrc || dlUrl) {
            registerSessionMedia({
                name: res.output_file || (res.operation + '.mp4'),
                mime_type: res.mime_type || 'application/octet-stream',
                data_url: mediaSrc,
                download_url: dlUrl,
                size: res.size || 0
            });
        }
    }
    var box = $('chat-box');
    var div = document.createElement('div');
    div.className = 'msg bot';
    div.innerHTML = buildMediaCardHtml(res);
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    sfx(res.success ? 'success' : 'error');
}

function runAICommand(cmd) {
    var input = $('ai-input');
    if (!input) return;
    input.value = cmd;
    sfx('click');
    sendAI();
}

async function sendAI() {
    var input = $('ai-input');
    var btn = $('btn-ai');
    var text = input.value.trim();
    var pending = state.attachments.slice();

    if (!text && !pending.length) { sfx('error'); return; }

    var chipsNode = null;
    if (pending.length) {
        chipsNode = document.createElement('div');
        chipsNode.className = 'msg-files';
        pending.forEach(function (f) {
            var sp = document.createElement('span');
            sp.className = 'chip';
            sp.textContent = fileIcon(f.type) + ' ' + f.name;
            chipsNode.appendChild(sp);
        });
    }
    appendMsg(text || '(dosya gönderildi)', 'user', chipsNode);
    input.value = '';
    state.attachments = [];
    renderAttachTray();

    btn.disabled = true;
    btn.innerText = 'Düşünüyor...';
    sfx('send');

    var loadingId = 'ld-' + Date.now();
    var loading = document.createElement('div');
    loading.className = 'msg bot';
    loading.id = loadingId;
    loading.innerHTML = '<span class="loading"></span><span class="loading"></span><span class="loading"></span>';
    $('chat-box').appendChild(loading);
    $('chat-box').scrollTop = $('chat-box').scrollHeight;

    try {
        var inlineFiles = [];
        if (pending.length) {
            try { inlineFiles = await filesToInline(pending); }
            catch (e) { appendMsg('Dosya okunamadı: ' + e.message, 'error'); }
        }
        var contextFiles = buildContextFilesPayload();

        var res = await apiFetch('/api/ai', {
            method: 'POST',
            body: JSON.stringify({
                message: text,
                files: inlineFiles,
                context_files: contextFiles,
                history: state.chatHistory.slice(-10)
            })
        });
        var data = await parseApiResponse(res);
        var ld = $(loadingId);
        if (ld) ld.remove();

        if (res.status === 401 || data.auth_required) {
            handleAuthRequired(data.error);
            return;
        }
        if (!res.ok) {
            appendMsg(data.error || 'Bir hata oluştu', 'error');
            if (data.quota) applyQuotaSnapshot(data.quota);
            sfx('error');
        } else {
            appendMsg(data.reply, 'bot');
            sfx('receive');
            if (text) state.chatHistory.push({ role: 'user', text: text });
            if (data.reply) state.chatHistory.push({ role: 'model', text: data.reply });
            if (state.chatHistory.length > 20) state.chatHistory = state.chatHistory.slice(-20);

            if (data.quota) {
                applyQuotaSnapshot(data.quota);
            } else if (typeof data.remaining === 'number') {
                updateQuota(data.remaining);
            }

            var genImgs = Array.isArray(data.generated_images) ? data.generated_images : [];
            if (genImgs.length > 1) {
                genImgs.forEach(function (imgItem, idx) {
                    setTimeout(function () { appendImageResult(imgItem); }, 150 * (idx + 1));
                });
            }
            if (data.image_result && genImgs.length <= 1) {
                setTimeout(function () { appendImageResult(data.image_result); }, 200);
            } else if (data.media_result && (!data.image_result || genImgs.length > 1)) {
                setTimeout(function () { appendMediaResult(data.media_result); }, 250 + genImgs.length * 120);
            }
            if (data.wa_event) {
                setTimeout(function () { appendWaEventCard(data.wa_event); }, 120);
            }
        }
    } catch (e) {
        var ld2 = $(loadingId);
        if (ld2) ld2.remove();
        appendMsg('Bağlantı hatası: ' + e.message, 'error');
        sfx('error');
    } finally {
        btn.disabled = false;
        btn.innerText = 'Gönder';
    }
}

/* ---------------- SESLI MESAJDAN METNE ---------------- */
var speechRec = null;
var isListeningSpeech = false;
var mediaRecorder = null;
var recordedChunks = [];

function _setMicState(active) {
    var btn = $('btn-mic');
    if (!btn) return;
    btn.classList.toggle('recording', !!active);
    btn.classList.toggle('listening', !!active);
}

async function _transcribeBlobAndSend(blob) {
    var input = $('ai-input');
    var btn = $('btn-mic');
    if (!blob || blob.size < 100) {
        showToast('Ses kaydı alınamadı.', 'warn');
        return;
    }
    if (btn) btn.disabled = true;
    try {
        var b64 = await new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () {
                var r = String(reader.result || '');
                var c = r.indexOf(',');
                resolve(c >= 0 ? r.slice(c + 1) : r);
            };
            reader.onerror = function () { reject(new Error('Ses okunamadı.')); };
            reader.readAsDataURL(blob);
        });
        var res = await apiFetch('/api/stt', {
            method: 'POST',
            body: JSON.stringify({
                audio_base64: b64,
                mime_type: blob.type || 'audio/webm',
                lang: 'tr-TR'
            })
        });
        var data = await parseApiResponse(res);
        if (res.status === 401 || data.auth_required) {
            handleAuthRequired(data.error);
            return;
        }
        if (!res.ok || !data.text) {
            throw new Error(data.error || 'Ses metne dönüştürülemedi.');
        }
        if (input) {
            input.value = String(data.text).trim();
        }
        if (input && input.value.trim()) {
            sendAI();
        }
    } catch (e) {
        showToast(e.message, 'error');
        sfx('error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function _startFallbackRecorder() {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
        showToast('Tarayıcınız sesli mesaj dönüşümünü desteklemiyor.', 'warn');
        return;
    }
    try {
        var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        recordedChunks = [];
        mediaRecorder = new MediaRecorder(stream);
        mediaRecorder.ondataavailable = function (e) { if (e.data && e.data.size) recordedChunks.push(e.data); };
        mediaRecorder.onstop = function () {
            stream.getTracks().forEach(function (t) { t.stop(); });
            _setMicState(false);
            var blob = new Blob(recordedChunks, { type: 'audio/webm' });
            _transcribeBlobAndSend(blob);
        };
        mediaRecorder.start();
        _setMicState(true);
        sfx('click');
        showToast('Dinleniyor... Bitirmek için mikrofona tekrar dokunun.', 'info');
    } catch (e) {
        _setMicState(false);
        showToast('Mikrofon erişimi reddedildi: ' + e.message, 'error');
        sfx('error');
    }
}

function toggleRecord() {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
        mediaRecorder.stop();
        _setMicState(false);
        return;
    }
    if (isListeningSpeech && speechRec) {
        try { speechRec.stop(); } catch (e) {}
        return;
    }

    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
        _startFallbackRecorder();
        return;
    }

    var input = $('ai-input');
    var baseText = input ? input.value.trim() : '';
    var finalTranscript = '';

    try {
        speechRec = new SR();
        speechRec.lang = 'tr-TR';
        speechRec.interimResults = true;
        speechRec.continuous = false;

        speechRec.onstart = function () {
            isListeningSpeech = true;
            _setMicState(true);
            sfx('click');
        };

        speechRec.onresult = function (ev) {
            var interim = '';
            var finalChunk = '';
            for (var i = ev.resultIndex; i < ev.results.length; i++) {
                var tr = ev.results[i][0] ? ev.results[i][0].transcript : '';
                if (ev.results[i].isFinal) {
                    finalChunk += tr;
                } else {
                    interim += tr;
                }
            }
            if (finalChunk) {
                finalTranscript = (finalTranscript ? (finalTranscript + ' ') : '') + finalChunk.trim();
            }
            var combined = [baseText, finalTranscript, interim.trim()].filter(Boolean).join(' ').trim();
            if (input) input.value = combined;
        };

        speechRec.onerror = function (ev) {
            var errCode = ev && ev.error ? String(ev.error) : '';
            isListeningSpeech = false;
            _setMicState(false);
            if (errCode === 'not-allowed' || errCode === 'permission-denied') {
                showToast('Mikrofon izni verilmedi.', 'error');
                sfx('error');
                return;
            }
            if (errCode === 'service-not-allowed' || errCode === 'network' || errCode === 'language-not-supported') {
                _startFallbackRecorder();
                return;
            }
            if (errCode && errCode !== 'aborted' && errCode !== 'no-speech') {
                showToast('Ses algılama hatası: ' + errCode, 'warn');
            }
        };

        speechRec.onend = function () {
            var wasListening = isListeningSpeech;
            isListeningSpeech = false;
            _setMicState(false);
            if (!wasListening) return;
            var currentText = input ? input.value.trim() : '';
            if (currentText && (finalTranscript || currentText !== baseText)) {
                sendAI();
            }
        };

        speechRec.start();
    } catch (e) {
        isListeningSpeech = false;
        _setMicState(false);
        _startFallbackRecorder();
    }
}

function initChatDragDrop() {
    var chatBox = $('chat-box');
    if (!chatBox) return;
    chatBox.addEventListener('dragover', function (e) {
        e.preventDefault();
        e.currentTarget.style.outline = '2px dashed #ef4444';
    });
    chatBox.addEventListener('dragleave', function (e) {
        e.currentTarget.style.outline = '';
    });
    chatBox.addEventListener('drop', function (e) {
        e.preventDefault();
        e.currentTarget.style.outline = '';
        var files = Array.prototype.slice.call(e.dataTransfer.files || []);
        files.forEach(function (f) { state.attachments.push(f); });
        renderAttachTray();
        sfx('ready');
    });
}
