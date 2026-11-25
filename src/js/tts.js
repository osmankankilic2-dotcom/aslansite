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

var activeInlineAudio = null;
var activeSpeakBtn = null;

function stopWebSpeech() {
    try {
        if (window.speechSynthesis && typeof window.speechSynthesis.cancel === 'function') {
            window.speechSynthesis.cancel();
        }
    } catch (e) {}
}

function speakWithWebSpeech(text, voiceId, ratePct, pitchHz, onDone) {
    if (!window.speechSynthesis || typeof window.SpeechSynthesisUtterance === 'undefined') {
        if (onDone) onDone(false);
        return false;
    }
    try {
        stopWebSpeech();
        var utter = new window.SpeechSynthesisUtterance(String(text || '').slice(0, 4000));
        var isEn = String(voiceId || '').indexOf('en-') === 0;
        utter.lang = isEn ? 'en-US' : 'tr-TR';
        var rNum = Math.max(-40, Math.min(60, Number(ratePct || 0)));
        var pNum = Math.max(-20, Math.min(20, Number(pitchHz || 0)));
        utter.rate = Math.max(0.6, Math.min(1.6, 1 + rNum / 100));
        utter.pitch = Math.max(0.6, Math.min(1.4, 1 + pNum / 50));

        var voices = window.speechSynthesis.getVoices ? window.speechSynthesis.getVoices() : [];
        var wantedPrefix = isEn ? 'en' : 'tr';
        for (var i = 0; i < voices.length; i++) {
            if (voices[i] && voices[i].lang && voices[i].lang.toLowerCase().indexOf(wantedPrefix) === 0) {
                utter.voice = voices[i];
                break;
            }
        }
        utter.onend = function () { if (onDone) onDone(true); };
        utter.onerror = function () { if (onDone) onDone(false); };
        window.speechSynthesis.speak(utter);
        return true;
    } catch (e) {
        if (onDone) onDone(false);
        return false;
    }
}

function stopAllAudioPlayback() {
    stopWebSpeech();
    if (activeInlineAudio) {
        try { activeInlineAudio.pause(); activeInlineAudio.currentTime = 0; } catch (e) {}
        activeInlineAudio = null;
    }
    if (activeSpeakBtn) {
        activeSpeakBtn.textContent = '🔊 Seslendir';
        activeSpeakBtn = null;
    }
    var p = $('player');
    if (p && typeof p.pause === 'function') {
        try { p.pause(); p.currentTime = 0; } catch (e) {}
    }
    document.querySelectorAll('#chat-box audio, #media-result-box audio').forEach(function (a) {
        try { a.pause(); a.currentTime = 0; } catch (e) {}
    });
}

async function speakChatMessage(text, btn) {
    var clean = String(text || '').trim();
    if (!clean) return;
    if (btn && activeSpeakBtn === btn) {
        stopAllAudioPlayback();
        return;
    }
    stopAllAudioPlayback();
    if (btn) {
        activeSpeakBtn = btn;
        btn.textContent = '⏳ Hazırlanıyor...';
    }
    var voice = ($('tts-voice') && $('tts-voice').value) || 'tr-TR-AhmetNeural';
    var rateVal = Number(($('tts-rate') && $('tts-rate').value) || 0);
    var pitchVal = Number(($('tts-pitch') && $('tts-pitch').value) || 0);
    var rate = (rateVal >= 0 ? '+' : '') + rateVal + '%';
    var pitch = (pitchVal >= 0 ? '+' : '') + pitchVal + 'Hz';

    function resetBtn() {
        if (activeSpeakBtn === btn && btn) btn.textContent = '🔊 Seslendir';
        if (activeSpeakBtn === btn) activeSpeakBtn = null;
    }

    try {
        var res = await apiFetch('/api/tts', {
            method: 'POST',
            body: JSON.stringify({ text: clean.slice(0, 5000), voice: voice, rate: rate, pitch: pitch })
        });
        if (!res.ok) {
            var errData = await parseApiResponse(res);
            if (res.status === 401 || errData.auth_required) {
                resetBtn();
                handleAuthRequired(errData.error);
                return;
            }
            throw new Error(errData.error || 'Sunucu sesi alınamadı.');
        }
        var blob = await res.blob();
        if (!blob || blob.size < 100) throw new Error('Ses verisi boş.');
        if (state.lastAudioUrl) {
            try { URL.revokeObjectURL(state.lastAudioUrl); } catch (e) {}
        }
        var url = URL.createObjectURL(blob);
        state.lastAudioUrl = url;
        var audio = new Audio(url);
        activeInlineAudio = audio;
        if (btn) btn.textContent = '⏹️ Durdur';
        audio.onended = function () { activeInlineAudio = null; resetBtn(); };
        audio.onerror = function () {
            activeInlineAudio = null;
            if (!speakWithWebSpeech(clean, voice, rateVal, pitchVal, resetBtn)) {
                resetBtn();
                showToast('Ses oynatılamadı.', 'error');
            }
        };
        await audio.play();
    } catch (e) {
        if (btn) btn.textContent = '⏹️ Durdur';
        var spoke = speakWithWebSpeech(clean, voice, rateVal, pitchVal, function () { resetBtn(); });
        if (!spoke) {
            resetBtn();
            showToast(e.message || 'Seslendirme başlatılamadı.', 'error');
            sfx('error');
        }
    }
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
            if (speakWithWebSpeech(text, voice, rateVal, pitchVal)) {
                sfx('success');
                showToast('Ses cihaz seslendiricisi ile oynatılıyor.', 'info');
                return;
            }
            throw new Error(errData.error || ('Aslan AI ses işlemi tamamlanamadı (' + res.status + ').'));
        }
        var blob = await res.blob();
        if (!blob || blob.size < 100) {
            if (speakWithWebSpeech(text, voice, rateVal, pitchVal)) {
                sfx('success');
                showToast('Ses cihaz seslendiricisi ile oynatılıyor.', 'info');
                return;
            }
            throw new Error('Aslan AI ses verisi oluşturulamadı.');
        }
        if (state.lastAudioUrl) {
            try { URL.revokeObjectURL(state.lastAudioUrl); } catch (e) {}
        }
        var url = URL.createObjectURL(blob);
        state.lastAudioUrl = url;
        $('player').src = url;
        $('dl-link').href = url;
        $('tts-engine-badge').textContent = 'Aslan AI';
        showEl($('audio-box'), true);
        $('player').play().catch(function () {
            speakWithWebSpeech(text, voice, rateVal, pitchVal);
        });
        if (typeof registerSessionMediaBlob === 'function') {
            registerSessionMediaBlob(blob, 'aslan-ses.mp3', 'audio/mpeg');
        }
        sfx('success');
        showToast('Ses Aslan AI tarafından oluşturuldu.', 'success');
    } catch (e) {
        if (speakWithWebSpeech(text, voice, rateVal, pitchVal)) {
            sfx('success');
            showToast('Ses cihaz üzerinden oynatılıyor.', 'info');
            return;
        }
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
