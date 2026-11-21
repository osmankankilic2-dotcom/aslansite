"""
Aslan AI cekirdegi.

Oncelik Sirasi (Metin Istekleri):
  1) Ucretsiz / anahtarsiz acik uclar (Pollinations OpenAI POST -> Pollinations POST ->
     FREE_AI_ENDPOINTS -> Pollinations GET)
  2) Son Durak Yedek: Mevcut Gemini API yapilandirmasi (GEMINI_API_KEY + model zinciri)

Multimodal (gorsel/video/ses/PDF ekli) istekler dogrudan Gemini API kullanir.
Ayrica yapay zeka asistanindan TTS ve okuma kontrolu (tts / tts_control) saglar.
"""
import asyncio
import base64
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from . import api_utils, config as cfg, database, media_engine

DEFAULT_VOICE = cfg.DEFAULT_VOICE
TTS_MAX_CHARS = cfg.TTS_MAX_CHARS
GEMINI_ENDPOINT = cfg.GEMINI_ENDPOINT
GEMINI_TEXT_MODEL = cfg.GEMINI_TEXT_MODEL
GEMINI_IMAGE_MODEL = cfg.GEMINI_IMAGE_MODEL
GEMINI_LITE_MODEL = cfg.GEMINI_LITE_MODEL
GEMINI_FALLBACK_MODELS = cfg.GEMINI_FALLBACK_MODELS
SUPPORTED_SOCIAL_PLATFORMS = cfg.SUPPORTED_SOCIAL_PLATFORMS
POLLINATIONS_ENDPOINT = cfg.POLLINATIONS_ENDPOINT
IMAGE_MODEL_CHAIN = cfg.IMAGE_MODEL_CHAIN

_RATE_RE = re.compile(r'^[+-]?\d{1,3}%$')
_PITCH_RE = re.compile(r'^[+-]?\d{1,2}Hz$', re.IGNORECASE)


# ===================================================================== yardimcilar
def get_api_key() -> str:
    """Mevcut Gemini/Google API anahtarini dondurur (son durak yedek + multimodal)."""
    for name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_GENAI_API_KEY'):
        key = os.environ.get(name, '').strip()
        if key:
            return key
    return ''


def get_groq_key() -> str:
    """Groq anahtari."""
    return os.environ.get('GROQ_API_KEY', '').strip()


def _post_json(url: str, payload: dict, timeout: int = 90, headers: dict = None):
    """JSON POST istegi yapar ve cozumlenmis yanit dondurur."""
    data = json.dumps(payload).encode('utf-8')
    req_headers = {'Content-Type': 'application/json', 'User-Agent': 'ASLAN-AI/1.0'}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _post_raw_text(url: str, payload: dict, timeout: int = 20, headers: dict = None) -> str:
    """JSON gonderip duz metin veya JSON yaniti okur."""
    data = json.dumps(payload).encode('utf-8')
    req_headers = {'Content-Type': 'application/json', 'User-Agent': 'ASLAN-AI/1.0'}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='replace')


def _get_raw_text(url: str, timeout: int = 18) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': 'ASLAN-AI/1.0'}, method='GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='replace')


def _sanitize_history(history) -> list:
    """Istemciden gelen sohbet gecmisini temizler ve son N turla sinirlar."""
    if not isinstance(history, list):
        return []
    cleaned = []
    for item in history[-(cfg.MAX_HISTORY_TURNS * 2):]:
        if not isinstance(item, dict):
            continue
        raw_role = str(item.get('role', 'user')).strip().lower()
        role = 'model' if raw_role in ('model', 'assistant', 'bot') else 'user'
        text = str(item.get('text') or item.get('content') or '').strip()
        if not text:
            continue
        cleaned.append({'role': role, 'text': text[:2000]})
    return cleaned[-cfg.MAX_HISTORY_TURNS:]


# ===================================================================== sistem istemi
def build_system_prompt(available_files=None, extra_context=''):
    """Aslan AI sistem istemi (arayuzdeki ozellikleri ve otonom eylemleri anlatir)."""
    prompt = """Sen Aslan AI'sin. Kendini her zaman "Aslan AI" olarak tanit; hicbir model veya saglayici adi kullanma. Turkce yanit ver, samimi ve profesyonel ol.

ASLAN SITE OZELLIKLERI:
- Ses Studyosu: Turkce/Ingilizce seslendirme (Ahmet/Emel/Andrew/Ava), hiz ve ton ayari, MP3 indirme
- Aslan AI Asistan: Gorsel, ses, video ve PDF analizi; cok turlu sohbet ve otonom medya/TTS kontrolu
- Medya Studyosu: Video birlestirme, kirpma, ses cikarma, sessize alma, hiz degistirme, ses sikistirma, gorsel ekleme, fotograf slaydi, ses temizleme
- Gorsel Uretimi: Metinden gorsel olusturma (gunluk 3 / haftalik ve aylik toplam 5 hak)
- Hesap & Kota: Kullanici hesabina bagli kalici kullanim haklari (gunluk 5 AI mesaji)

OTONOM EYLEM SISTEMI:
Kullanicinin istegi bir medya/gorsel/ses islemini veya sesli okuma kontrolunu gerektiriyorsa,
yanitinin sonuna asagidaki formatta TEK bir JSON blogu ekle:

```aslan_action
{
  "action": "<operation_name>",
  "files": ["<dosya_id>"],
  "params": {"<param>": "value"}
}
```

Kullanilabilir action degerleri:
- tts                    -> Metni seslendirir ve oynatir: {"action":"tts","files":[],"params":{"text":"...","voice":"tr-TR-AhmetNeural","rate":"+0%","pitch":"+0Hz","autoplay":true}}
- tts_control            -> Ses studyosu ayarlarini ve oynatmayı kontrol eder (command: "play" | "stop" | "pause" | "settings"): {"action":"tts_control","files":[],"params":{"command":"stop","voice":"tr-TR-EmelNeural","rate":"+20%","pitch":"+4Hz","text":"..."}}
- merge_video_image      -> Videoya gorsel ekler (files: [video_id, gorsel_id])
- crop_image             -> Gorsel kirpar (params: x, y, width, height)
- trim_video             -> Video kirpar (params: start "00:00:05", duration 10)
- extract_audio          -> Videodan ses cikarir (params: format "mp3")
- combine_audio_video    -> Ses + video birlestirir (files: [video_id, ses_id])
- overlay_audio          -> Videoya ses ekler (files: [video_id, ses_id])
- stitch_media           -> Fotograf/video birlestirir (files: [id1, id2, ...])
- slideshow_with_music   -> Fotograflardan + arka plan muzigiyle video (files: [foto1..fotoN, ses_id])
- video_concat           -> Iki videoyu birlestirir (files: [video1_id, video2_id])
- merge_audio            -> Ses dosyalarini birlestirir (files: [ses1_id, ses2_id])
- denoise_audio          -> Ses arka plan gurultusunu temizler (files: [ses_id])
- compress_audio         -> Ses sikistirir (params: bitrate "96k", format "mp3")
- mute_video             -> Videoyu sessize alir (files: [video_id])
- change_speed           -> Oynatma hizini degistirir (params: speed 1.25)
- remove_background      -> Gorselden arka plan kaldirir (files: [gorsel_id])
- extract_thumbnail      -> Videodan kapak kare (files: [video_id])
- optimize_video         -> Videoyu sikistirir (files: [video_id])
- convert                -> Format donusturur (params: format "mp4")
- resize                 -> Yeniden boyutlandirir (params: width, height)
- generate_image         -> Metinden gorsel uretir: {"action":"generate_image","files":[],"params":{"prompt":"..."}}
- social_download        -> Video indirir: {"action":"social_download","files":[],"params":{"url":"https://..."}}

KURALLAR:
- Kullanici "bunu sesli oku", "hizi artir", "sesi Emel yap", "okumayi durdur" derse mutlaka uygun "tts" veya "tts_control" JSON blogunu ekle.
- files icindeki degerler kullanicinin yukledigi dosyalarin kimlikleridir (asagida listelenir).
- Slayt/video birlestirmede en fazla 10 fotograf kullanilabilir; dosya sirasi onemlidir.
- Eylem gerekmiyorsa aslan_action blogunu YAZMA.
- Kullaniciya kisa, net ve yardimsever yanit ver.
"""
    if available_files:
        lines = []
        for af in available_files:
            if not isinstance(af, dict):
                continue
            fid = af.get('id', '')
            fname = af.get('name', af.get('original_name', ''))
            ftype = af.get('mime_type', af.get('type', ''))
            if fid:
                lines.append('  - id: %s | ad: %s | tur: %s' % (fid, fname, ftype))
        if lines:
            prompt += ('\n\nKULLANICININ YUKLEDIGI DOSYALAR (bu id\'leri action.files icinde kullan):\n'
                       + '\n'.join(lines))
    if extra_context:
        prompt += '\n\nEK BILGI:\n' + extra_context
    return prompt


def _build_contents(message, parts=None, history=None):
    """Gecmis turlar ve guncel mesaji Gemini contents dizisine cevirir (cift mesaj eklemez)."""
    current_parts = list(parts or [])
    if message:
        has_msg = (
            current_parts
            and isinstance(current_parts[0], dict)
            and current_parts[0].get('text') == message
        )
        if not has_msg:
            current_parts.insert(0, {'text': message})
    if not current_parts:
        return []

    contents = []
    for turn in _sanitize_history(history):
        contents.append({'role': turn['role'], 'parts': [{'text': turn['text']}]})
    contents.append({'role': 'user', 'parts': current_parts})
    return contents


def _build_openai_messages(message, history=None, available_files=None, parts=None):
    """OpenAI uyumlu acik uc noktalar icin mesaj dizisi hazirlar."""
    text_chunks = []
    for p in (parts or []):
        if isinstance(p, dict) and p.get('text') and p.get('text') != message:
            text_chunks.append(str(p.get('text')))
    messages = [{'role': 'system', 'content': build_system_prompt(available_files)}]
    for turn in _sanitize_history(history):
        messages.append({
            'role': 'assistant' if turn['role'] == 'model' else 'user',
            'content': turn['text'],
        })
    user_content = '\n\n'.join([c for c in ([message] + text_chunks) if c]).strip()
    if not user_content:
        return []
    messages.append({'role': 'user', 'content': user_content})
    return messages


def _is_valid_free_reply(text: str) -> bool:
    s = str(text or '').strip()
    if not s or len(s) < 2:
        return False
    low = s[:120].lower()
    if low.startswith('<!doctype') or low.startswith('<html') or 'error 5' in low or 'bad gateway' in low:
        return False
    return True


def _try_free_endpoints(message, history=None, available_files=None, parts=None):
    """
    Hesap/API anahtari gerektirmeyen ucretsiz acik AI uc noktalarini sirayla dener.

    Returns: (yanit_metni, hata_mesaji)
    """
    messages = _build_openai_messages(
        message, history=history, available_files=available_files, parts=parts)
    if not messages:
        return None, 'Mesaj bos'

    last_err = ''

    # 1) Pollinations OpenAI-uyumlu acik uc noktasi
    try:
        data = _post_json(
            cfg.POLLINATIONS_TEXT_OPENAI,
            {'model': 'openai', 'messages': messages, 'temperature': 0.7},
            timeout=18,
        )
        choices = data.get('choices') or []
        content = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
        if _is_valid_free_reply(content):
            return content, None
    except Exception as e:
        last_err = api_utils.safe_error_message(e)

    # 2) Pollinations dogrudan POST metin uc noktasi
    try:
        raw = _post_raw_text(
            cfg.POLLINATIONS_TEXT_POST,
            {'model': 'openai', 'messages': messages},
            timeout=18,
        )
        text = raw.strip()
        if text.startswith('{'):
            try:
                parsed = json.loads(text)
                choices = parsed.get('choices') or []
                if choices:
                    text = ((choices[0].get('message') or {}).get('content') or '').strip()
            except Exception:
                pass
        if _is_valid_free_reply(text):
            return text, None
    except Exception as e:
        last_err = api_utils.safe_error_message(e)

    # 3) Opsiyonel topluluk / g4f uyumlu anahtarsiz proxy uc noktalari (FREE_AI_ENDPOINTS)
    extra_urls = [u.strip() for u in os.environ.get('FREE_AI_ENDPOINTS', '').split(',') if u.strip()]
    for url in extra_urls:
        try:
            data = _post_json(url, {'model': 'gpt-4o-mini', 'messages': messages}, timeout=15)
            choices = data.get('choices') or []
            content = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
            if _is_valid_free_reply(content):
                return content, None
        except Exception as e:
            last_err = api_utils.safe_error_message(e)

    # 4) Pollinations GET acik uc noktasi (kisa metin yedegi)
    try:
        user_msg = messages[-1]['content'][:1000]
        sys_short = 'Sen Aslan AI sin. Turkce, kisa, samimi ve yardimsever yanit ver.'
        qs = urllib.parse.urlencode({'model': 'openai', 'system': sys_short})
        url = '%s/%s?%s' % (cfg.POLLINATIONS_TEXT_GET, urllib.parse.quote(user_msg, safe=''), qs)
        text = _get_raw_text(url, timeout=15).strip()
        if _is_valid_free_reply(text):
            return text, None
    except Exception as e:
        last_err = api_utils.safe_error_message(e)

    return None, last_err or 'Ucretsiz uc noktalar yanit vermedi'


def _try_gemini_reply(message, parts=None, available_files=None, api_key=None, history=None):
    """Kullanicinin mevcut Gemini API yapilandirmasiyla yanit uretir (son durak / multimodal)."""
    key = api_key or get_api_key()
    if not key:
        return None, 'API anahtari yapilandirilmamis'
    contents = _build_contents(message, parts=parts, history=history)
    if not contents:
        return None, 'Mesaj bos'

    payload = {
        'contents': contents,
        'systemInstruction': {'parts': [{'text': build_system_prompt(available_files)}]},
        'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 2048},
    }
    headers = {'x-goog-api-key': key}
    models = [GEMINI_TEXT_MODEL] + list(GEMINI_FALLBACK_MODELS)
    last_error = 'Model yanit vermedi'
    for model in models:
        url = '%s/%s:generateContent' % (GEMINI_ENDPOINT, model)
        try:
            data = _post_json(url, payload, timeout=90, headers=headers)
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode('utf-8', errors='replace')[:200]
            except Exception:
                pass
            last_error = 'AI servis hatasi (%s): %s' % (
                e.code, api_utils.safe_error_message(body, 'istek reddedildi'))
            continue
        except Exception as e:
            last_error = 'Baglanti hatasi: %s' % api_utils.safe_error_message(e)
            continue
        reply = _extract_text(data)
        if reply:
            return reply, None
        last_error = 'Model yanit vermedi'
    return None, last_error


def generate_reply(message, parts=None, available_files=None, api_key=None, history=None):
    """
    Metin isteklerinde once ucretsiz/anahtarsiz acik uclari dener;
    hepsi hata verirse son durak olarak mevcut Gemini API yapilandirmasina gecer.
    Multimodal (gorsel/ses/video/PDF) isteklerde dogrudan Gemini API kullanir.

    Returns: (yanit_metni, hata_mesaji)
    """
    contents = _build_contents(message, parts=parts, history=history)
    if not contents:
        return None, 'Mesaj bos'

    has_binary = any(
        isinstance(p, dict) and ('inline_data' in p or 'inlineData' in p)
        for p in (parts or [])
    )

    free_err = ''
    if not has_binary:
        reply, free_err = _try_free_endpoints(
            message, history=history, available_files=available_files, parts=parts)
        if reply:
            return reply, None

    gem_reply, gem_err = _try_gemini_reply(
        message, parts=parts, available_files=available_files, api_key=api_key, history=history)
    if gem_reply:
        return gem_reply, None

    return None, gem_err or free_err or 'Yanit alinamadi'


def generate_reply_lite(message, parts=None, available_files=None, api_key=None, history=None):
    """Hafif modelle yanit uretir."""
    return generate_reply(message, parts=parts, available_files=available_files,
                          api_key=api_key, history=history)


def _extract_text(data):
    """Gemini yanitindan metni cikarir."""
    try:
        candidates = data.get('candidates') or []
        parts = (candidates[0].get('content') or {}).get('parts') or []
        chunks = [p.get('text', '') for p in parts if p.get('text')]
        text = ''.join(chunks).strip()
        return text or None
    except Exception:
        return None


# ===================================================================== gorsel uretimi
def generate_image(prompt, device_id=None, api_key=None, username_lower=None):
    """
    Metinden gorsel uretir ve kota kontrolu yapar.
    `username_lower` verilmisse kota dogrudan veritabanindaki kullanici hesabina baglanir.
    Kota yalnizca uretim basarili olduktan sonra dusulur.
    """
    from . import quota

    prompt = str(prompt or '').strip()
    if not prompt:
        return False, {'success': False, 'quota_exceeded': False,
                       'error': 'Gorsel icin aciklama gerekli'}
    if len(prompt) > cfg.MAX_PROMPT_CHARS:
        return False, {'success': False, 'quota_exceeded': False,
                       'error': 'Aciklama en fazla %d karakter olabilir' % cfg.MAX_PROMPT_CHARS}

    if username_lower:
        allowed, status = database.check_user_image_quota(username_lower)
    else:
        allowed, status = quota.check_image(device_id or 'anonymous')

    if not allowed:
        return False, {
            'success': False,
            'quota_exceeded': True,
            'error': ('Gunluk gorsel hakkiniz (%d) veya haftalik/aylik toplam hakkiniz (%d) doldu.'
                      % (status.get('daily_limit', cfg.IMAGE_DAILY_LIMIT),
                         status.get('weekly_limit', cfg.IMAGE_WEEKLY_LIMIT))),
            'quota': status,
        }

    # Once ucretsiz Pollinations veya Gemini gorsel zinciri (kullanicinin gorsel kotasini korur)
    image_b64 = None
    mime = 'image/png'
    last_error = None
    engine = None

    key = api_key or get_api_key()
    headers = {'x-goog-api-key': key} if key else None

    for model in (IMAGE_MODEL_CHAIN if key else ()):
        if image_b64:
            break
        try:
            if model.startswith('imagen'):
                url = '%s/%s:predict' % (GEMINI_ENDPOINT, model)
                payload = {'instances': [{'prompt': prompt}],
                           'parameters': {'sampleCount': 1, 'aspectRatio': '1:1'}}
                data = _post_json(url, payload, timeout=120, headers=headers)
                for pred in data.get('predictions') or []:
                    b64 = pred.get('bytesBase64Encoded')
                    if b64:
                        image_b64 = b64
                        mime = pred.get('mimeType') or mime
                        engine = model
                        break
            else:
                url = '%s/%s:generateContent' % (GEMINI_ENDPOINT, model)
                payload = {
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'responseModalities': ['IMAGE', 'TEXT']},
                }
                data = _post_json(url, payload, timeout=120, headers=headers)
                for cand in data.get('candidates') or []:
                    for part in ((cand.get('content') or {}).get('parts') or []):
                        inline = part.get('inlineData') or part.get('inline_data')
                        if inline and inline.get('data'):
                            image_b64 = inline['data']
                            mime = inline.get('mimeType') or inline.get('mime_type') or mime
                            engine = model
                            break
                    if image_b64:
                        break
        except Exception as e:
            last_error = api_utils.safe_error_message(e)

    if not image_b64:
        image_b64, mime = _pollinations_image(prompt)
        if image_b64:
            engine = 'pollinations'

    if not image_b64:
        cur_status = (database.check_user_image_quota(username_lower)[1]
                      if username_lower else quota.image_status(device_id or 'anonymous'))
        return False, {
            'success': False,
            'quota_exceeded': False,
            'error': 'Gorsel uretilemedi%s' % (': %s' % last_error if last_error else ''),
            'quota': cur_status,
        }

    if username_lower:
        _, updated_status = database.consume_user_image_quota(username_lower)
    else:
        _, updated_status = quota.consume_image(device_id or 'anonymous')

    return True, {
        'success': True,
        'image_base64': image_b64,
        'mime_type': mime,
        'prompt': prompt,
        'quota': updated_status,
        'engine': engine or 'gemini',
    }


def _pollinations_image(prompt, width=1024, height=1024, timeout=120):
    """Anahtarsiz gorsel uretimi yedegi."""
    try:
        from urllib.parse import quote
        url = '%s/%s?width=%d&height=%d&nologo=true&seed=%s' % (
            POLLINATIONS_ENDPOINT, quote(prompt, safe=''), width, height,
            str(abs(hash(prompt)) % 100000))
        req = urllib.request.Request(url, headers={'User-Agent': 'ASLAN-Media/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
            ctype = resp.headers.get('Content-Type') or 'image/jpeg'
        if not blob or len(blob) < 512 or not ctype.startswith('image/'):
            return None, None
        return base64.b64encode(blob).decode('ascii'), ctype
    except Exception:
        return None, None


# ===================================================================== TTS
def normalize_tts_voice(voice: str) -> str:
    v = str(voice or '').strip()
    return v if v in cfg.ALLOWED_TTS_VOICES else DEFAULT_VOICE


def normalize_tts_rate(rate: str) -> str:
    raw = str(rate or '+0%').strip()
    if not _RATE_RE.fullmatch(raw):
        return '+0%'
    val = max(-50, min(100, int(raw[:-1])))
    return '%+d%%' % val


def normalize_tts_pitch(pitch: str) -> str:
    raw = str(pitch or '+0Hz').strip()
    if not _PITCH_RE.fullmatch(raw):
        return '+0Hz'
    val = max(-50, min(50, int(raw[:-2])))
    return '%+dHz' % val


def synthesize_speech(text, voice=DEFAULT_VOICE, rate='+0%', pitch='+0Hz'):
    """Metni sese donusturur. (ses_verisi_bytes, motor_adi) dondurur."""
    text = str(text or '').strip()
    if not text:
        return None, None
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]
    voice = normalize_tts_voice(voice)
    rate = normalize_tts_rate(rate)
    pitch = normalize_tts_pitch(pitch)

    data, engine_name = _try_edge_tts(text, voice, rate=rate, pitch=pitch)
    if data:
        return data, engine_name

    lang = 'en' if voice.startswith('en-') else 'tr'
    data = _try_gtts(text, lang=lang)
    if data:
        return data, 'gTTS'
    return None, None


async def _edge_tts_generate(text, voice, rate='+0%', pitch='+0Hz'):
    import edge_tts
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        await communicate.save(tmp_path)
        with open(tmp_path, 'rb') as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _try_edge_tts(text, voice, rate='+0%', pitch='+0Hz'):
    for _ in range(3):
        try:
            return asyncio.run(_edge_tts_generate(text, voice, rate=rate, pitch=pitch)), 'edge-tts'
        except Exception as e:
            err = str(e)
            if 'NoAudioReceived' in err or '403' in err:
                continue
            break
    return None, None


def _try_gtts(text, lang='tr'):
    try:
        import io
        from gtts import gTTS
        tts = gTTS(text=text, lang=lang)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception:
        return None


# ===================================================================== eylemler
def extract_action(text):
    """Yanit icindeki aslan_action JSON blogunu ayristirir (yoksa None)."""
    if not text or 'aslan_action' not in text:
        return None
    match = re.search(r'```\s*aslan_action\s*(.*?)```', text, re.DOTALL)
    payload = match.group(1) if match else None
    if not payload:
        brace = re.search(r'\{[^{}]*"action"\s*:.*?\}', text, re.DOTALL)
        payload = brace.group(0) if brace else None
    if not payload:
        return None
    try:
        action = json.loads(payload.strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(action, dict) or not action.get('action'):
        return None
    return action


def strip_action(text):
    """Yanittan aslan_action blogunu temizler."""
    cleaned = re.sub(r'```\s*aslan_action\s*.*?```', '', text or '', flags=re.DOTALL)
    cleaned = re.sub(r'```\s*aslan_action\s*\{.*$', '', cleaned, flags=re.DOTALL)
    return cleaned.strip()


def resolve_file_paths(file_ids, uploaded_files=None):
    """Dosya kimliklerini guvenli sekilde tam yola cevirir."""
    from .config import MEDIA_TEMP_DIR
    mapping = {}
    for meta in (uploaded_files or []):
        if not isinstance(meta, dict):
            continue
        fid = meta.get('id')
        path = meta.get('path')
        if fid and path and os.path.exists(path):
            mapping[str(fid)] = path

    paths = []
    for fid in file_ids or []:
        key = str(fid)
        path = mapping.get(key)
        if not path:
            name = os.path.basename(key)
            candidate = os.path.join(MEDIA_TEMP_DIR, name)
            path = candidate if (os.path.exists(candidate)
                                 and os.path.isfile(candidate)) else None
        if path:
            paths.append(path)
    return paths


def attach_download(descriptor, path):
    """Sonuc tanimlayicisina indirilebilir kaynak (inline data URL veya Vercel Blob) ekler."""
    from . import blob_store
    from .config import MAX_INLINE_RESULT_BYTES
    try:
        size = os.path.getsize(path)
    except OSError:
        return descriptor

    if size <= MAX_INLINE_RESULT_BYTES:
        try:
            with open(path, 'rb') as fh:
                payload = base64.b64encode(fh.read()).decode('ascii')
            descriptor['data_url'] = 'data:%s;base64,%s' % (
                descriptor.get('mime_type', 'application/octet-stream'), payload)
            return descriptor
        except OSError:
            pass

    try:
        uploaded = blob_store.upload_file(path)
    except Exception:
        uploaded = None
    if uploaded and uploaded.get('url'):
        descriptor['download_url'] = uploaded['url']
        descriptor['blob_pathname'] = uploaded.get('pathname', '')
    return descriptor


def run_action(action, uploaded_files=None, device_id=None, username_lower=None):
    """AI tarafindan istenen eylemi otonom olarak calistirir."""
    action = action or {}
    name = str(action.get('action', '')).strip()
    params = action.get('params') or {}
    file_ids = action.get('files') or []

    if name == 'tts':
        return run_tts_action(params)

    if name == 'tts_control':
        return run_tts_control_action(params)

    if name == 'generate_image':
        prompt = str(params.get('prompt', '')).strip()
        ok, result = generate_image(
            prompt, device_id=device_id or 'anonymous', username_lower=username_lower)
        if not ok:
            return {'success': False, 'operation': name,
                    'error': result.get('error', 'Gorsel uretilemedi'),
                    'quota': result.get('quota', {})}
        b64 = result['image_base64']
        mime = result['mime_type']
        return {
            'success': True,
            'operation': 'generate_image',
            'label': 'Görsel üretimi',
            'image_base64': b64,
            'mime_type': mime,
            'data_url': 'data:%s;base64,%s' % (mime, b64),
            'output_file': 'aslan-gorsel.png',
            'prompt': result.get('prompt', ''),
            'engine': result.get('engine', 'gemini'),
            'quota': result.get('quota', {}),
        }

    if name == 'social_download':
        return run_social_download_action(params)

    if name in media_engine.SUPPORTED_MEDIA_OPERATIONS:
        paths = resolve_file_paths(file_ids, uploaded_files)
        if not paths:
            return {'success': False, 'operation': name,
                    'error': 'Bu islem icin gerekli dosya secilmedi.'}
        out_path = media_engine.new_output_path(operation=name, params=params)
        ok, result = media_engine.execute_media_operation(name, paths, out_path, params)
        if ok:
            descriptor = media_engine.result_descriptor(name, result)
            try:
                descriptor['summary'] = media_engine.media_summary(result)
            except Exception:
                pass
            attach_download(descriptor, result)
            return descriptor
        return {'success': False, 'operation': name,
                'error': api_utils.safe_error_message(result)}

    return {'success': False, 'operation': name, 'error': 'Bilinmeyen islem: %s' % name}


def run_tts_control_action(params):
    """AI tarafindan verilen ses studyosu kontrol komutlarini (durdur/baslat/ayar) paketler."""
    p = params if isinstance(params, dict) else {}
    command = str(p.get('command', 'settings')).strip().lower()
    if command not in ('play', 'stop', 'pause', 'settings'):
        command = 'settings'
    res = {
        'success': True,
        'operation': 'tts_control',
        'label': 'Ses kontrolü',
        'command': command,
    }
    if p.get('voice'):
        res['voice'] = normalize_tts_voice(p.get('voice'))
    if p.get('rate') is not None:
        res['rate'] = normalize_tts_rate(p.get('rate'))
    if p.get('pitch') is not None:
        res['pitch'] = normalize_tts_pitch(p.get('pitch'))
    if p.get('text'):
        res['text'] = str(p.get('text')).strip()[:TTS_MAX_CHARS]
    return res


def run_tts_action(params):
    """Metinden ses uretir ve indirilebilir dosya tanimlayicisi dondurur."""
    import uuid
    from .config import MEDIA_TEMP_DIR

    text = str((params or {}).get('text', '')).strip()
    voice = normalize_tts_voice((params or {}).get('voice', DEFAULT_VOICE))
    rate = normalize_tts_rate((params or {}).get('rate', '+0%'))
    pitch = normalize_tts_pitch((params or {}).get('pitch', '+0Hz'))
    autoplay = bool((params or {}).get('autoplay', True))
    if not text:
        return {'success': False, 'operation': 'tts', 'error': 'Seslendirilecek metin bos'}

    audio, engine_name = synthesize_speech(text, voice, rate=rate, pitch=pitch)
    if not audio:
        return {'success': False, 'operation': 'tts',
                'error': 'Ses uretilemedi (ses motoruna ulasilamadi)'}

    fname = '%s.mp3' % uuid.uuid4().hex
    out_path = os.path.join(MEDIA_TEMP_DIR, fname)
    try:
        with open(out_path, 'wb') as f:
            f.write(audio)
    except Exception as e:
        return {'success': False, 'operation': 'tts',
                'error': 'Ses dosyasi yazilamadi: %s' % api_utils.safe_error_message(e)}

    descriptor = media_engine.result_descriptor('tts', out_path)
    descriptor['text'] = text
    descriptor['voice'] = voice
    descriptor['rate'] = rate
    descriptor['pitch'] = pitch
    descriptor['autoplay'] = autoplay
    descriptor['engine'] = engine_name
    attach_download(descriptor, out_path)
    return descriptor


# ===================================================================== sosyal medya
def is_supported_social_url(url):
    """Desteklenen platformlara ait guvenli baglanti mi?"""
    ok, _ = api_utils.validate_social_url(url)
    return ok


def run_social_download_action(params):
    """Sosyal medya platformlarindan video indirir (SSRF korumali)."""
    url = str((params or {}).get('url', '')).strip()
    ok, res = api_utils.validate_social_url(url)
    if not ok:
        return {'success': False, 'operation': 'social_download', 'error': res}
    return download_social_media(res, params)


def download_social_media(url, params=None):
    """TikTok / YouTube / Facebook / Instagram vb. kaynaklardan video indirir."""
    import uuid
    from .config import MEDIA_TEMP_DIR

    ok, clean_or_err = api_utils.validate_social_url(url)
    if not ok:
        return {'success': False, 'operation': 'social_download', 'error': clean_or_err}
    url = clean_or_err

    params = params or {}
    try:
        import yt_dlp
    except ImportError:
        return {'success': False, 'operation': 'social_download',
                'error': 'Video indirme motoru sunucuda kullanilamiyor (yt-dlp eksik)'}

    out_dir = os.path.join(MEDIA_TEMP_DIR, 'social')
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass

    outtmpl = os.path.join(out_dir, '%(id)s.%(ext)s')
    options = {
        'outtmpl': outtmpl,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
        'merge_output_format': 'mp4',
        'socket_timeout': 30,
        'retries': 2,
        'max_filesize': cfg.MAX_FILE_SIZE,
    }
    if params.get('audio_only'):
        options['format'] = 'bestaudio/best'

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get('_type') == 'playlist' and info.get('entries'):
                info = info['entries'][0]
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                candidates = [os.path.join(out_dir, f) for f in os.listdir(out_dir)]
                candidates = [c for c in candidates if os.path.isfile(c)]
                path = max(candidates, key=os.path.getmtime) if candidates else path
            if not os.path.exists(path):
                return {'success': False, 'operation': 'social_download',
                        'error': 'Video indirilemedi'}

            ext = api_utils.safe_extension(path) or '.mp4'
            final = '%s%s' % (uuid.uuid4().hex, ext)
            final_path = os.path.join(MEDIA_TEMP_DIR, final)
            try:
                if os.path.abspath(path) != os.path.abspath(final_path):
                    os.replace(path, final_path)
                path = final_path
            except Exception:
                final = os.path.basename(path)

            descriptor = media_engine.result_descriptor(
                'social_download', path,
                {
                    'title': info.get('title', ''),
                    'uploader': info.get('uploader', ''),
                    'duration': info.get('duration', 0),
                    'platform': info.get('extractor_key', ''),
                },
            )
            descriptor['output_file'] = final
            descriptor['download_url'] = '/api/media/download/%s' % final
            try:
                descriptor['summary'] = media_engine.media_summary(path)
            except Exception:
                pass
            attach_download(descriptor, path)
            return descriptor
    except Exception as e:
        return {'success': False, 'operation': 'social_download',
                'error': 'Video indirilemedi: %s' % api_utils.safe_error_message(e)}
