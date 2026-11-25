"""
Aslan AI Ozel WhatsApp Botu Olusturucu, Onay Akisi, Tek Hak Kontrolu & Canli Guncelleyici.
"""
import copy
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config as cfg, database

_ACTIVE_STAGES = frozenset({
    'awaiting_risk_consent',
    'awaiting_phone',
    'awaiting_phone_consent',
    'awaiting_code_confirm',
    'pairing',
    'connected_wait_ack',
    'custom_triggers',
    'custom_groups',
    'custom_human',
    'custom_tone',
    'custom_ai_choice',
    'custom_ai_key',
})

_YES_WORDS = frozenset({
    'evet', 'onay', 'onayliyorum', 'onaylıyorum', 'kabul', 'kabul ediyorum',
    'izin', 'izin veriyorum', 'tamam', 'olur', 'devam', 'devam et',
    'baglandim', 'bağlandım', 'baglandi', 'bağlandı', 'ok', 'yes', 'istiyorum',
    'kodu almayi onayliyorum', 'kodu almayı onaylıyorum',
})
_NO_WORDS = frozenset({
    'hayir', 'hayır', 'red', 'reddediyorum', 'istemiyorum', 'iptal',
    'vazgectim', 'vazgeçtim', 'olmaz', 'hayir istemiyorum', 'hayır istemiyorum',
    'kural', 'kurallara gore', 'kurallara göre', 'sadece kurallar',
    'sadece kurallara gore', 'sadece kurallara göre', 'baglamak istemiyorum', 'bağlamak istemiyorum',
})

_PHONE_RE = re.compile(r'\+?\d[\d\s().-]{8,18}\d')
_KEY_RE = re.compile(r'(AIza[0-9A-Za-z_-]{12,}|gsk_[0-9A-Za-z_-]{12,}|sk-[A-Za-z0-9_-]{12,})')


def _default_bot_config() -> dict:
    return {
        'enabled': True,
        'allow_groups': False,
        'allow_private': True,
        'group_mode': 'off',
        'trigger_keywords': [],
        'ignored_keywords': [],
        'typing_delay_seconds': 2,
        'show_typing': True,
        'tone': 'Samimi, nazik ve profesyonel',
        'instructions': '',
        'default_reply': '',
        'rules': [],
        'auto_reactions': [],
        'custom_ai': {
            'enabled': False,
            'provider': '',
            'model': '',
            'endpoint': '',
            'api_key_enc': '',
            'has_key': False,
        },
    }


def _default_wa_state() -> dict:
    return {
        'stage': 'idle',
        'risk_accepted': False,
        'phone': '',
        'phone_consent': False,
        'code_confirmed': False,
        'pairing_code': '',
        'connected': False,
        'connected_ack': False,
        'setup_session': {
            'active': False,
            'credit_charged': False,
            'started_at': 0,
        },
        'config': _default_bot_config(),
    }


def _normalize_wa_state(raw) -> dict:
    base = _default_wa_state()
    if not isinstance(raw, dict):
        return base
    for k in ('stage', 'risk_accepted', 'phone', 'phone_consent', 'code_confirmed',
              'pairing_code', 'connected', 'connected_ack'):
        if k in raw:
            base[k] = raw[k]
    if isinstance(raw.get('setup_session'), dict):
        base['setup_session'].update(raw['setup_session'])
    if isinstance(raw.get('config'), dict):
        cfg_obj = copy.deepcopy(base['config'])
        for k, v in raw['config'].items():
            if k == 'custom_ai' and isinstance(v, dict):
                cfg_obj['custom_ai'].update(v)
            else:
                cfg_obj[k] = v
        base['config'] = cfg_obj
    return base


def _worker_base_url() -> str:
    env_url = (os.environ.get('WA_BOT_WORKER_URL') or cfg.WA_BOT_WORKER_URL or '').strip().rstrip('/')
    if env_url:
        return env_url
    if not os.environ.get('VERCEL'):
        port = int(os.environ.get('WA_WORKER_PORT') or cfg.WA_BOT_LOCAL_PORT or 5056)
        return 'http://127.0.0.1:%d' % port
    return ''


def _worker_token() -> str:
    return (
        os.environ.get('WA_BOT_WORKER_TOKEN')
        or os.environ.get('MEDIA_WORKER_TOKEN')
        or cfg.WA_BOT_WORKER_TOKEN
        or ''
    ).strip()


def call_wa_worker(method: str, path: str, payload=None, timeout: int = 25) -> dict:
    base = _worker_base_url()
    if not base:
        return {'success': False, 'error': 'Aslan AI WhatsApp bağlantı servisi şu an yapılandırılmamış.'}
    url = '%s%s' % (base, path)
    headers = {'User-Agent': 'Aslan-AI-WA/1.0'}
    tok = _worker_token()
    if tok:
        headers['X-Worker-Token'] = tok
    data = None
    if method.upper() == 'POST':
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode('utf-8'))
            return parsed if isinstance(parsed, dict) else {'success': False, 'error': 'Aslan AI bağlantı servisi yanıt veremedi.'}
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode('utf-8', errors='replace'))
            if isinstance(parsed, dict) and parsed.get('error'):
                return {'success': False, 'error': str(parsed['error'])}
        except Exception:
            pass
        return {'success': False, 'error': 'Aslan AI WhatsApp servisi geçici olarak yanıt veremedi.'}
    except Exception:
        return {'success': False, 'error': 'Aslan AI WhatsApp bağlantı servisine ulaşılamadı.'}


def build_runtime_config(wa_state: dict) -> dict:
    """Bot servisine gonderilecek calisma yapilandirmasini olusturur (sifreli anahtari cozer)."""
    cfg_obj = copy.deepcopy((wa_state or {}).get('config') or _default_bot_config())
    cai = cfg_obj.get('custom_ai') if isinstance(cfg_obj.get('custom_ai'), dict) else {}
    enc = cai.pop('api_key_enc', '')
    cai.pop('has_key', None)
    cai['api_key'] = database.decrypt_secret_value(enc) if enc else ''
    cfg_obj['custom_ai'] = cai
    return cfg_obj


def sanitize_config_summary(wa_state: dict) -> dict:
    """Arayuze donecek ozet (asla acik API anahtari veya kutuphane adi icermez)."""
    st = _normalize_wa_state(wa_state)
    c = st['config']
    cai = c.get('custom_ai') or {}
    return {
        'enabled': bool(c.get('enabled', True)),
        'connected': bool(st.get('connected')),
        'stage': st.get('stage', 'idle'),
        'allow_groups': bool(c.get('allow_groups')),
        'allow_private': bool(c.get('allow_private', True)),
        'group_mode': c.get('group_mode', 'off'),
        'trigger_keywords': list(c.get('trigger_keywords') or []),
        'ignored_keywords': list(c.get('ignored_keywords') or []),
        'typing_delay_seconds': c.get('typing_delay_seconds', 2),
        'show_typing': bool(c.get('show_typing', True)),
        'tone': c.get('tone', ''),
        'rules_count': len(c.get('rules') or []),
        'auto_reactions_count': len(c.get('auto_reactions') or []),
        'custom_ai_enabled': bool(cai.get('enabled') and (cai.get('has_key') or cai.get('api_key_enc'))),
        'custom_ai_provider': cai.get('provider', '') if cai.get('enabled') else '',
    }


def _tr_lower(text: str) -> str:
    return str(text or '').replace('İ', 'i').strip().lower().replace('\u0307', '')


def _is_affirmative(msg: str) -> bool:
    low = _tr_lower(msg)
    if not low:
        return False
    cleaned = re.sub(r'[.!?,:;]+$', '', low).strip()
    if cleaned in _YES_WORDS:
        return True
    if any(n in cleaned for n in ('hayır', 'hayir', 'iptal', 'reddet', 'istemiyorum', 'vazgeç')):
        return False
    return any(p in cleaned for p in (
        'onaylıyorum', 'onayliyorum', 'kabul ediyorum', 'izin veriyorum',
        'evet', 'onay', 'tamam', 'devam', 'bağlandım', 'baglandim',
    ))


def _is_negative(msg: str) -> bool:
    low = _tr_lower(msg)
    if not low:
        return False
    cleaned = re.sub(r'[.!?,:;]+$', '', low).strip()
    if cleaned in _NO_WORDS:
        return True
    return any(p in cleaned for p in (
        'hayır', 'hayir', 'iptal', 'reddediyorum', 'kabul etmiyorum',
        'izin vermiyorum', 'onaylamıyorum', 'onaylamiyorum', 'vazgeç', 'vazgec',
        'istemiyorum', 'sadece kural', 'kurallara göre', 'kurallara gore',
    ))


def _extract_phone(msg: str) -> str:
    m = _PHONE_RE.search(str(msg or ''))
    if not m:
        return ''
    digits = re.sub(r'\D', '', m.group(0))
    if len(digits) == 10 and digits.startswith('5'):
        digits = '90' + digits
    elif len(digits) == 11 and digits.startswith('05'):
        digits = '9' + digits
    if 10 <= len(digits) <= 15:
        return digits
    return ''


def _mask_phone_display(digits: str) -> str:
    if len(digits) <= 6:
        return '+' + digits
    return '+%s ••• %s' % (digits[:4], digits[-4:])


def _detect_provider_and_key(msg: str):
    m = _KEY_RE.search(str(msg or ''))
    key = m.group(1) if m else ''
    if not key:
        kv = re.search(r'(?:api[_\s-]*key|anahtar)\s*[:=]\s*([^\s,;]{10,})', str(msg or ''), re.I)
        if kv:
            key = kv.group(1).strip()
    low = _tr_lower(msg)
    provider = ''
    if 'gemini' in low or 'google' in low or key.startswith('AIza'):
        provider = 'gemini'
    elif 'groq' in low or key.startswith('gsk_'):
        provider = 'groq'
    elif 'openrouter' in low or key.startswith('sk-or-'):
        provider = 'openrouter'
    elif 'anthropic' in low or 'claude' in low or key.startswith('sk-ant-'):
        provider = 'anthropic'
    elif 'openai' in low or 'chatgpt' in low or 'gpt' in low or key.startswith('sk-'):
        provider = 'openai'
    return provider, key


def _extract_quoted_terms(text: str) -> list:
    items = re.findall(r"['\"]([^'\"]{1,40})['\"]", str(text or ''))
    out = []
    for it in items:
        clean = it.strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def _apply_natural_customization_rules(cfg_obj: dict, text: str) -> None:
    """Kullanicinin mesajindan bot tetikleyici, grup, zamanlama, uslub ve kural ayarlari cikarir."""
    low = _tr_lower(text)
    quoted = _extract_quoted_terms(text)

    if any(p in low for p in ('gruplarda cevap vermesin', 'gruplar kapalı', 'gruplar kapali', 'grup kapalı', 'grup kapali', 'grupta cevap vermesin', 'sadece özel', 'sadece ozel')):
        cfg_obj['allow_groups'] = False
        cfg_obj['group_mode'] = 'off'
    elif any(p in low for p in ('gruplarda sadece', 'etiket', 'bahsedilince', 'etiketlenince')):
        cfg_obj['allow_groups'] = True
        cfg_obj['group_mode'] = 'mentions_or_triggers'
    elif any(p in low for p in ('gruplarda da cevap', 'hem grup hem özel', 'hem grup hem ozel', 'gruplar açık', 'gruplar acik', 'gruplarda yanıt')):
        cfg_obj['allow_groups'] = True
        cfg_obj['group_mode'] = 'all'

    if 'sadece gruplar' in low or 'özel mesaj kapalı' in low or 'ozel mesaj kapali' in low:
        cfg_obj['allow_private'] = False
        cfg_obj['allow_groups'] = True
    elif 'özel mesaj açık' in low or 'ozel mesaj acik' in low or 'özel mesajlarda' in low or 'ozel mesajlarda' in low:
        cfg_obj['allow_private'] = True

    sec_m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:sn|saniye)', low)
    if sec_m:
        try:
            val = float(sec_m.group(1).replace(',', '.'))
            cfg_obj['typing_delay_seconds'] = max(0, min(30, int(round(val))))
        except ValueError:
            pass

    if any(p in low for p in ('yazıyor kapalı', 'yaziyor kapali', 'yazıyor gösterme', 'yaziyor gosterme', 'yazıyor olmasın', 'yaziyor olmasin')):
        cfg_obj['show_typing'] = False
    elif any(p in low for p in ('yazıyor açık', 'yaziyor acik', 'yazıyor göstersin', 'yaziyor gostersin', 'yazıyor bilgisi', 'yaziyor bilgisi', 'composing')):
        cfg_obj['show_typing'] = True

    if any(p in low for p in ('botu durdur', 'botu kapat', 'devre dışı', 'devre disi')):
        cfg_obj['enabled'] = False
    elif any(p in low for p in ('botu başlat', 'botu baslat', 'botu aç', 'botu ac', 'aktif et')):
        cfg_obj['enabled'] = True

    if 'sadece' in low and any(w in low for w in ('kelime', 'geçince', 'gecince', 'yanıt', 'yanit', 'cevap')):
        if quoted:
            cfg_obj['trigger_keywords'] = quoted
        else:
            m_kw = re.search(r"sadece\s+([^\s.,;]+)\s+(?:kelime|geçince|gecince)", low)
            if m_kw and m_kw.group(1):
                cfg_obj['trigger_keywords'] = [m_kw.group(1).strip()]

    # Otomatik emoji tepkileri ve soru->cevap kurallari ("kelime -> cevap" veya "kelime: cevap")
    for line in re.split(r'[;\n]+', str(text or '')):
        if '->' in line:
            parts = line.split('->', 1)
            trig = parts[0].strip(" '\"")
            ans = parts[1].strip(" '\"")
            if trig and ans:
                if len(ans) <= 4 and not any(ch.isalnum() for ch in ans):
                    cfg_obj['auto_reactions'].append({'trigger': trig, 'emoji': ans})
                else:
                    cfg_obj['rules'].append({'match_type': 'contains', 'trigger': trig, 'response': ans})


def is_wa_setup_trigger(message: str) -> bool:
    low = _tr_lower(message)
    if not low:
        return False
    has_wa = ('whatsapp' in low or re.search(r'\bwp\b', low) is not None)
    if not has_wa:
        return False
    return any(k in low for k in (
        'bot', 'asistan', 'otomasyon', 'kur', 'oluştur', 'olustur',
        'yapabilir misin', 'yapar mısın', 'yapar misin', 'bağla', 'bagla',
    ))


def is_in_active_setup(wa_state: dict) -> bool:
    st = _normalize_wa_state(wa_state)
    return bool(st['setup_session'].get('active')) and st.get('stage') in _ACTIVE_STAGES


def is_live_update_intent(message: str, wa_state: dict) -> bool:
    st = _normalize_wa_state(wa_state)
    if st.get('stage') != 'active' and not st.get('connected'):
        return False
    low = _tr_lower(message)
    has_bot_ref = any(k in low for k in (
        'bot', 'whatsapp', 'gruplar', 'grupta', 'özel mesaj', 'ozel mesaj',
        'yazıyor', 'yaziyor', 'tetikleyici', 'üslup', 'uslup', 'cevap vermesin',
        'yanıt versin', 'yanit versin', 'kelime geçince', 'kelime gecince',
    ))
    return has_bot_ref and not is_wa_setup_trigger(message)


def get_status_snapshot(username_lower: str) -> dict:
    """Hem veritabani hem canlı WhatsApp servisinden güncel durumu dondurur."""
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    worker_res = call_wa_worker(
        'GET',
        '/wa/status?user_id=%s' % urllib.parse.quote(username_lower, safe=''),
        timeout=8,
    )
    if worker_res.get('success'):
        if worker_res.get('connected'):
            was_connected = bool(wa_state.get('connected'))
            wa_state['connected'] = True
            if wa_state.get('stage') == 'pairing':
                wa_state['stage'] = 'connected_wait_ack'
            if not was_connected:
                database.save_user_wa_bot(username_lower, wa_state)
        worker_status = worker_res.get('status', wa_state.get('stage'))
    else:
        worker_status = wa_state.get('stage', 'idle')

    return {
        'success': True,
        'connected': bool(wa_state.get('connected')),
        'status': worker_status,
        'stage': wa_state.get('stage', 'idle'),
        'setup_active': is_in_active_setup(wa_state),
        'connected_ack': bool(wa_state.get('connected_ack')),
        'notification': 'Bağlantınız başarılı' if wa_state.get('connected') else '',
        'config_summary': sanitize_config_summary(wa_state),
    }


def confirm_connected(username_lower: str) -> dict:
    """Kullanici 'Baglantiniz basarili' bildirimini onayladiginda 0 hakla ozellestirmeyi baslatir."""
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    snap = get_status_snapshot(username_lower)
    if not snap.get('connected'):
        return {
            'success': False,
            'error': 'WhatsApp cihaz bağlantısı henüz tamamlanmadı. Lütfen 8 haneli kodu telefonunuzdan girip tekrar deneyin.',
            'status': snap,
        }
    wa_state['connected'] = True
    wa_state['connected_ack'] = True
    wa_state['stage'] = 'custom_triggers'
    wa_state['setup_session']['active'] = True
    database.save_user_wa_bot(username_lower, wa_state)
    q1 = _question_triggers_text()
    return {
        'success': True,
        'reply': q1,
        'wa_event': {'type': 'customization_started', 'stage': 'custom_triggers', 'connected': True},
        'status': get_status_snapshot(username_lower),
    }


def stop_user_bot(username_lower: str, logout: bool = False) -> dict:
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    wa_state['config']['enabled'] = False
    if logout:
        wa_state['connected'] = False
        wa_state['stage'] = 'idle'
        wa_state['setup_session']['active'] = False
    res = call_wa_worker('POST', '/wa/stop', {'user_id': username_lower, 'logout': bool(logout)}, timeout=10)
    database.save_user_wa_bot(username_lower, wa_state)
    return {
        'success': True,
        'worker_stopped': bool(res.get('success', True)),
        'config_summary': sanitize_config_summary(wa_state),
    }


def _question_triggers_text() -> str:
    return (
        "✅ **Bağlantınız başarılı olarak onaylandı!** Şimdi Aslan AI WhatsApp botunuzu adım adım özelleştirelim *(bu kurulum tek hak kapsamındadır)*.\n\n"
        "**1/5 — Tetikleyici ve Sessiz Kalma Kuralları:**\n"
        "Bot hangi mesajlara veya komutlara cevap versin, hangilerine sessiz kalsın?\n"
        "*(Örn: \"Tüm mesajlara cevap versin ama reklam, link kelimelerine sessiz kalsın\" veya \"Sadece fiyat, bilgi, merhaba kelimelerine cevap versin\")*"
    )


def _question_groups_text() -> str:
    return (
        "**2/5 — Grup ve Özel Mesaj Davranışı:**\n"
        "Bot grup sohbetlerinde ve özel mesajlarda nasıl davransın?\n"
        "*(Örn: \"Özel mesajlarda cevap versin, gruplarda cevap vermesin\" veya \"Gruplarda sadece etiket/tetikleyici geçince yazsın\")*"
    )


def _question_human_text() -> str:
    return (
        "**3/5 — İnsansı Zamanlama ve Yazıyor Bilgisi:**\n"
        "Cevap verirken insan gibi görünmesi için kaç saniye beklesin ve **\"yazıyor...\"** bilgisini göstersin mi?\n"
        "*(Örn: \"3 saniye beklesin ve yazıyor göstersin\" veya \"1 saniye beklesin, yazıyor kapalı\")*"
    )


def _question_tone_text() -> str:
    return (
        "**4/5 — Üslup, Kurallar ve Otomatik Tepkiler:**\n"
        "Botun üslubu, özel kural/yanıtları ve otomatik emoji tepkileri nasıl olsun?\n"
        "*(Örn: \"Samimi ve nazik olsun; teşekkür -> ❤️; fiyat -> Güncel fiyat listemiz için 1 yazın\")*"
    )


def _question_ai_choice_text() -> str:
    return (
        "**5/5 — Yapay Zeka Entegrasyonu:**\n"
        "Botuna kendi yapay zekanı bağlamak ister misin? Bağlamak istiyorsan API anahtarını paylaşabilirsin, istemiyorsan sadece belirlediğin kurallara göre çalışacak şekilde ayarlayabilirim."
    )


def _sync_worker_config(username_lower: str, wa_state: dict) -> dict:
    return call_wa_worker(
        'POST',
        '/wa/config',
        {'user_id': username_lower, 'config': build_runtime_config(wa_state)},
        timeout=10,
    )


def _finalize_bot_setup(username_lower: str, wa_state: dict) -> tuple:
    cfg_obj = wa_state['config']
    if not cfg_obj.get('default_reply') and not (cfg_obj.get('custom_ai') or {}).get('enabled'):
        if not cfg_obj.get('rules'):
            cfg_obj['default_reply'] = (
                'Merhaba! Mesajınız alındı. Aslan AI WhatsApp asistanı olarak size en kısa sürede yardımcı olacağım.'
            )
    cfg_obj['enabled'] = True
    wa_state['stage'] = 'active'
    wa_state['setup_session']['active'] = False
    database.save_user_wa_bot(username_lower, wa_state)
    _sync_worker_config(username_lower, wa_state)
    summary = sanitize_config_summary(wa_state)
    mode_label = (
        'Kendi yapay zekanız (%s) + özel kurallar' % summary['custom_ai_provider']
        if summary['custom_ai_enabled'] else 'Otomatik kural ve senaryo tabanlı yanıt sistemi'
    )
    reply = (
        "🎉 **Aslan AI Özel WhatsApp Botunuz Hazır ve Aktif!**\n\n"
        "• **Çalışma Modu:** %s\n"
        "• **Özel Mesaj / Grup:** Özel: %s | Grup: %s (%s)\n"
        "• **İnsansı Gecikme / Yazıyor:** %s sn | %s\n"
        "• **Üslup:** %s\n\n"
        "İstediğiniz zaman sohbete gelip *\"Artık gruplarda cevap vermesin, sadece 'sipariş' kelimesi geçince yanıt versin\"* diyerek botunuzu canlı güncelleyebilirsiniz."
        % (
            mode_label,
            'Açık' if summary['allow_private'] else 'Kapalı',
            'Açık' if summary['allow_groups'] else 'Kapalı',
            summary['group_mode'],
            summary['typing_delay_seconds'],
            'Açık' if summary['show_typing'] else 'Kapalı',
            summary['tone'],
        )
    )
    return reply, {'type': 'bot_ready', 'config_summary': summary}


def apply_structured_update(username_lower: str, params: dict) -> dict:
    """AI aksiyonu veya canli sohbet uzerinden WhatsApp bot ayarlarini aninda gunceller."""
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    p = params if isinstance(params, dict) else {}
    cfg_obj = wa_state['config']
    for bool_key in ('enabled', 'allow_groups', 'allow_private', 'show_typing'):
        if bool_key in p:
            cfg_obj[bool_key] = bool(p[bool_key])
    if 'group_mode' in p and str(p['group_mode']) in ('off', 'all', 'mentions_or_triggers'):
        cfg_obj['group_mode'] = str(p['group_mode'])
    if 'typing_delay_seconds' in p:
        try:
            cfg_obj['typing_delay_seconds'] = max(0, min(30, int(float(p['typing_delay_seconds']))))
        except (TypeError, ValueError):
            pass
    for list_key in ('trigger_keywords', 'ignored_keywords'):
        if list_key in p and isinstance(p[list_key], list):
            cfg_obj[list_key] = [str(x).strip() for x in p[list_key] if str(x).strip()]
    for str_key in ('tone', 'instructions', 'default_reply'):
        if str_key in p and p[str_key] is not None:
            cfg_obj[str_key] = str(p[str_key]).strip()
    if 'rules' in p and isinstance(p['rules'], list):
        cfg_obj['rules'] = [r for r in p['rules'] if isinstance(r, dict)]
    if 'auto_reactions' in p and isinstance(p['auto_reactions'], list):
        cfg_obj['auto_reactions'] = [r for r in p['auto_reactions'] if isinstance(r, dict)]
    if p.get('enabled') is False:
        call_wa_worker('POST', '/wa/stop', {'user_id': username_lower, 'logout': False}, timeout=8)
    else:
        _sync_worker_config(username_lower, wa_state)
    database.save_user_wa_bot(username_lower, wa_state)
    summary = sanitize_config_summary(wa_state)
    return {
        'success': True,
        'operation': 'wa_bot_update',
        'label': 'WhatsApp Bot Güncellemesi',
        'config_summary': summary,
    }


def apply_live_update_text(username_lower: str, message: str) -> tuple:
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    _apply_natural_customization_rules(wa_state['config'], message)
    low = _tr_lower(message)
    if 'üslup' in low or 'uslup' in low or 'resmi' in low or 'samimi' in low:
        wa_state['config']['tone'] = message.strip()[:200]
    if wa_state['config'].get('enabled') is False:
        call_wa_worker('POST', '/wa/stop', {'user_id': username_lower, 'logout': False}, timeout=8)
    else:
        _sync_worker_config(username_lower, wa_state)
    database.save_user_wa_bot(username_lower, wa_state)
    summary = sanitize_config_summary(wa_state)
    reply = (
        "✅ **WhatsApp Bot Ayarlarınız Canlı Olarak Güncellendi:**\n"
        "• **Durum:** %s\n"
        "• **Özel Mesaj / Grup:** Özel: %s | Grup: %s (%s)\n"
        "• **Tetikleyici Kelimeler:** %s\n"
        "• **İnsansı Gecikme / Yazıyor:** %s sn | %s"
        % (
            'Aktif' if summary['enabled'] else 'Durduruldu',
            'Açık' if summary['allow_private'] else 'Kapalı',
            'Açık' if summary['allow_groups'] else 'Kapalı',
            summary['group_mode'],
            ', '.join(summary['trigger_keywords']) if summary['trigger_keywords'] else 'Tüm mesajlar',
            summary['typing_delay_seconds'],
            'Açık' if summary['show_typing'] else 'Kapalı',
        )
    )
    return reply, {'type': 'config_updated', 'config_summary': summary}


def should_consume_credit(username_lower: str, message: str) -> tuple:
    """
    WhatsApp bot akisinda tek hak (toplam 1 kredi) kuralini uygular.

    Returns: (intercept_type, needs_credit_charge)
      intercept_type: 'setup_continue' | 'setup_start' | 'live_update' | None
    """
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    if is_in_active_setup(wa_state):
        already_charged = bool(wa_state['setup_session'].get('credit_charged'))
        return 'setup_continue', (not already_charged)
    if is_wa_setup_trigger(message):
        return 'setup_start', True
    if is_live_update_intent(message, wa_state):
        return 'live_update', True
    return None, True


def handle_chat_turn(username_lower: str, message: str, consume_credit_fn=None):
    """
    WhatsApp bot olusturucu / canli guncelleyici konusma adimini isletir.

    Returns: (handled: bool, reply: str, wa_event: dict | None, credit_consumed: bool)
    """
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    msg = str(message or '').strip()

    # 1) Yeni kurulum baslatma
    if is_wa_setup_trigger(msg) and not is_in_active_setup(wa_state):
        if callable(consume_credit_fn):
            consume_credit_fn()
        fresh = _default_wa_state()
        fresh['stage'] = 'awaiting_risk_consent'
        fresh['setup_session'] = {
            'active': True,
            'credit_charged': True,
            'started_at': int(time.time()),
        }
        database.save_user_wa_bot(username_lower, fresh)
        reply = (
            "⚠️ **Önemli Risk Bilgilendirmesi ve Sorumluluk Reddi**\n\n"
            "WhatsApp botu oluşturmak için resmi olmayan üçüncü taraf çoklu cihaz bağlantı yöntemleri kullanılmaktadır. Devam etmeden önce şu riskleri bilmeniz gerekir:\n"
            "• WhatsApp hesabınız geçici olarak kısıtlanabilir, askıya alınabilir veya kalıcı olarak kapatılabilir.\n"
            "• Bağlantı beklenmedik şekilde kesilebilir veya oturum sonlandırılabilir.\n"
            "• Otomatik yanıtların ve hesabınızdan gönderilen tüm mesajların hukuki/hesap içi sorumluluğu tamamen size aittir.\n\n"
            "**Sorumluluk Reddi:** Bu işlemden doğabilecek hiçbir hesap kısıtlaması, veri kaybı veya aksaklıktan **Aslan AI ve site yönetimi sorumlu tutulamaz.**\n"
            "*(Not: Tüm WhatsApp bot kurulum ve özelleştirme adımları toplamda yalnızca **1 hak** sayılır.)*\n\n"
            "Yukarıdaki riskleri ve sorumluluk reddini kabul ediyor musunuz? (**Onaylıyorum** / **İptal**)"
        )
        return True, reply, {'type': 'risk_prompt', 'stage': 'awaiting_risk_consent'}, True

    # 2) Devam eden kurulum adimlari (0 ek hak!)
    if is_in_active_setup(wa_state):
        stage = wa_state.get('stage')

        if _is_negative(msg) and stage in (
            'awaiting_risk_consent', 'awaiting_phone', 'awaiting_phone_consent', 'awaiting_code_confirm', 'pairing'
        ):
            wa_state['stage'] = 'idle'
            wa_state['setup_session']['active'] = False
            database.save_user_wa_bot(username_lower, wa_state)
            return True, "İşlem isteğiniz üzerine durduruldu. Onayınız olmadan hiçbir WhatsApp bağlantısı başlatılmadı.", {
                'type': 'setup_cancelled'
            }, False

        if stage == 'awaiting_risk_consent':
            if not _is_affirmative(msg):
                return True, (
                    "Devam edebilmem için risk bilgilendirmesini ve sorumluluk reddini açıkça onaylamanız gerekir (**Onaylıyorum** veya **İptal** yazabilirsiniz)."
                ), {'type': 'risk_prompt', 'stage': 'awaiting_risk_consent'}, False
            wa_state['risk_accepted'] = True
            wa_state['stage'] = 'awaiting_phone'
            database.save_user_wa_bot(username_lower, wa_state)
            return True, (
                "✅ Risk bildirimi ve sorumluluk reddi onaylandı.\n\n"
                "Lütfen WhatsApp botunu bağlamak istediğiniz telefon numarasını ülke koduyla birlikte yazın *(örn: `+90 555 123 45 67`)*."
            ), {'type': 'phone_prompt', 'stage': 'awaiting_phone'}, False

        if stage == 'awaiting_phone':
            digits = _extract_phone(msg)
            if not digits:
                return True, (
                    "Geçerli bir telefon numarası algılanamadı. Lütfen ülke koduyla birlikte girin *(örn: `+90 555 123 45 67`)*."
                ), {'type': 'phone_prompt', 'stage': 'awaiting_phone'}, False
            wa_state['phone'] = digits
            wa_state['stage'] = 'awaiting_phone_consent'
            database.save_user_wa_bot(username_lower, wa_state)
            return True, (
                "Girdiğiniz **%s** numaralı telefonun WhatsApp cihaz eşleştirme işlemi için kullanılmasına izin veriyor musunuz?\n"
                "(**İzin veriyorum** / **İptal**)" % _mask_phone_display(digits)
            ), {'type': 'phone_consent_prompt', 'stage': 'awaiting_phone_consent'}, False

        if stage == 'awaiting_phone_consent':
            if not _is_affirmative(msg):
                return True, (
                    "Telefon numarasının eşleştirme için kullanılmasına açıkça izin vermeniz gerekiyor (**İzin veriyorum** veya **İptal**)."
                ), {'type': 'phone_consent_prompt', 'stage': 'awaiting_phone_consent'}, False
            wa_state['phone_consent'] = True
            wa_state['stage'] = 'awaiting_code_confirm'
            database.save_user_wa_bot(username_lower, wa_state)
            return True, (
                "⏳ **Önemli Hatırlatma:** Üretilecek **8 haneli eşleştirme kodunun (pairing code) geçerlilik süresi çok kısadır** (yaklaşık 60 saniye içinde telefonunuza girmeniz gerekir).\n\n"
                "**Kodu almayı onaylıyor musunuz?** (**Onaylıyorum** / **İptal**)"
            ), {'type': 'code_confirm_prompt', 'stage': 'awaiting_code_confirm'}, False

        if stage == 'awaiting_code_confirm':
            if not _is_affirmative(msg):
                return True, (
                    "Hazır olduğunuzda **Onaylıyorum** yazarak 8 haneli kodu oluşturabilir veya **İptal** diyebilirsiniz."
                ), {'type': 'code_confirm_prompt', 'stage': 'awaiting_code_confirm'}, False
            wa_state['code_confirmed'] = True
            pair_res = call_wa_worker(
                'POST',
                '/wa/pair',
                {
                    'user_id': username_lower,
                    'phone': wa_state.get('phone', ''),
                    'config': build_runtime_config(wa_state),
                },
                timeout=35,
            )
            if not pair_res.get('success'):
                database.save_user_wa_bot(username_lower, wa_state)
                return True, (
                    "⚠️ %s Hazır olduğunuzda tekrar **Onaylıyorum** yazabilirsiniz."
                    % pair_res.get('error', 'Eşleştirme kodu şu an alınamadı.')
                ), {'type': 'pairing_retry'}, False

            if pair_res.get('already_connected'):
                wa_state['connected'] = True
                wa_state['stage'] = 'connected_wait_ack'
                database.save_user_wa_bot(username_lower, wa_state)
                return True, "✅ **Bağlantınız başarılı!** Özelleştirmeye geçmek için onay butonuna tıklayın veya **Devam** yazın.", {
                    'type': 'connected', 'connected': True
                }, False

            raw8 = str(pair_res.get('pairing_code') or '')
            formatted = str(pair_res.get('formatted_code') or raw8)
            wa_state['pairing_code'] = raw8
            wa_state['stage'] = 'pairing'
            database.save_user_wa_bot(username_lower, wa_state)
            reply = (
                "🔑 **8 Haneli Eşleştirme Kodunuz Hazır!** *(Kısa süre içinde geçersiz olur)*\n\n"
                "Telefonunuzda şu adımları sırasıyla izleyin:\n"
                "1. **WhatsApp > Bağlı Cihazlar > Cihaz Bağla > Telefon numarasıyla bağla** yolunu açın.\n"
                "2. Yukarıdaki/karttaki kopyalama butonuyla kodu kopyalayıp girin.\n\n"
                "Bağlantı tamamlandığında ekranda **\"Bağlantınız başarılı\"** bildirimi görünecektir."
            )
            return True, reply, {
                'type': 'pairing_code',
                'code': raw8,
                'formatted_code': formatted,
                'expires_in': int(pair_res.get('expires_in') or 60),
                'status': 'pairing',
            }, False

        if stage in ('pairing', 'connected_wait_ack'):
            snap = get_status_snapshot(username_lower)
            if 'yeni kod' in _tr_lower(msg) or 'tekrar kod' in _tr_lower(msg):
                wa_state['stage'] = 'awaiting_code_confirm'
                database.save_user_wa_bot(username_lower, wa_state)
                return handle_chat_turn(username_lower, 'onaylıyorum', consume_credit_fn=None)
            if snap.get('connected'):
                wa_state['connected'] = True
                wa_state['connected_ack'] = True
                wa_state['stage'] = 'custom_triggers'
                database.save_user_wa_bot(username_lower, wa_state)
                return True, _question_triggers_text(), {
                    'type': 'customization_started', 'stage': 'custom_triggers', 'connected': True
                }, False
            return True, (
                "⏳ WhatsApp bağlantısı bekleniyor. Telefonunuzdan **WhatsApp > Bağlı Cihazlar > Cihaz Bağla > Telefon numarasıyla bağla** "
                "adımlarını tamamlayın. İsterseniz *\"yeni kod\"* yazarak taze kod isteyebilirsiniz."
            ), {'type': 'pairing_waiting'}, False

        if stage == 'custom_triggers':
            cfg_obj = wa_state['config']
            low = _tr_lower(msg)
            if any(p in low for p in ('sessiz kal', 'yoksay', 'cevap verme')):
                # Kabaca "x kelimelerine sessiz kal" ayristirma
                quoted = _extract_quoted_terms(msg)
                if quoted and ('sadece' not in low):
                    cfg_obj['ignored_keywords'] = quoted
            _apply_natural_customization_rules(cfg_obj, msg)
            if not cfg_obj['trigger_keywords'] and ('herkese' not in low and 'tüm' not in low and 'tum' not in low):
                quoted = _extract_quoted_terms(msg)
                if quoted:
                    cfg_obj['trigger_keywords'] = quoted
            wa_state['stage'] = 'custom_groups'
            database.save_user_wa_bot(username_lower, wa_state)
            return True, _question_groups_text(), {'type': 'custom_step', 'stage': 'custom_groups'}, False

        if stage == 'custom_groups':
            _apply_natural_customization_rules(wa_state['config'], msg)
            wa_state['stage'] = 'custom_human'
            database.save_user_wa_bot(username_lower, wa_state)
            return True, _question_human_text(), {'type': 'custom_step', 'stage': 'custom_human'}, False

        if stage == 'custom_human':
            _apply_natural_customization_rules(wa_state['config'], msg)
            wa_state['stage'] = 'custom_tone'
            database.save_user_wa_bot(username_lower, wa_state)
            return True, _question_tone_text(), {'type': 'custom_step', 'stage': 'custom_tone'}, False

        if stage == 'custom_tone':
            wa_state['config']['tone'] = msg.strip()[:240]
            wa_state['config']['instructions'] = msg.strip()[:1200]
            _apply_natural_customization_rules(wa_state['config'], msg)
            wa_state['stage'] = 'custom_ai_choice'
            database.save_user_wa_bot(username_lower, wa_state)
            return True, _question_ai_choice_text(), {'type': 'custom_step', 'stage': 'custom_ai_choice'}, False

        if stage == 'custom_ai_choice':
            provider, key = _detect_provider_and_key(msg)
            if key:
                wa_state['config']['custom_ai'] = {
                    'enabled': True,
                    'provider': provider or 'openai',
                    'model': '',
                    'endpoint': '',
                    'api_key_enc': database.encrypt_secret_value(key),
                    'has_key': True,
                }
                reply, ev = _finalize_bot_setup(username_lower, wa_state)
                return True, reply, ev, False
            if _is_negative(msg):
                wa_state['config']['custom_ai'] = {
                    'enabled': False, 'provider': '', 'model': '', 'endpoint': '',
                    'api_key_enc': '', 'has_key': False,
                }
                reply, ev = _finalize_bot_setup(username_lower, wa_state)
                return True, reply, ev, False
            if _is_affirmative(msg) or 'bağla' in _tr_lower(msg) or 'bagla' in _tr_lower(msg):
                wa_state['stage'] = 'custom_ai_key'
                database.save_user_wa_bot(username_lower, wa_state)
                return True, (
                    "Harika! Hangi yapay zeka sağlayıcısını kullanacağınızı *(örn: OpenAI, Gemini, Groq, Anthropic)* ve **API anahtarınızı** yazın "
                    "*(anahtarınız şifreli olarak güvenle saklanır)*. İsterseniz vazgeçip *\"kurallara göre ayarla\"* diyebilirsiniz."
                ), {'type': 'custom_step', 'stage': 'custom_ai_key'}, False
            return True, _question_ai_choice_text(), {'type': 'custom_step', 'stage': 'custom_ai_choice'}, False

        if stage == 'custom_ai_key':
            if _is_negative(msg):
                wa_state['config']['custom_ai'] = {
                    'enabled': False, 'provider': '', 'model': '', 'endpoint': '',
                    'api_key_enc': '', 'has_key': False,
                }
                reply, ev = _finalize_bot_setup(username_lower, wa_state)
                return True, reply, ev, False
            provider, key = _detect_provider_and_key(msg)
            if not key:
                return True, (
                    "Geçerli bir API anahtarı algılanamadı. Lütfen API anahtarınızı *(sağlayıcı adıyla birlikte)* yazın veya kural tabanlı mod için **\"hayır\"** deyin."
                ), {'type': 'custom_step', 'stage': 'custom_ai_key'}, False
            wa_state['config']['custom_ai'] = {
                'enabled': True,
                'provider': provider or 'openai',
                'model': '',
                'endpoint': '',
                'api_key_enc': database.encrypt_secret_value(key),
                'has_key': True,
            }
            reply, ev = _finalize_bot_setup(username_lower, wa_state)
            return True, reply, ev, False

    # 3) Aktif bot canli guncelleme
    if is_live_update_intent(msg, wa_state):
        if callable(consume_credit_fn):
            consume_credit_fn()
        reply, ev = apply_live_update_text(username_lower, msg)
        return True, reply, ev, True

    return False, '', None, False
