"""
Aslan AI Birlesik Yapay Zeka Saglayici, Model ve Ozel Uc Nokta Ayarlari.

Kullanicinin sectigi hazir saglayiciyi (Google Gemini, OpenAI, OpenRouter, Groq, DeepSeek,
Mistral, Anthropic) veya SSRF-dogrulamali Ozel HTTPS Uc Noktayi sifreli sekilde saklar,
hem site ici Aslan AI sohbetine hem de otonom olarak arka plan WhatsApp botuna uygular.
"""
import copy
import time

from . import api_utils, config as cfg, database

PROVIDER_CATALOG = [
    {
        'id': 'default',
        'label': 'Aslan AI (Varsayılan Ücretsiz)',
        'default_model': '',
        'endpoint': '',
        'requires_key': False,
        'requires_endpoint': False,
        'model_placeholder': 'Otomatik ücretsiz akıllı zincir',
    },
    {
        'id': 'gemini',
        'label': 'Google Gemini',
        'default_model': cfg.GEMINI_TEXT_MODEL,
        'endpoint': cfg.GEMINI_ENDPOINT,
        'requires_key': True,
        'requires_endpoint': False,
        'model_placeholder': cfg.GEMINI_TEXT_MODEL,
    },
    {
        'id': 'openai',
        'label': 'OpenAI',
        'default_model': 'gpt-4o-mini',
        'endpoint': 'https://api.openai.com/v1/chat/completions',
        'requires_key': True,
        'requires_endpoint': False,
        'model_placeholder': 'gpt-4o-mini',
    },
    {
        'id': 'openrouter',
        'label': 'OpenRouter',
        'default_model': 'openai/gpt-4o-mini',
        'endpoint': 'https://openrouter.ai/api/v1/chat/completions',
        'requires_key': True,
        'requires_endpoint': False,
        'model_placeholder': 'openai/gpt-4o-mini',
    },
    {
        'id': 'groq',
        'label': 'Groq',
        'default_model': cfg.GROQ_TEXT_MODEL,
        'endpoint': cfg.GROQ_ENDPOINT,
        'requires_key': True,
        'requires_endpoint': False,
        'model_placeholder': cfg.GROQ_TEXT_MODEL,
    },
    {
        'id': 'deepseek',
        'label': 'DeepSeek',
        'default_model': 'deepseek-chat',
        'endpoint': 'https://api.deepseek.com/v1/chat/completions',
        'requires_key': True,
        'requires_endpoint': False,
        'model_placeholder': 'deepseek-chat',
    },
    {
        'id': 'mistral',
        'label': 'Mistral',
        'default_model': 'mistral-small-latest',
        'endpoint': 'https://api.mistral.ai/v1/chat/completions',
        'requires_key': True,
        'requires_endpoint': False,
        'model_placeholder': 'mistral-small-latest',
    },
    {
        'id': 'anthropic',
        'label': 'Anthropic',
        'default_model': 'claude-3-5-haiku-latest',
        'endpoint': 'https://api.anthropic.com/v1/messages',
        'requires_key': True,
        'requires_endpoint': False,
        'model_placeholder': 'claude-3-5-haiku-latest',
    },
    {
        'id': 'custom',
        'label': 'Özel Sağlayıcı / Özel Uç Nokta (Custom Endpoint)',
        'default_model': '',
        'endpoint': '',
        'requires_key': False,
        'requires_endpoint': True,
        'model_placeholder': 'Özel model adı (ör. qwen-2.5-72b)',
    },
]

_PROVIDER_MAP = {p['id']: p for p in PROVIDER_CATALOG}


def get_provider_spec(provider_id: str) -> dict:
    pid = str(provider_id or 'default').strip().lower()
    return dict(_PROVIDER_MAP.get(pid) or _PROVIDER_MAP['default'])


def mask_api_key(key: str) -> str:
    raw = str(key or '').strip()
    if not raw:
        return ''
    if len(raw) <= 8:
        return raw[:2] + '••••'
    return raw[:4] + '••••••' + raw[-4:]


def normalize_stored_settings(raw) -> dict:
    src = raw if isinstance(raw, dict) else {}
    pid = str(src.get('provider') or 'default').strip().lower()
    if pid not in _PROVIDER_MAP:
        pid = 'default'
    enc = str(src.get('api_key_enc') or '').strip()
    return {
        'provider': pid,
        'model': str(src.get('model') or '').strip()[:128],
        'endpoint': str(src.get('endpoint') or '').strip()[:512],
        'api_key_enc': enc,
        'has_api_key': bool(enc or src.get('has_api_key')),
        'apply_to_chat': bool(src.get('apply_to_chat', True)),
        'apply_to_whatsapp': bool(src.get('apply_to_whatsapp', True)),
        'updated_at': int(src.get('updated_at') or 0),
    }


def sanitize_settings_for_client(raw) -> dict:
    norm = normalize_stored_settings(raw)
    masked = ''
    if norm.get('api_key_enc'):
        plain = database.decrypt_secret_value(norm['api_key_enc'])
        if plain:
            masked = mask_api_key(plain)
        else:
            norm['has_api_key'] = False
    return {
        'provider': norm['provider'],
        'model': norm['model'],
        'endpoint': norm['endpoint'],
        'has_api_key': bool(norm['has_api_key'] and masked),
        'api_key_masked': masked,
        'apply_to_chat': bool(norm['apply_to_chat']),
        'apply_to_whatsapp': bool(norm['apply_to_whatsapp']),
        'updated_at': norm['updated_at'],
    }


def build_runtime_ai_settings(username_lower: str) -> dict:
    """Sunucu ici calisma zamani ayarlarini cozer (asla disariya dondurulmez)."""
    raw = database.get_user_ai_settings(username_lower)
    norm = normalize_stored_settings(raw)
    spec = get_provider_spec(norm['provider'])
    key = database.decrypt_secret_value(norm['api_key_enc']) if norm['api_key_enc'] else ''
    return {
        'provider': norm['provider'],
        'model': norm['model'] or spec.get('default_model', ''),
        'endpoint': norm['endpoint'] or spec.get('endpoint', ''),
        'api_key': key,
        'apply_to_chat': norm['apply_to_chat'],
        'apply_to_whatsapp': norm['apply_to_whatsapp'],
        'updated_at': norm['updated_at'],
    }


def get_client_settings_payload(username_lower: str) -> dict:
    raw = database.get_user_ai_settings(username_lower)
    return {
        'success': True,
        'settings': sanitize_settings_for_client(raw),
        'providers': copy.deepcopy(PROVIDER_CATALOG),
    }


def update_user_ai_settings(username_lower: str, data: dict):
    """
    Kullanici AI saglayici, model, ozel HTTPS uc nokta ve API anahtari ayarlarini dogrular,
    sifreli olarak kaydeder ve bagli WhatsApp botunu otonom sekilde gunceller.

    Returns: (ok, payload_or_err, status_code)
    """
    from . import wa_bot

    if not username_lower:
        return False, 'Kullanıcı oturumu gerekli.', 401
    if not isinstance(data, dict):
        return False, 'Geçersiz ayar verisi.', 400

    prev = normalize_stored_settings(database.get_user_ai_settings(username_lower))
    pid = str(data.get('provider') or 'default').strip().lower()
    if pid not in _PROVIDER_MAP:
        return False, 'Desteklenmeyen yapay zeka sağlayıcısı.', 400

    spec = _PROVIDER_MAP[pid]
    model = str(data.get('model') or '').strip()[:128]
    endpoint = str(data.get('endpoint') or '').strip()[:512]
    raw_key = str(data.get('api_key') or '').strip()
    clear_key = bool(data.get('clear_api_key'))
    apply_to_chat = bool(data.get('apply_to_chat', True))
    apply_to_wa = bool(data.get('apply_to_whatsapp', True))

    if pid == 'default':
        stored = {
            'provider': 'default',
            'model': '',
            'endpoint': '',
            'api_key_enc': '',
            'has_api_key': False,
            'apply_to_chat': apply_to_chat,
            'apply_to_whatsapp': apply_to_wa,
            'updated_at': int(time.time()),
        }
    else:
        if pid == 'custom':
            ok_ep, ep_or_err = api_utils.validate_custom_ai_endpoint(endpoint)
            if not ok_ep:
                return False, ep_or_err, 400
            endpoint = ep_or_err
        elif endpoint:
            ok_ep, ep_or_err = api_utils.validate_custom_ai_endpoint(endpoint)
            if not ok_ep:
                return False, ep_or_err, 400
            endpoint = ep_or_err

        if clear_key:
            api_key_enc = ''
        elif raw_key:
            api_key_enc = database.encrypt_secret_value(raw_key)
        else:
            api_key_enc = prev.get('api_key_enc', '')

        has_key = bool(api_key_enc)
        if spec.get('requires_key') and not has_key:
            return False, '%s için geçerli bir API anahtarı girmelisiniz.' % spec['label'], 400

        if not model:
            model = spec.get('default_model', '')
        if pid == 'custom' and not model:
            return False, 'Özel sağlayıcı için kullanılacak model adını girmelisiniz.', 400

        stored = {
            'provider': pid,
            'model': model,
            'endpoint': endpoint,
            'api_key_enc': api_key_enc,
            'has_api_key': has_key,
            'apply_to_chat': apply_to_chat,
            'apply_to_whatsapp': apply_to_wa,
            'updated_at': int(time.time()),
        }

    if not database.save_user_ai_settings(username_lower, stored):
        return False, 'Ayarlar kaydedilemedi.', 500

    wa_synced = False
    try:
        wa_synced = bool(wa_bot.sync_custom_ai_from_settings(username_lower, stored))
    except Exception:
        wa_synced = False

    return True, {
        'success': True,
        'settings': sanitize_settings_for_client(stored),
        'providers': copy.deepcopy(PROVIDER_CATALOG),
        'wa_synced': wa_synced,
    }, 200
