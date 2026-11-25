/* ============================================================
   TTS STUDIO + AI CONTROLLED PLAYBACK
   ============================================================ */
function updateTtsCounter() {
    var el = $('tts-text');
    var cnt = $('tts-char-count');
    if (!el || !cnt) return;
    var len = el.value.length;
    cnt.textContent = len + ' / 5000 karakter';
    cnt.classList.toggle('warn', len >= 4500);
}

function updateTtsSliders() {
    var rateEl = $('tts-rate');
    var pitchEl = $('tts-pitch');
    if (!rateEl || !pitchEl) return;
    var r = Number(rateEl.value || 0);
    var p = Number(pitchEl.value || 0);
    if ($('tts-rate-val')) $('tts-rate-val').textContent = (r >= 0 ? '+' : '') + r + '%';
    if ($('tts-pitch-val')) $('tts-pitch-val').textContent = (p >= 0 ? '+' : '') + p + 'Hz';
}

function resetTtsSliders() {
    if ($('tts-rate')) $('tts-rate').value = 0;
    if ($('tts-pitch')) $('tts-pitch').value = 0;
    updateTtsSliders();
    sfx('click');
}

function clearTTS() {
    if ($('tts-text')) $('tts-text').value = '';
    updateTtsCounter();
    sfx('click');
}

function stopAllAudioPlayback() {
    var p = $('player');
    if (p && typeof p.pause === 'function') {
        try { p.pause(); p.currentTime = 0; } catch (e) {}
    }
    document.querySelectorAll('#chat-box audio, #media-result-box audio').forEach(function (a) {
        try { a.pause(); a.currentTime = 0; } catch (e) {}
    });
}

function syncTtsControlsFromAction(res) {
    if (!res) return;
    if (res.voice && $('tts-voice')) $('tts-voice').value = res.voice;
    if (res.rate && $('tts-rate')) {
        var rv = parseInt(String(res.rate).replace('%', ''), 10);
        if (!isNaN(rv)) $('tts-rate').value = String(Math.max(-40, Math.min(60, rv)));
    }
    if (res.pitch && $('tts-pitch')) {
        var pv = parseInt(String(res.pitch).replace(/hz/i, ''), 10);
        if (!isNaN(pv)) $('tts-pitch').value = String(Math.max(-20, Math.min(20, pv)));
    }
    if (res.text && $('tts-text')) {
        $('tts-text').value = res.text;
        updateTtsCounter();
    }
    updateTtsSliders();
}

function handleTtsControlAction(res) {
    syncTtsControlsFromAction(res);
    var cmd = String(res.command || 'settings').toLowerCase();
    if (cmd === 'stop' || cmd === 'pause') {
        stopAllAudioPlayback();
        showToast('Sesli okuma durduruldu.', 'info');
        return;
    }
    if (cmd === 'play') {
        var p = $('player');
        if (res.text) {
            generateAudio();
            return;
        }
        if (p && p.src && p.src !== window.location.href) {
            p.play().catch(function () {});
            showToast('Ses oynatılıyor.', 'success');
            return;
        }
        if ($('tts-text') && $('tts-text').value.trim()) {
            generateAudio();
            return;
        }
    }
    showToast('Ses Stüdyosu ayarları güncellendi.', 'success');
}

async function generateAudio() {
    var text = $('tts-text').value.trim();
    var voice = $('tts-voice').value;
    var rateVal = Number($('tts-rate').value || 0);
    var pitchVal = Number($('tts-pitch').value || 0);
    var rate = (rateVal >= 0 ? '+' : '') + rateVal + '%';
    var pitch = (pitchVal >= 0 ? '+' : '') + pitchVal + 'Hz';
    var btn = $('btn-speak');
    if (!text) { showToast('Lütfen seslendirilecek metni yazın!', 'warn'); sfx('error'); return; }

    btn.disabled = true;
    btn.innerText = 'İşleniyor...';
    try {
        var res = await apiFetch('/api/tts', {
            method: 'POST',
            body: JSON.stringify({ text: text, voice: voice, rate: rate, pitch: pitch })
        });
        if (!res.ok) {
            var errData = await parseApiResponse(res);
            if (res.status === 401 || errData.auth_required) {
                handleAuthRequired(errData.error);
                return;
            }
            throw new Error(errData.error || ('Aslan AI ses işlemi tamamlanamadı (' + res.status + ').'));
        }
        var blob = await res.blob();
        if (state.lastAudioUrl) {
            try { URL.revokeObjectURL(state.lastAudioUrl); } catch (e) {}
        }
        var url = URL.createObjectURL(blob);
        state.lastAudioUrl = url;
        $('player').src = url;
        $('dl-link').href = url;
        $('tts-engine-badge').textContent = 'Aslan AI';
        showEl($('audio-box'), true);
        $('player').play().catch(function () {});
        if (typeof registerSessionMediaBlob === 'function') {
            registerSessionMediaBlob(blob, 'aslan-ses.mp3', 'audio/mpeg');
        }
        sfx('success');
        showToast('Ses Aslan AI tarafından oluşturuldu.', 'success');
    } catch (e) {
        sfx('error');
        showToast(e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerText = 'Sesi Oluştur ve Çal';
    }
}

function copyLink() {
    var url = $('dl-link').href;
    if (!url || url === '#') return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(
            function () { sfx('success'); showToast('Ses bağlantısı panoya kopyalandı!', 'success'); },
            function () { showToast('Bağlantı kopyalanamadı.', 'error'); }
        );
    } else {
        showToast('Panoya kopyalama desteklenmiyor.', 'warn');
    }
}
