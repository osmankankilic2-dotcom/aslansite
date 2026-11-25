"""
Aslan AI Ozel WhatsApp Botu Olusturucu, Onay Akisi, Tek Hak Kontrolu & Canli Guncelleyici.
"""
import copy
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import api_utils, config as cfg, database

_SETUP_STAGES = frozenset({
    'awaiting_risk_consent',
    'awaiting_phone',
    'awaiting_phone_consent',
    'awaiting_code_confirm',
    'pairing',
    'connected_wait_ack',
    'customizing',
    'custom_triggers',
    'custom_groups',
    'custom_human',
    'custom_tone',
    'custom_ai_choice',
    'custom_ai_key',
})

def _has_active_bot(wa_state: dict) -> bool:
    st = _normalize_wa_state(wa_state)
    return st.get('stage') == 'active' or (bool(st.get('connected')) and bool(st.get('connected_ack')) and st.get('stage') == 'active')


def is_wa_related_message(message: str, wa_state: dict = None) -> bool:
    """Mesajin WhatsApp bot kurulumu, eslestirmesi veya yonetimiyle ilgili olup olmadigini belirler."""
    st = _normalize_wa_state(wa_state)
    low = _tr_lower(message)
    if not low:
        return False
    if 'whatsapp' in low or re.search(r'\bwp\b', low) is not None:
        return True
    if is_in_active_setup(st):
        return True
    if st.get('connected') or st.get('stage') in ('pairing', 'connected_wait_ack', 'customizing', 'active'):
        bot_refs = (
            'bot', 'gruplar', 'grupta', 'özel mesaj', 'ozel mesaj',
            'yazıyor', 'yaziyor', 'tetikleyici', 'üslup', 'uslup',
            'cevap vermesin', 'yanıt versin', 'yanit versin', 'eşleştir', 'eslestir',
            'kod', 'oturum', 'uyan', 'durum', 'durdur', 'kapat', 'başlat', 'baslat',
        )
        if any(k in low for k in bot_refs):
            return True
    return False


def classify_credit_policy(username_lower: str, message: str) -> tuple:
    """
    Guncel Hak / Kota Kuralini uygular:
      - Kurulum ve yapilandirma adimlari (risk onayi, numara, 8 haneli kod, ilk ozellestirme):
        kullanicinin standart AI hakkindan DUSULMEZ -> ('wa_setup_free', 'free')
      - Kurulum bittikten sonra WhatsApp bot hizmeti yonetimi:
        ayri 10 haklik WA bot kotasindan kullanilir -> ('wa_service', 'wa_bot')
      - Normal sohbet / medya / gorsel istekleri:
        standart gunluk AI kotasiyla devam eder -> ('standard', 'ai')
    """
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    low = _tr_lower(message)
    has_wa_kw = ('whatsapp' in low or re.search(r'\bwp\b', low) is not None)
    setup_active = is_in_active_setup(wa_state) or (wa_state.get('stage') in _SETUP_STAGES)

    if _has_active_bot(wa_state):
        if is_wa_related_message(message, wa_state):
            # Kullanici kurulu botu sifirdan baska numaraya kurmak istiyorsa kurulum ucretsiz
            if has_wa_kw and (
                any(w in low for w in ('yeni bot', 'yeni whatsapp', 'baştan kur', 'bastan kur', 'sıfırdan', 'sifirdan', 'yeniden bağla', 'yeniden bagla'))
                or re.search(r'\byeni\b.*\bbot', low)
            ):
                return 'wa_setup_free', 'free'
            return 'wa_service', 'wa_bot'
        return 'standard', 'ai'

    if setup_active or is_wa_related_message(message, wa_state):
        return 'wa_setup_free', 'free'

    return 'standard', 'ai'


def should_consume_credit(username_lower: str, message: str) -> tuple:
    """Geriye donuk uyumlu kota kontrolu (kurulum adimlari ucretsiz)."""
    mode, bucket = classify_credit_policy(username_lower, message)
    if bucket == 'free':
        return 'setup_free', False
    if bucket == 'wa_bot':
        return 'wa_service', False
    return None, True


def scrub_sensitive_reply(text: str) -> str:
    """Yanitta yanlislikla tekrar edilmis ham API anahtarlarini maskeler."""
    s = str(text or '')
    s = re.sub(r'AIza[0-9A-Za-z_-]{12,}', '[gizli-anahtar]', s)
    s = re.sub(r'gsk_[0-9A-Za-z_-]{12,}', '[gizli-anahtar]', s)
    s = re.sub(r'sk-[A-Za-z0-9_-]{14,}', '[gizli-anahtar]', s)
    return s


def build_ai_context(username_lower: str, message: str = '') -> str:
    """
    Gercek yapay zekanin WhatsApp bot durumunu okuyup canli yonetebilmesi icin
    dinamik durum baglami olusturur (kalip sablon yok).
    """
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    user_rec = database.get_user_by_username(username_lower) or {}
    q_snap = database.user_quota_snapshot(user_rec) if user_rec else {}
    wa_q = q_snap.get('wa_bot') or {'limit': cfg.WA_BOT_QUOTA_LIMIT, 'used': 0, 'remaining': cfg.WA_BOT_QUOTA_LIMIT}

    live_snap = None
    if wa_state.get('connected') or wa_state.get('stage') in ('pairing', 'connected_wait_ack', 'customizing', 'active'):
        try:
            live_snap = get_status_snapshot(username_lower)
        except Exception:
            live_snap = None

    st_summary = {
        'stage': wa_state.get('stage', 'idle'),
        'setup_active': bool(wa_state['setup_session'].get('active')),
        'risk_accepted': bool(wa_state.get('risk_accepted')),
        'phone_saved': _mask_phone_display(wa_state['phone']) if wa_state.get('phone') else '',
        'phone_consent': bool(wa_state.get('phone_consent')),
        'connected': bool((live_snap or {}).get('connected', wa_state.get('connected'))),
        'sleep_state': (live_snap or {}).get('sleep_state', 'idle'),
        'messages_handled': int((live_snap or {}).get('messages_handled') or 0),
        'config': sanitize_config_summary(wa_state),
        'wa_service_quota': {
            'limit': wa_q.get('limit', 10),
            'remaining': wa_q.get('remaining', 10),
        },
    }
    return (
        "CANLI WHATSAPP BOT DURUMU (Aslan AI gercek zamanli kontrol):\n"
        + json.dumps(st_summary, ensure_ascii=False)
        + "\n\nWHATSAPP BOT YONETIM TALIMATLARI:\n"
        "- Kullaniciya asla kalip veya numarali sihirbaz metinleri (1/5, 2/5 vb.) yazma; gercek bir asistan gibi dogal, net ve zeki yanit ver.\n"
        "- Kullanici hangi AI modelini/saglayicisini baglamak istedigini, kurallarini, bekleme suresini veya sorusunu yazdiginda bunu anlayip dogrudan yanitla veya ilgili action ile uygula.\n"
        "- Kurulum ve yapilandirma adimlari kullanicinin mevcut hakkini dusmez (ucretsizdir); kurulum tamamlandiktan sonra WhatsApp bot hizmeti tarafinda toplam 10 hak tanimlidir.\n"
        "- Eslestirme kodu (wa_bot_pair) uretmeden once: (1) WhatsApp hesabi kisitlama/askiya alma riskini ve sorumlulugun kullaniciya ait oldugunu acikca bildirip onayini al, (2) telefon numarasini alip eslestirmeye izin verdigini dogrula. Kullanici konusmada bunlari zaten onayladiysa ve gecerli numara verdiyse dogrudan wa_bot_pair cagir.\n"
        "- wa_bot_pair cagirirken kullanicinin verdigi numarayi params.phone alanina yaz (veya kayitli numara zaten varsa bos birakabilirsin); sahte kod uydurma, kodu arka plan servisi uretir.\n"
        "- Baglanti kurulduktan veya ayarlar alindiktan sonra wa_bot_update ile kurallari, gecikmeyi, ozel AI model/anahtarini kaydedip ilk kurulumu tamamlamak icin finalize_setup: true gonder.\n"
        "- Arka plan kutuphane/sunucu adlarini (Baileys, Vercel vb.) ve ham API anahtarlarini yanitinda ASLA gecirme."
    )


def action_start_setup(username_lower: str, params: dict) -> dict:
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    p = params if isinstance(params, dict) else {}
    if wa_state.get('stage') in ('idle', 'stopped', ''):
        wa_state['stage'] = 'awaiting_risk_consent'
    wa_state['setup_session'] = {
        'active': True,
        'credit_charged': False,
        'started_at': int(time.time()),
    }
    if p.get('risk_accepted') is True:
        wa_state['risk_accepted'] = True
        if wa_state['stage'] == 'awaiting_risk_consent':
            wa_state['stage'] = 'awaiting_phone'
    if p.get('phone'):
        digits = _extract_phone(str(p.get('phone')))
        if digits:
            wa_state['phone'] = digits
            if p.get('phone_consent') is True:
                wa_state['phone_consent'] = True
                wa_state['stage'] = 'awaiting_code_confirm'
            else:
                wa_state['stage'] = 'awaiting_phone_consent'
    database.save_user_wa_bot(username_lower, wa_state)
    return {
        'success': True,
        'operation': 'wa_bot_start_setup',
        'stage': wa_state['stage'],
    }


def action_pair(username_lower: str, params: dict) -> dict:
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    p = params if isinstance(params, dict) else {}
    raw_phone = str(p.get('phone') or wa_state.get('phone') or '').strip()
    digits = _extract_phone(raw_phone)
    if not digits:
        return {
            'success': False,
            'operation': 'wa_bot_pair',
            'error': 'Geçerli bir telefon numarası bulunamadı. Lütfen ülke koduyla birlikte yazın (ör. +90 555 123 45 67).',
        }
    wa_state['phone'] = digits
    wa_state['risk_accepted'] = True
    wa_state['phone_consent'] = True
    wa_state['code_confirmed'] = True
    wa_state['setup_session']['active'] = True
    wa_state['setup_session']['credit_charged'] = False

    pair_res = call_wa_worker(
        'POST',
        '/wa/pair',
        {
            'user_id': username_lower,
            'phone': digits,
            'config': build_runtime_config(wa_state),
        },
        timeout=35,
    )
    if not pair_res.get('success'):
        database.save_user_wa_bot(username_lower, wa_state)
        return {
            'success': False,
            'operation': 'wa_bot_pair',
            'error': pair_res.get('error', 'Eşleştirme kodu şu an alınamadı.'),
            'wa_event': {'type': 'pairing_retry'},
        }

    if pair_res.get('already_connected'):
        wa_state['connected'] = True
        wa_state['stage'] = 'connected_wait_ack'
        database.save_user_wa_bot(username_lower, wa_state)
        return {
            'success': True,
            'operation': 'wa_bot_pair',
            'already_connected': True,
            'wa_event': {'type': 'connected', 'connected': True},
        }

    raw8 = str(pair_res.get('pairing_code') or '')
    formatted = str(pair_res.get('formatted_code') or raw8)
    wa_state['pairing_code'] = raw8
    wa_state['stage'] = 'pairing'
    database.save_user_wa_bot(username_lower, wa_state)
    return {
        'success': True,
        'operation': 'wa_bot_pair',
        'pairing_code': raw8,
        'formatted_code': formatted,
        'wa_event': {
            'type': 'pairing_code',
            'code': raw8,
            'formatted_code': formatted,
            'expires_in': int(pair_res.get('expires_in') or 60),
            'status': 'pairing',
        },
    }


def action_status(username_lower: str) -> dict:
    snap = get_status_snapshot(username_lower)
    ev = None
    if snap.get('connected') and not snap.get('connected_ack'):
        ev = {'type': 'connected', 'connected': True}
    elif snap.get('config_summary'):
        ev = {'type': 'status', 'status': snap}
    return {
        'success': True,
        'operation': 'wa_bot_status',
        'status': snap,
        'wa_event': ev,
    }


def action_wake(username_lower: str) -> dict:
    res = wake_user_bot(username_lower, force=True)
    return {
        'success': bool(res.get('success', False)),
        'operation': 'wa_bot_wake',
        'awake': bool(res.get('awake', False)),
    }


def action_stop(username_lower: str, params: dict) -> dict:
    p = params if isinstance(params, dict) else {}
    res = stop_user_bot(username_lower, logout=bool(p.get('logout')))
    return {
        'success': bool(res.get('success', True)),
        'operation': 'wa_bot_stop',
        'wa_event': {'type': 'config_updated', 'config_summary': res.get('config_summary', {})},
    }


def action_cancel_setup(username_lower: str) -> dict:
    wa_state = _normalize_wa_state(database.get_user_wa_bot(username_lower))
    wa_state['stage'] = 'idle'
    wa_state['setup_session']['active'] = False
    database.save_user_wa_bot(username_lower, wa_state)
    return {
        'success': True,
        'operation': 'wa_bot_cancel_setup',
        'wa_event': {'type': 'setup_cancelled'},
    }

_YES_WORDS = frozenset({
    'evet', 'onay', 'onayliyorum', 'onaylıyorum', 'kabul', 'kabul ediyorum',
    'izin', 'izin veriyorum', 'tamam', 'olur', 'devam', 'devam et',
    'baglandim', 'bağlandım', 'baglandi', 'bağlandı', 'ok', 'yes', 'istiyorum',
    'kodu almayi onayliyorum', 'kodu almayı onaylıyorum',
    'kodu al', 'kodu ver', 'kodu gonder', 'kodu gönder',
    'kodu olustur', 'kodu oluştur', 'olustur', 'oluştur',
    'baslat', 'başlat', 'peki', 'tabi', 'tabii',
    'evet onayliyorum', 'evet onaylıyorum',
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


def _vercel_bridge_host() -> str:
    prod = (os.environ.get('VERCEL_PROJECT_PRODUCTION_URL') or '').strip()
    if prod:
        h = api_utils._parse_env_host(prod)
        if h:
            return h
    v_env = (os.environ.get('VERCEL_ENV') or '').strip().lower()
    v_url = (os.environ.get('VERCEL_URL') or '').strip()
    if v_env != 'production' and v_url:
        h = api_utils._parse_env_host(v_url)
        if h and h.endswith('.vercel.app'):
            return h
    return 'aslansite.vercel.app'


def _worker_base_url() -> str:
    env_url = (os.environ.get('WA_BOT_WORKER_URL') or cfg.WA_BOT_WORKER_URL or '').strip().rstrip('/')
    if env_url:
        return env_url
    if not os.environ.get('VERCEL'):
        port = int(os.environ.get('WA_WORKER_PORT') or cfg.WA_BOT_LOCAL_PORT or 5056)
        return 'http://127.0.0.1:%d' % port
    return 'https://%s/api/wa_bridge' % _vercel_bridge_host()


def _worker_token() -> str:
    explicit = (
        os.environ.get('WA_BOT_WORKER_TOKEN')
        or os.environ.get('MEDIA_WORKER_TOKEN')
        or cfg.WA_BOT_WORKER_TOKEN
        or ''
    ).strip()
    if explicit:
        return explicit
    seed = (
        os.environ.get('QUOTA_SIGNING_SECRET')
        or os.environ.get('BLOB_READ_WRITE_TOKEN')
        or os.environ.get('GEMINI_API_KEY')
        or 'aslan-local-wa-secret'
    ).strip()
    return hmac.new(seed.encode('utf-8'), b'aslan-wa-bridge-v1', hashlib.sha256).hexdigest()


def get_worker_endpoint() -> tuple:
    return _worker_base_url(), _worker_token()


def call_wa_worker(method: str, path: str, payload=None, timeout: int = 25) -> dict:
    base, tok = get_worker_endpoint()
    if not base:
        return {'success': False, 'error': 'Aslan AI WhatsApp eşleştirme servisine geçici olarak ulaşılamadı. Lütfen birkaç saniye sonra tekrar deneyin.'}
    url = '%s%s' % (base, path)
    headers = {'User-Agent': 'Aslan-AI-WA/1.0'}
    if tok:
        headers['X-Worker-Token'] = tok
    data = None
    if method.upper() == 'POST':
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    fallback_err = 'Aslan AI WhatsApp servisi geçici olarak yanıt veremedi.'
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode('utf-8'))
            if isinstance(parsed, dict):
                if parsed.get('error'):
                    parsed['error'] = api_utils.safe_error_message(parsed['error'], fallback_err)
                return parsed
            return {'success': False, 'error': 'Aslan AI bağlantı servisi yanıt veremedi.'}
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode('utf-8', errors='replace'))
            if isinstance(parsed, dict) and parsed.get('error'):
                return {'success': False, 'error': api_utils.safe_error_message(parsed['error'], fallback_err)}
        except Exception:
            pass
        return {'success': False, 'error': fallback_err}
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
    if any(n in cleaned for n in (
        'hayır', 'hayir', 'iptal', 'reddet', 'reddediyorum', 'istemiyorum',
        'vazgeç', 'vazgec', 'vermiyorum', 'onaylamıyorum', 'onaylamiyorum',
        'gönderme', 'gonderme', 'oluşturma', 'olusturma', 'olmaz',
    )):
        return False
    return any(p in cleaned for p in (
        'onaylıyorum', 'onayliyorum', 'kabul ediyorum', 'izin veriyorum',
        'evet', 'onay', 'tamam', 'devam', 'bağlandım', 'baglandim',
        'kodu al', 'kodu ver', 'kodu gönder', 'kodu gonder',
        'kodu oluştur', 'kodu olustur', 'olur', 'peki', 'tabii', 'tabi',
        'başlat', 'baslat', 'oluştur', 'olustur',
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


_ACTIVE_STAGES = _SETUP_STAGES

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
        sleep_state = worker_res.get('sleep_state') or ('sleeping' if wa_state.get('connected') else 'idle')
        messages_handled = int(worker_res.get('messages_handled') or 0)
    else:
        worker_status = wa_state.get('stage', 'idle')
        sleep_state = 'sleeping' if wa_state.get('connected') else 'idle'
        messages_handled = 0

    return {
        'success': True,
        'connected': bool(wa_state.get('connected')),
        'status': worker_status,
        'stage': wa_state.get('stage', 'idle'),
        'sleep_state': sleep_state,
        'messages_handled': messages_handled,
        'setup_active': is_in_active_setup(wa_state),
        'connected_ack': bool(wa_state.get('connected_ack')),
        'notification': 'Bağlantınız başarılı' if wa_state.get('connected') else '',
        'config_summary': sanitize_config_summary(wa_state),
    }


def wake_user_bot(username_lower: str, force: bool = True) -> dict:
    """Bot oturumunu uyku-uyanma dongusuyle sessizce uyandirir."""
    res = call_wa_worker('POST', '/wa/wake', {'user_id': username_lower, 'force': bool(force)}, timeout=10)
    return {
        'success': bool(res.get('success', res.get('ok', False))),
        'awake': bool(res.get('awake', res.get('ok', False))),
        'sleep_wake': bool(res.get('sleep_wake', True)),
        'ts': int(res.get('ts') or time.time()),
    }


def confirm_connected(username_lower: str) -> dict:
    """Kullanici 'Baglantiniz basarili' bildirimini onayladiginda AI esliginde ozellestirmeyi baslatir."""
    from . import ai_engine
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
    wa_state['stage'] = 'customizing'
    wa_state['setup_session']['active'] = True
    database.save_user_wa_bot(username_lower, wa_state)

    ctx = build_ai_context(username_lower)
    ai_prompt = (
        "Kullanıcının WhatsApp cihaz bağlantısı az önce başarıyla onaylandı. "
        "Kalıp veya numaralı liste (1/5 vb.) kullanmadan doğal ve samimi şekilde tebrik et; "
        "botunun kimlere nasıl cevap vereceğini, üslubunu, bekleme/yazıyor davranışını ve isterse "
        "kendi yapay zekasını bağlamak isteyip istemediğini tek akıcı mesajda sor."
    )
    q_reply, _ = ai_engine.generate_reply(ai_prompt, extra_context=ctx)
    if not q_reply:
        q_reply = (
            "✅ **WhatsApp bağlantınız onaylandı!** Botunuzu nasıl yönetelim?\n\n"
            "Bana doğrudan istediğiniz gibi tarif edebilirsiniz: hangi mesajlara veya gruplara yanıt vereceğini, "
            "kaç saniye \"yazıyor...\" göstereceğini, üslubunu/kurallarını ve varsa bağlamak istediğiniz yapay zeka modelini söyleyebilirsiniz."
        )
    return {
        'success': True,
        'reply': scrub_sensitive_reply(q_reply),
        'wa_event': {'type': 'customization_started', 'stage': 'customizing', 'connected': True},
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


def process_action_outcome(action_res, clean_reply: str, orig_reply: str) -> tuple:
    """
    wa_bot_* eylem sonucunu isler:
      Returns (is_wa_action, media_result_out, reply_out, wa_event_out)
    """
    if not isinstance(action_res, dict):
        return False, action_res, orig_reply, None
    op = str(action_res.get('operation') or '').strip()
    if not op.startswith('wa_bot_'):
        return False, action_res, orig_reply, None

    wa_event = action_res.get('wa_event')
    if op == 'wa_bot_pair':
        if not action_res.get('success'):
            err = api_utils.safe_error_message(
                action_res.get('error'),
                'Aslan AI WhatsApp eşleştirme kodu şu an oluşturulamadı. Lütfen tekrar deneyin.',
            )
            return True, None, '⚠️ %s' % err, wa_event or {'type': 'pairing_retry'}
        return True, None, clean_reply or orig_reply or '8 haneli eşleştirme kodunuz hazır!', wa_event

    if op in ('wa_bot_update', 'wa_bot_stop'):
        fallback = 'WhatsApp bot ayarları güncellendi.' if op == 'wa_bot_update' else 'WhatsApp botu durduruldu.'
        return True, None, clean_reply or orig_reply or fallback, wa_event

    return True, None, clean_reply or orig_reply, wa_event


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
    if p.get('phone'):
        digits = _extract_phone(str(p.get('phone')))
        if digits:
            wa_state['phone'] = digits
    if 'risk_accepted' in p:
        wa_state['risk_accepted'] = bool(p['risk_accepted'])
    if 'phone_consent' in p:
        wa_state['phone_consent'] = bool(p['phone_consent'])
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

    if isinstance(p.get('custom_ai'), dict):
        cai_in = p['custom_ai']
        cur_cai = cfg_obj.setdefault('custom_ai', {})
        if cai_in.get('enabled') is False:
            cur_cai.update({
                'enabled': False, 'provider': '', 'model': '', 'endpoint': '',
                'api_key_enc': '', 'has_key': False,
            })
        else:
            raw_key = str(cai_in.get('api_key') or '').strip()
            provider = str(cai_in.get('provider') or cur_cai.get('provider') or '').strip().lower()
            model = str(cai_in.get('model') or cur_cai.get('model') or '').strip()
            endpoint = str(cai_in.get('endpoint') or cur_cai.get('endpoint') or '').strip()
            if raw_key:
                det_p, _ = _detect_provider_and_key(raw_key)
                cur_cai['api_key_enc'] = database.encrypt_secret_value(raw_key)
                cur_cai['has_key'] = True
                provider = provider or det_p or 'openai'
            cur_cai['enabled'] = bool(cai_in.get('enabled', True) and (cur_cai.get('has_key') or cur_cai.get('api_key_enc')))
            cur_cai['provider'] = provider
            cur_cai['model'] = model
            cur_cai['endpoint'] = endpoint

    finalized = bool(p.get('finalize_setup')) or (
        wa_state.get('connected') and wa_state.get('stage') in ('customizing', 'connected_wait_ack', 'custom_triggers', 'custom_groups', 'custom_human', 'custom_tone', 'custom_ai_choice', 'custom_ai_key')
    )
    if finalized:
        if not cfg_obj.get('default_reply') and not (cfg_obj.get('custom_ai') or {}).get('enabled') and not cfg_obj.get('rules'):
            cfg_obj['default_reply'] = 'Merhaba! Mesajınız alındı. Aslan AI WhatsApp asistanı olarak size en kısa sürede yardımcı olacağım.'
        cfg_obj['enabled'] = True
        wa_state['stage'] = 'active'
        wa_state['setup_session']['active'] = False

    if p.get('enabled') is False:
        call_wa_worker('POST', '/wa/stop', {'user_id': username_lower, 'logout': False}, timeout=8)
    else:
        _sync_worker_config(username_lower, wa_state)
    database.save_user_wa_bot(username_lower, wa_state)
    summary = sanitize_config_summary(wa_state)
    ev_type = 'bot_ready' if finalized else 'config_updated'
    return {
        'success': True,
        'operation': 'wa_bot_update',
        'label': 'WhatsApp Bot Güncellemesi',
        'config_summary': summary,
        'wa_event': {'type': ev_type, 'config_summary': summary},
    }


# Statik kalip akisi kaldirildi: tum yonetim Aslan AI + wa_bot_* aksiyonlari uzerinden yurur.
