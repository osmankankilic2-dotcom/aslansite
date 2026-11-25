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

from . import api_utils, config as cfg, database, media_engine, worker_client

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
    prompt = """Sen Aslan AI'sin. Kendini her zaman yalnizca "Aslan AI" olarak tanit. Kullaniciya verdigin yanitlarda arka planda kullanilan hicbir yapay zeka modeli, saglayici, API, kutuphane veya arac adini (Gemini, Pollinations, OpenAI, GPT, Mistral, Flux, Imagen, FFmpeg, yt-dlp, Baileys, edge-tts, gTTS, Pillow vb.) ASLA soyleme ve ima etme; tum yetenekleri yalnizca "Aslan AI" olarak sun. Turkce yanit ver, samimi ve profesyonel ol.

ASLAN SITE OZELLIKLERI:
- Ses Studyosu: Turkce/Ingilizce seslendirme (Ahmet/Emel/Andrew/Ava), hiz ve ton ayari, MP3 indirme
- Aslan AI Asistan: Gorsel, ses, video ve PDF analizi; sesli mesajdan metne donusum; cok turlu sohbet ve otonom medya/TTS kontrolu
- Aslan AI Medya Motoru: Video/gorsel/ses birlestirme, gorsel+ses video olusturma, metin bindirme (add_text), altyazi ekleme (burn_subtitles), filtreleme (apply_filter), kirpma, ses cikarma, sessize alma, hiz degistirme, fotograf slaydi, video baglantisindan medya indirme
- Gorsel Uretimi: Metinden gorsel olusturma (kullanici basina aylik toplam 2 gorsel hakki)
- Hesap & Kota: Kullanici hesabina bagli kalici kullanim haklari (gunluk 5 AI mesaji, aylik 2 gorsel)

OTONOM EYLEM VE ZINCIRLEME SISTEMI:
Kullanicinin istegi bir medya/gorsel/ses islemini veya cok adimli uretim/duzenlemeyi gerektiriyorsa,
yanitinin sonuna asagidaki formatta TEK bir ```aslan_action``` JSON blogu ekle:

Tek adim icin:
```aslan_action
{
  "action": "<operation_name>",
  "files": ["<dosya_id_veya_$referans>"],
  "params": {"<param>": "value"}
}
```

Cok adimli uretim ve medya isleme zinciri icin (en fazla 5 adim):
```aslan_action
{
  "steps": [
    {"id": "img1", "action": "generate_image", "params": {"prompt": "..."}},
    {"id": "img2", "action": "generate_image", "params": {"prompt": "..."}},
    {"id": "vid1", "action": "slideshow_with_music", "files": ["$img1", "$img2"], "params": {"per_image_duration": 3, "transition": "fade"}}
  ]
}
```
Not: Onceki adimin ciktisini sonraki adimda "$id", "$prev", "$last_image", "$last_video" veya "$last_audio" ile referanslayabilirsin.

Kullanilabilir action degerleri:
- tts                    -> Metni seslendirir: {"action":"tts","files":[],"params":{"text":"...","voice":"tr-TR-AhmetNeural","rate":"+0%","pitch":"+0Hz","autoplay":true}}
- tts_control            -> Ses studyosunu kontrol eder (command: "play" | "stop" | "pause" | "settings")
- generate_image         -> Metinden gorsel uretir: {"action":"generate_image","files":[],"params":{"prompt":"..."}}
- social_download        -> Video baglantisindan medya indirir (gizli sohbet yetenegi): {"action":"social_download","files":[],"params":{"url":"https://..."}}
- Control & Configure Aslan AI WhatsApp Bot (NO static templates):
  * NEVER use rigid numbered wizards (do NOT say "1/5", "2/5" etc.); converse naturally, intelligently, and directly adapt to what the user asks, including which AI model/provider, phone number, rules, delays, or questions they mention.
  * Available WhatsApp bot actions:
    - wa_bot_start_setup   -> Marks setup/pairing guidance active (uses EK BILGI live state): {"action":"wa_bot_start_setup","files":[],"params":{"phone":"+90555...","risk_accepted":true,"phone_consent":true}}
    - wa_bot_pair          -> Connects real-time WhatsApp session & generates 8-char pairing code: {"action":"wa_bot_pair","files":[],"params":{"phone":"905551234567"}} (ONLY invoke after the user was warned about temporary restriction/suspension risk + sole responsibility AND confirmed pairing their phone number! If they already explicitly consented & provided a valid phone number in the conversation, call wa_bot_pair right away.)
    - wa_bot_status        -> Queries live WhatsApp session status in real time: {"action":"wa_bot_status","files":[],"params":{}}
    - wa_bot_update        -> Updates WhatsApp bot rules, tone, groups, typing delay, or custom AI provider/model/key (set finalize_setup=true once initial customization is ready): {"action":"wa_bot_update","files":[],"params":{"enabled":true,"allow_groups":false,"allow_private":true,"group_mode":"off","trigger_keywords":["..."],"ignored_keywords":["..."],"typing_delay_seconds":2,"show_typing":true,"tone":"...","instructions":"...","default_reply":"...","rules":[{"match_type":"contains","trigger":"...","response":"..."}],"auto_reactions":[{"trigger":"...","emoji":"❤️"}],"custom_ai":{"enabled":true,"provider":"gemini","model":"gemini-4-hydrogen","api_key":"..."},"finalize_setup":true}}
    - wa_bot_wake          -> Wakes the background WhatsApp bot session immediately: {"action":"wa_bot_wake","files":[],"params":{"force":true}}
    - wa_bot_stop          -> Pauses or logs out the WhatsApp bot: {"action":"wa_bot_stop","files":[],"params":{"logout":false}}
    - wa_bot_send_message  -> Sends a direct WhatsApp message to any person, phone number, or group chat through the user's connected WhatsApp session: {"action":"wa_bot_send_message","files":[],"params":{"target":"<telefon_numarasi_kisi_adi_veya_grup_adi>","message":"<gonderilecek_mesaj>"}}
    - wa_bot_cancel_setup  -> Cancels an active setup session: {"action":"wa_bot_cancel_setup","files":[],"params":{}}
- image_audio_video      -> Gorsel + sesi videoya donusturur (files: [gorsel_id_veya_$last_image, ses_id_veya_$last_audio])
- add_text               -> Gorsel veya videoya yazi ekler (params: text, position "top"|"center"|"bottom", font_size, color)
- burn_subtitles         -> Videoya altyazi ekler (params: srt veya subtitles [{"start":0,"end":2.5,"text":"..."}])
- apply_filter           -> Gorsel/videoya filtre uygular (params: filter "grayscale"|"sepia"|"blur"|"sharpen"|"vignette"|"vintage"|"warm"|"cool"|"invert"|"hflip"|"vflip")
- merge_video_image      -> Videoya gorsel ekler (files: [video_id, gorsel_id])
- crop_image             -> Gorsel/video kirpar (params: x, y, width, height)
- trim_video             -> Video kirpar (params: start "00:00:05", duration 10)
- extract_audio          -> Videodan ses cikarir (params: format "mp3")
- combine_audio_video    -> Ses + video/gorsel birlestirir (files: [video_veya_gorsel_id, ses_id])
- overlay_audio          -> Videoya/gorsele ses ekler (files: [video_veya_gorsel_id, ses_id])
- stitch_media           -> Fotograf/video birlestirir (files: [id1, id2, ...], params: transition "fade", per_image_duration 3)
- slideshow_with_music   -> Fotograflar (+ varsa ses) -> MP4 slayt video (files: [foto1, foto2, ..., ses_id?])
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

KURALLAR:
- Video indirme arayuzde gizlidir ancak sohbet icinde TAM AKTIFTIR: kullanici "Video indirebiliyor musun?" diye sorarsa olumlu yanit ver ("Evet, video bağlantısını paylaşırsanız Aslan AI ile indirebilir ve dilerseniz ardından kırpma, ses çıkarma veya yazı ekleme gibi düzenlemeler yapabilirim."), arka plan kutuphane/arac adlarini asla anma.
- Kullanici video linki verip "bu videoyu indir" derse "social_download" kullan; sonraki mesajda "indirdigin videoyu kirp / sesini ayir / yazi ekle" derse listedeki mevcut dosya kimligini ($last_video veya yuklenen dosya id'sini) files icine yaz.
- Kullanici "bunu sesli oku", "hizi artir", "sesi Emel yap", "okumayi durdur" derse uygun "tts" veya "tts_control" blogunu ekle.
- Kullanici mevcut veya yeni WhatsApp botundan bahsettiginde, kurulum sordugunda, numara veya yapay zeka modeli belirttiginde asla kalip/sabit sablon metinler yazma; EK BILGI bolumundeki canli WhatsApp durumuna gore dogal ve akilli sohbet et ve uygun "wa_bot_*" action blogunu kullan. Asla arka plan kutuphane/protokol adi (Baileys, Vercel vb.) veya kullanicinin ham API anahtarini yanitta yazma.
- Kullanici bagli WhatsApp botu aktifken belirli bir kisiye, telefon numarasına veya gruba doğrudan mesaj göndermeni isterse ("Ahmet'e şunu yaz", "90555... numarasına şunu gönder", "Aile grubuna şunu yaz" vb.), mutlaka "wa_bot_send_message" aksiyonunu "target" ve "message" parametreleriyle kullan; aksiyonu eklemeden "gönderildi" iddiasında bulunma.
- Kullanici "iki gorsel uret, birlestirip video yap ve ses ekle" gibi cok adimli istekte bulunursa mutlaka "steps" dizisi kullan.
- files icindeki degerler kullanicinin yukledigi/urettigi dosyalarin kimlikleridir (asagida listelenir) veya adim referanslaridir ($img1, $last_image, $last_video, $last_audio).
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


def _build_openai_messages(message, history=None, available_files=None, parts=None, extra_context=''):
    """OpenAI uyumlu acik uc noktalar icin mesaj dizisi hazirlar."""
    text_chunks = []
    for p in (parts or []):
        if isinstance(p, dict) and p.get('text') and p.get('text') != message:
            text_chunks.append(str(p.get('text')))
    messages = [{'role': 'system', 'content': build_system_prompt(available_files, extra_context=extra_context)}]
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


def _ddg_fetch_vqd4(status_url: str, timeout: int = 6) -> str:
    """DuckDuckGo AI Chat icin dogrudan x-vqd-4 oturum jetonu ister (PoW/JS cozulmez)."""
    req = urllib.request.Request(
        status_url,
        headers={
            'x-vqd-accept': '1',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
            'Referer': 'https://duckduckgo.com/',
        },
        method='GET',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return (resp.headers.get('x-vqd-4') or '').strip()


def _parse_ddg_sse(raw_sse: str) -> str:
    chunks = []
    for line in str(raw_sse or '').splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        data_part = line[5:].strip()
        if not data_part or data_part == '[DONE]':
            continue
        try:
            obj = json.loads(data_part)
            if isinstance(obj, dict) and obj.get('message'):
                chunks.append(str(obj['message']))
        except Exception:
            continue
    return ''.join(chunks).strip()


def _try_duckduckgo_ai(messages: list):
    """DuckDuckGo AI Chat anonim ucretsiz uc noktasini dener (x-vqd-4 varsa)."""
    status_url = getattr(cfg, 'DDG_STATUS_URL', '')
    chat_url = getattr(cfg, 'DDG_CHAT_URL', '')
    if not status_url or not chat_url:
        return None
    try:
        vqd4 = _ddg_fetch_vqd4(status_url, timeout=6)
        if not vqd4:
            return None
        convo = []
        sys_txt = ''
        for m in messages:
            if m.get('role') == 'system':
                sys_txt = str(m.get('content') or '')[:1600]
            else:
                role = 'assistant' if m.get('role') == 'assistant' else 'user'
                content = str(m.get('content') or '')
                if sys_txt and role == 'user' and not convo:
                    content = sys_txt + '\n\nKullanici Mesaji:\n' + content
                    sys_txt = ''
                convo.append({'role': role, 'content': content})
        raw = _post_raw_text(
            chat_url,
            {'model': getattr(cfg, 'DDG_TEXT_MODEL', 'gpt-4o-mini'), 'messages': convo},
            timeout=15,
            headers={
                'x-vqd-4': vqd4,
                'Accept': 'text/event-stream',
                'Origin': 'https://duckduckgo.com',
                'Referer': 'https://duckduckgo.com/',
            },
        )
        txt = _parse_ddg_sse(raw)
        if not txt and raw.strip().startswith('{'):
            try:
                parsed = json.loads(raw)
                txt = str(parsed.get('message') or '').strip()
            except Exception:
                pass
        if _is_valid_free_reply(txt):
            return txt
    except Exception:
        return None
    return None


def _try_deepinfra_open(messages: list):
    """DeepInfra acik OpenAI-uyumlu uc noktasini dener."""
    url = getattr(cfg, 'DEEPINFRA_CHAT_URL', '')
    if not url:
        return None
    headers = {'X-Deepinfra-Source': 'web-page'}
    di_key = os.environ.get('DEEPINFRA_API_KEY', '').strip()
    if di_key:
        headers['Authorization'] = 'Bearer ' + di_key
    try:
        data = _post_json(
            url,
            {
                'model': getattr(cfg, 'DEEPINFRA_TEXT_MODEL', 'meta-llama/Llama-3.3-70B-Instruct'),
                'messages': messages,
                'temperature': 0.7,
            },
            timeout=10,
            headers=headers,
        )
        choices = data.get('choices') or []
        content = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
        if _is_valid_free_reply(content):
            return content
    except Exception:
        return None
    return None


def _try_huggingface_open(messages: list):
    """Hugging Face ucretsiz acik router uc noktasini dener."""
    url = getattr(cfg, 'HF_CHAT_URL', '')
    if not url:
        return None
    headers = {}
    hf_tok = os.environ.get('HF_TOKEN', '').strip() or os.environ.get('HUGGINGFACEHUB_API_TOKEN', '').strip()
    if hf_tok:
        headers['Authorization'] = 'Bearer ' + hf_tok
    try:
        data = _post_json(
            url,
            {
                'model': getattr(cfg, 'HF_TEXT_MODEL', 'Qwen/Qwen2.5-72B-Instruct'),
                'messages': messages,
                'temperature': 0.7,
            },
            timeout=10,
            headers=headers or None,
        )
        choices = data.get('choices') or []
        content = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
        if _is_valid_free_reply(content):
            return content
    except Exception:
        return None
    return None


def _try_free_endpoints(message, history=None, available_files=None, parts=None, extra_context=''):
    """
    Hesap/API anahtari gerektirmeyen ucretsiz acik AI uc noktalarini sirayla dener:
      1) Pollinations AI (openai-fast OpenAI uc noktasi + GET yedegi)
      2) DuckDuckGo AI Chat (anonim ucretsiz uc nokta)
      3) DeepInfra & Hugging Face ucretsiz acik uc noktalari (+ FREE_AI_ENDPOINTS)

    Returns: (yanit_metni, hata_mesaji)
    """
    messages = _build_openai_messages(
        message,
        history=history,
        available_files=available_files,
        parts=parts,
        extra_context=extra_context,
    )
    if not messages:
        return None, 'Mesaj bos'

    last_err = ''

    # 1) Pollinations OpenAI-uyumlu acik uc noktasi (openai-fast oncelikli)
    for free_model in getattr(cfg, 'POLLINATIONS_TEXT_MODELS', ('openai-fast', 'openai', 'mistral')):
        try:
            data = _post_json(
                cfg.POLLINATIONS_TEXT_OPENAI,
                {'model': free_model, 'messages': messages, 'temperature': 0.7},
                timeout=16,
            )
            choices = data.get('choices') or []
            content = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
            if _is_valid_free_reply(content):
                return content, None
        except Exception as e:
            last_err = api_utils.safe_error_message(e)

    # 1b) Pollinations GET acik uc noktasi (kisa metin yedegi)
    try:
        user_msg = messages[-1]['content'][:1000]
        sys_short = (
            'Sen Aslan AI sin. Kendini yalnizca Aslan AI olarak tanit; '
            'arka plandaki hicbir model, API veya kutuphane adini soyleme. '
            'Turkce, kisa, samimi ve yardimsever yanit ver.'
        )
        qs = urllib.parse.urlencode({'model': 'openai-fast', 'system': sys_short})
        url = '%s/%s?%s' % (cfg.POLLINATIONS_TEXT_GET, urllib.parse.quote(user_msg, safe=''), qs)
        text = _get_raw_text(url, timeout=14).strip()
        if _is_valid_free_reply(text):
            return text, None
    except Exception as e:
        last_err = api_utils.safe_error_message(e)

    # 2) DuckDuckGo AI Chat ucretsiz anonim uc noktasi
    ddg_reply = _try_duckduckgo_ai(messages)
    if ddg_reply:
        return ddg_reply, None

    # 3) DeepInfra & Hugging Face acik uc noktalari
    di_reply = _try_deepinfra_open(messages)
    if di_reply:
        return di_reply, None

    hf_reply = _try_huggingface_open(messages)
    if hf_reply:
        return hf_reply, None

    # 3b) Opsiyonel ek anahtarsiz acik uc noktalar (FREE_AI_ENDPOINTS)
    extra_urls = [u.strip() for u in os.environ.get('FREE_AI_ENDPOINTS', '').split(',') if u.strip()]
    for url in extra_urls:
        try:
            data = _post_json(url, {'model': 'gpt-4o-mini', 'messages': messages}, timeout=12)
            choices = data.get('choices') or []
            content = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
            if _is_valid_free_reply(content):
                return content, None
        except Exception as e:
            last_err = api_utils.safe_error_message(e)

    return None, last_err or 'Aslan AI su an yanit veremedi. Lutfen tekrar deneyin.'


def _try_gemini_reply(message, parts=None, available_files=None, api_key=None, history=None, extra_context=''):
    """Kullanicinin mevcut Gemini API yapilandirmasiyla yanit uretir (son durak gemini-4-hydrogen + multimodal)."""
    key = api_key or get_api_key()
    if not key:
        return None, 'Aslan AI servisi su an kullanilamiyor.'
    contents = _build_contents(message, parts=parts, history=history)
    if not contents:
        return None, 'Mesaj bos'

    payload = {
        'contents': contents,
        'systemInstruction': {'parts': [{'text': build_system_prompt(available_files, extra_context=extra_context)}]},
        'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 2048},
    }
    headers = {'x-goog-api-key': key}
    models = [GEMINI_TEXT_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_TEXT_MODEL]
    last_error = 'Aslan AI su an yanit vermedi.'
    for model in models:
        url = '%s/%s:generateContent' % (GEMINI_ENDPOINT, model)
        try:
            data = _post_json(url, payload, timeout=45, headers=headers)
        except urllib.error.HTTPError:
            last_error = 'Aslan AI gecici olarak yanit veremedi. Lutfen tekrar deneyin.'
            continue
        except Exception:
            last_error = 'Aslan AI baglanti hatasi. Lutfen tekrar deneyin.'
            continue
        reply = _extract_text(data)
        if reply:
            return reply, None
        last_error = 'Aslan AI su an yanit vermedi.'
    return None, last_error


def _try_user_custom_ai(user_ai_cfg, message, parts=None, available_files=None, history=None, extra_context=''):
    """Kullanicinin Ayarlar menusunden sectigi saglayici / model / ozel HTTPS uc noktayla yanit uretir."""
    if not isinstance(user_ai_cfg, dict):
        return None, 'Özel AI yapılandırması geçersiz.'

    provider = str(user_ai_cfg.get('provider') or '').strip().lower()
    model = str(user_ai_cfg.get('model') or '').strip()
    endpoint = str(user_ai_cfg.get('endpoint') or '').strip()
    api_key = str(user_ai_cfg.get('api_key') or '').strip()

    if provider == 'gemini':
        if not api_key:
            return None, 'Google Gemini için API anahtarı bulunamadı.'
        contents = _build_contents(message, parts=parts, history=history)
        if not contents:
            return None, 'Mesaj bos'
        base_ep = endpoint or GEMINI_ENDPOINT
        if endpoint and ':generatecontent' in endpoint.lower():
            url = endpoint
        else:
            m_name = model or GEMINI_TEXT_MODEL
            url = '%s/%s:generateContent' % (base_ep.rstrip('/'), urllib.parse.quote(m_name, safe=''))
        payload = {
            'contents': contents,
            'systemInstruction': {'parts': [{'text': build_system_prompt(available_files, extra_context=extra_context)}]},
            'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 2048},
        }
        try:
            data = _post_json(url, payload, timeout=45, headers={'x-goog-api-key': api_key})
            reply = _extract_text(data)
            if reply:
                return reply, None
            return None, 'Seçili model yanıt döndürmedi.'
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return None, 'Google Gemini API anahtarınız reddedildi veya yetkisiz.'
            return None, 'Seçili yapay zeka servisi geçici olarak yanıt vermedi.'
        except Exception as e:
            return None, api_utils.safe_error_message(e, 'Seçili yapay zeka servisine bağlanılamadı.')

    messages = _build_openai_messages(
        message,
        history=history,
        available_files=available_files,
        parts=parts,
        extra_context=extra_context,
    )
    if not messages:
        return None, 'Mesaj bos'

    if provider == 'anthropic':
        if not api_key:
            return None, 'Anthropic için API anahtarı bulunamadı.'
        url = endpoint or 'https://api.anthropic.com/v1/messages'
        sys_txt = ''
        convo = []
        for m in messages:
            if m.get('role') == 'system':
                sys_txt = str(m.get('content') or '')
            else:
                convo.append({'role': 'assistant' if m.get('role') == 'assistant' else 'user', 'content': m.get('content')})
        try:
            data = _post_json(
                url,
                {
                    'model': model or 'claude-3-5-haiku-latest',
                    'max_tokens': 2048,
                    'system': sys_txt,
                    'messages': convo,
                },
                timeout=45,
                headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
            )
            blocks = data.get('content') or []
            txt = ''.join(b.get('text', '') for b in blocks if isinstance(b, dict)).strip()
            return (txt, None) if txt else (None, 'Seçili model yanıt döndürmedi.')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return None, 'Girilen API anahtarı reddedildi veya yetkisiz.'
            return None, 'Seçili yapay zeka servisi geçici olarak yanıt vermedi.'
        except Exception as e:
            return None, api_utils.safe_error_message(e, 'Seçili yapay zeka servisine bağlanılamadı.')

    preset_urls = {
        'openai': ('https://api.openai.com/v1/chat/completions', 'gpt-4o-mini'),
        'openrouter': ('https://openrouter.ai/api/v1/chat/completions', 'openai/gpt-4o-mini'),
        'groq': (cfg.GROQ_ENDPOINT, cfg.GROQ_TEXT_MODEL),
        'deepseek': ('https://api.deepseek.com/v1/chat/completions', 'deepseek-chat'),
        'mistral': ('https://api.mistral.ai/v1/chat/completions', 'mistral-small-latest'),
    }
    def_url, def_model = preset_urls.get(provider, ('', ''))
    target_url = endpoint or def_url
    target_model = model or def_model

    ok_ep, clean_ep_or_err = api_utils.validate_custom_ai_endpoint(target_url)
    if not ok_ep:
        return None, clean_ep_or_err
    target_url = clean_ep_or_err

    if provider == 'custom':
        low_url = target_url.lower()
        if ':generatecontent' in low_url:
            contents = _build_contents(message, parts=parts, history=history)
            hdrs = {'x-goog-api-key': api_key} if api_key else {}
            try:
                data = _post_json(
                    target_url,
                    {
                        'contents': contents,
                        'systemInstruction': {'parts': [{'text': build_system_prompt(available_files, extra_context=extra_context)}]},
                        'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 2048},
                    },
                    timeout=45,
                    headers=hdrs,
                )
                txt = _extract_text(data)
                return (txt, None) if txt else (None, 'Özel uç nokta yanıt döndürmedi.')
            except Exception as e:
                return None, api_utils.safe_error_message(e, 'Özel uç noktaya bağlanılamadı.')
        if not any(s in low_url for s in ('/chat/completions', '/completions', '/messages', '/responses', '/generate')):
            target_url = target_url.rstrip('/') + '/chat/completions'

    if not api_key and provider != 'custom':
        return None, 'Seçili sağlayıcı için API anahtarı bulunamadı.'

    hdrs = {}
    if api_key:
        hdrs['Authorization'] = 'Bearer ' + api_key
    try:
        data = _post_json(
            target_url,
            {
                'model': target_model or 'gpt-4o-mini',
                'messages': messages,
                'temperature': 0.7,
                'max_tokens': 2048,
            },
            timeout=45,
            headers=hdrs or None,
        )
        choices = data.get('choices') or []
        txt = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
        if not txt and isinstance(data.get('reply'), str):
            txt = data['reply'].strip()
        if not txt and isinstance(data.get('output_text'), str):
            txt = data['output_text'].strip()
        if txt:
            return txt, None
        return None, 'Özel AI uç noktası boş yanıt döndürdü.'
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, 'Özel AI API anahtarı reddedildi veya yetkisiz.'
        return None, 'Özel AI uç noktası yanıt veremedi (HTTP %d).' % e.code
    except Exception as e:
        return None, api_utils.safe_error_message(e, 'Özel AI uç noktasına bağlanılamadı.')


def generate_reply(message, parts=None, available_files=None, api_key=None, history=None, extra_context='', user_ai_config=None):
    """
    Kullanici Ayarlar menusunden ozel bir saglayici/uc nokta secmisse once onu kullanir.
    Varsayilan modda ise metin isteklerinde once ucretsiz/anahtarsiz acik uclari dener
    (Pollinations -> DDG -> DeepInfra/HF); hepsi hata verirse son durak olarak Gemini API
    (gemini-4-hydrogen) zincirine gecer.
    Multimodal (gorsel/ses/video/PDF) isteklerde varsayilan modda Gemini API kullanir.

    Returns: (yanit_metni, hata_mesaji)
    """
    contents = _build_contents(message, parts=parts, history=history)
    if not contents:
        return None, 'Mesaj bos'

    if (
        isinstance(user_ai_config, dict)
        and user_ai_config.get('apply_to_chat', True)
        and str(user_ai_config.get('provider') or 'default').strip().lower() not in ('', 'default')
    ):
        return _try_user_custom_ai(
            user_ai_config,
            message,
            parts=parts,
            available_files=available_files,
            history=history,
            extra_context=extra_context,
        )

    has_binary = any(
        isinstance(p, dict) and ('inline_data' in p or 'inlineData' in p)
        for p in (parts or [])
    )

    free_err = ''
    if not has_binary:
        reply, free_err = _try_free_endpoints(
            message,
            history=history,
            available_files=available_files,
            parts=parts,
            extra_context=extra_context,
        )
        if reply:
            return reply, None

    gem_reply, gem_err = _try_gemini_reply(
        message,
        parts=parts,
        available_files=available_files,
        api_key=api_key,
        history=history,
        extra_context=extra_context,
    )
    if gem_reply:
        return gem_reply, None

    return None, gem_err or free_err or 'Yanit alinamadi'


def generate_reply_lite(message, parts=None, available_files=None, api_key=None, history=None, extra_context='', user_ai_config=None):
    """Hafif modelle yanit uretir."""
    return generate_reply(
        message,
        parts=parts,
        available_files=available_files,
        api_key=api_key,
        history=history,
        extra_context=extra_context,
        user_ai_config=user_ai_config,
    )


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


# ===================================================================== ses -> metin (STT)
def transcribe_audio(audio_bytes: bytes, mime_type: str = 'audio/webm', lang: str = 'tr-TR'):
    """
    Ses kaydini once ucretsiz uc nokta ile, gerekirse son durak Gemini API ile metne cevirir.

    Returns: (metin, hata_mesaji)
    """
    if not audio_bytes:
        return None, 'Ses verisi bos'
    b64 = base64.b64encode(audio_bytes).decode('ascii')
    ext_fmt = 'mp3'
    low_mime = str(mime_type or '').lower()
    if 'wav' in low_mime:
        ext_fmt = 'wav'
    elif 'ogg' in low_mime or 'opus' in low_mime:
        ext_fmt = 'ogg'
    elif 'webm' in low_mime:
        ext_fmt = 'webm'
    elif 'm4a' in low_mime or 'mp4' in low_mime:
        ext_fmt = 'mp4'

    instruction = (
        'Bu ses kaydindaki konusmayi (%s) yalnizca birebir duz metin olarak yaz. '
        'Yorum, baslik veya ek aciklama ekleme.' % (lang or 'tr-TR')
    )
    last_err = ''

    try:
        data = _post_json(
            cfg.POLLINATIONS_TEXT_OPENAI,
            {
                'model': 'openai-audio',
                'messages': [{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': instruction},
                        {'type': 'input_audio', 'input_audio': {'data': b64, 'format': ext_fmt}},
                    ],
                }],
            },
            timeout=25,
        )
        choices = data.get('choices') or []
        txt = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
        if _is_valid_free_reply(txt):
            return txt, None
    except Exception as e:
        last_err = api_utils.safe_error_message(e)

    key = get_api_key()
    if key:
        headers = {'x-goog-api-key': key}
        payload = {
            'contents': [{
                'role': 'user',
                'parts': [
                    {'text': instruction},
                    {'inline_data': {'mime_type': mime_type or 'audio/webm', 'data': b64}},
                ],
            }],
            'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 1024},
        }
        for model in [GEMINI_TEXT_MODEL] + list(GEMINI_FALLBACK_MODELS):
            try:
                url = '%s/%s:generateContent' % (GEMINI_ENDPOINT, model)
                data = _post_json(url, payload, timeout=45, headers=headers)
                reply = _extract_text(data)
                if reply:
                    return reply.strip(), None
            except Exception as e:
                last_err = api_utils.safe_error_message(e)

    return None, 'Aslan AI ses kaydini su an metne donusturemedi. Lutfen tekrar deneyin.'


# ===================================================================== gorsel uretimi
def _try_free_image_endpoints(prompt: str):
    """Opsiyonel ucretsiz gorsel uretim uc noktalarini (FREE_IMAGE_ENDPOINTS) dener."""
    extra = [u.strip() for u in os.environ.get('FREE_IMAGE_ENDPOINTS', '').split(',') if u.strip()]
    for url in extra:
        try:
            data = json.dumps({'prompt': prompt}).encode('utf-8')
            req = urllib.request.Request(
                url,
                data=data,
                headers={'Content-Type': 'application/json', 'User-Agent': 'ASLAN-Media/1.0'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                ctype = (resp.headers.get('Content-Type') or '').lower()
                raw = resp.read()
                if ctype.startswith('image/') and len(raw) >= 512:
                    return base64.b64encode(raw).decode('ascii'), ctype
                parsed = json.loads(raw.decode('utf-8', errors='replace'))
                if isinstance(parsed, dict):
                    b64 = parsed.get('image_base64') or parsed.get('b64_json') or ''
                    if not b64 and isinstance(parsed.get('data_url'), str) and ',' in parsed['data_url']:
                        b64 = parsed['data_url'].split(',', 1)[1]
                    if b64:
                        return b64, parsed.get('mime_type') or 'image/png'
        except Exception:
            continue
    return None, None


def generate_image(prompt, device_id=None, api_key=None, username_lower=None):
    """
    Metinden gorsel uretir ve aylik kati 2 gorsel kotasi uygular.
    Oncelik: 1) Ucretsiz Pollinations & acik uclar -> 2) Son durak Gemini/Imagen.
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
            'error': ('Bu ayki %d gorsel uretim hakkiniz doldu. Yeni ay basladiginda haklariniz yenilenir.'
                      % status.get('monthly_limit', cfg.IMAGE_MONTHLY_LIMIT)),
            'quota': status,
        }

    # 1) Oncelikli: Ucretsiz/anahtarsiz Pollinations ve acik gorsel servisleri
    image_b64 = None
    mime = 'image/png'
    last_error = None
    engine = None

    image_b64, free_mime, free_engine = _pollinations_image(prompt)
    if image_b64:
        mime = free_mime or mime
        engine = free_engine or 'pollinations'

    if not image_b64:
        extra_b64, extra_mime = _try_free_image_endpoints(prompt)
        if extra_b64:
            image_b64 = extra_b64
            mime = extra_mime or mime
            engine = 'free-image'

    # 2) Son Durak Yedek: Gemini / Imagen (yalnizca ucretsiz servisler basarisiz olursa)
    key = api_key or get_api_key()
    headers = {'x-goog-api-key': key} if key else None
    if not image_b64 and key:
        for model in IMAGE_MODEL_CHAIN:
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
        cur_status = (database.check_user_image_quota(username_lower)[1]
                      if username_lower else quota.image_status(device_id or 'anonymous'))
        return False, {
            'success': False,
            'quota_exceeded': False,
            'error': 'Aslan AI gorsel uretimi su an tamamlanamadi. Lutfen tekrar deneyin.',
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
        'engine': engine or 'pollinations',
    }


def _pollinations_image(prompt, width=1024, height=1024, timeout=45):
    """Anahtarsiz Pollinations gorsel uretimi (coklu model failover)."""
    from urllib.parse import quote
    models = getattr(cfg, 'POLLINATIONS_IMAGE_MODELS', ('flux', 'turbo', ''))
    seed = str(abs(hash(prompt)) % 100000)
    encoded = quote(prompt, safe='')
    for idx, model_name in enumerate(models):
        try:
            url = '%s/%s?width=%d&height=%d&nologo=true&seed=%s' % (
                POLLINATIONS_ENDPOINT, encoded, width, height, seed)
            if model_name:
                url += '&model=' + quote(model_name, safe='')
            req = urllib.request.Request(url, headers={'User-Agent': 'ASLAN-Media/1.0'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                blob = resp.read()
                ctype = (resp.headers.get('Content-Type') or 'image/jpeg').split(';', 1)[0].strip()
            if blob and len(blob) >= 512 and ctype.startswith('image/'):
                tag = ('pollinations/' + model_name) if model_name else 'pollinations'
                return base64.b64encode(blob).decode('ascii'), ctype, tag
        except Exception:
            if idx + 1 >= len(models):
                break
            continue
    return None, None, None


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
    """Metni sese donusturur (edge-tts -> HTTPS Google TTS -> gTTS). (ses_verisi_bytes, motor_adi) dondurur."""
    text = str(text or '').strip()
    if not text:
        return None, None
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]
    voice = normalize_tts_voice(voice)
    rate = normalize_tts_rate(rate)
    pitch = normalize_tts_pitch(pitch)
    lang = 'en' if voice.startswith('en-') else 'tr'

    data, engine_name = _try_edge_tts(text, voice, rate=rate, pitch=pitch)
    if data:
        return data, engine_name

    data = _try_google_https_tts(text, lang=lang)
    if data:
        return data, 'https-tts'

    data = _try_gtts(text, lang=lang)
    if data:
        return data, 'gTTS'
    return None, None


async def _edge_tts_generate(text, voice, rate='+0%', pitch='+0Hz'):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    chunks = bytearray()
    async for item in communicate.stream():
        if isinstance(item, dict) and item.get('type') == 'audio' and item.get('data'):
            chunks.extend(item['data'])
    return bytes(chunks)


def _try_edge_tts(text, voice, rate='+0%', pitch='+0Hz'):
    for _ in range(2):
        try:
            audio = asyncio.run(asyncio.wait_for(
                _edge_tts_generate(text, voice, rate=rate, pitch=pitch),
                timeout=10.0,
            ))
            if audio and len(audio) >= 128:
                return audio, 'edge-tts'
        except Exception as e:
            err = str(e)
            if 'NoAudioReceived' in err or '403' in err:
                continue
            break
    return None, None


def _split_tts_chunks(text: str, max_len: int = 180) -> list:
    clean = ' '.join(str(text or '').split()).strip()
    if not clean:
        return []
    parts = []
    sentences = re.split(r'(?<=[.!?;:])\s+', clean)
    cur = ''
    for seg in sentences:
        seg = seg.strip()
        if not seg:
            continue
        if len(seg) <= max_len:
            if cur and (len(cur) + 1 + len(seg)) <= max_len:
                cur = cur + ' ' + seg
            else:
                if cur:
                    parts.append(cur)
                cur = seg
        else:
            if cur:
                parts.append(cur)
                cur = ''
            words = seg.split(' ')
            for w in words:
                if len(w) > max_len:
                    for k in range(0, len(w), max_len):
                        parts.append(w[k:k + max_len])
                    continue
                if cur and (len(cur) + 1 + len(w)) <= max_len:
                    cur = cur + ' ' + w
                else:
                    if cur:
                        parts.append(cur)
                    cur = w
    if cur:
        parts.append(cur)
    return parts[:18]


def _is_valid_mp3_bytes(blob: bytes) -> bool:
    if not blob or len(blob) < 128:
        return False
    low_head = blob[:64].lstrip().lower()
    if low_head.startswith(b'<!doctype') or low_head.startswith(b'<html'):
        return False
    return True


def _fetch_tts_chunk_https(chunk: str, lang: str = 'tr') -> bytes:
    qs_gtx = urllib.parse.urlencode({'client': 'gtx', 'ie': 'UTF-8', 'tl': lang, 'q': chunk})
    qs_tw = urllib.parse.urlencode({'client': 'tw-ob', 'ie': 'UTF-8', 'tl': lang, 'q': chunk})
    urls = (
        'https://translate.googleapis.com/translate_tts?' + qs_gtx,
        'https://translate.google.com/translate_tts?' + qs_tw,
    )
    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
                    'Referer': 'https://translate.google.com/',
                },
                method='GET',
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
                if _is_valid_mp3_bytes(data):
                    return data
        except Exception:
            continue
    return b''


def _try_google_https_tts(text: str, lang: str = 'tr') -> bytes:
    """Guvenilir HTTPS MP3 seslendirme yedegi (parcali akis)."""
    chunks = _split_tts_chunks(text, max_len=180)
    if not chunks:
        return b''
    out = bytearray()
    for ch in chunks:
        part = _fetch_tts_chunk_https(ch, lang=lang)
        if not part:
            return b''
        out.extend(part)
    return bytes(out) if len(out) >= 128 else b''


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
    """Yanit icindeki aslan_action JSON blogunu (tek eylem veya cok adimli steps) ayristirir."""
    if not text or 'aslan_action' not in text:
        return None
    match = re.search(r'```\s*aslan_action\s*(.*?)```', text, re.DOTALL)
    payload = match.group(1) if match else None
    if not payload:
        brace = re.search(r'\{.*"action"\s*:.*\}|\{.*"steps"\s*:.*\}', text, re.DOTALL)
        payload = brace.group(0) if brace else None
    if not payload:
        return None
    try:
        parsed = json.loads(payload.strip())
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list):
        steps = [s for s in parsed if isinstance(s, dict) and s.get('action')]
        return {'steps': steps} if steps else None
    if isinstance(parsed, dict):
        if isinstance(parsed.get('steps'), list):
            steps = [s for s in parsed['steps'] if isinstance(s, dict) and s.get('action')]
            return {'steps': steps} if steps else None
        if isinstance(parsed.get('actions'), list):
            steps = [s for s in parsed['actions'] if isinstance(s, dict) and s.get('action')]
            return {'steps': steps} if steps else None
        if parsed.get('action'):
            return parsed
    return None


def strip_action(text):
    """Yanittan aslan_action blogunu temizler."""
    cleaned = re.sub(r'```\s*aslan_action\s*.*?```', '', text or '', flags=re.DOTALL)
    cleaned = re.sub(r'```\s*aslan_action\s*\{.*$', '', cleaned, flags=re.DOTALL)
    return cleaned.strip()


def resolve_file_paths(file_ids, uploaded_files=None):
    """Dosya kimliklerini guvenli sekilde tam yola cevirir (geriye donuk uyumluluk)."""
    return _resolve_step_files(file_ids, uploaded_files=uploaded_files)


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


def _classify_path_kind(path: str, mime_hint: str = '') -> str:
    m = str(mime_hint or '').lower()
    ext = os.path.splitext(str(path or ''))[1].lower()
    if m.startswith('image/') or ext in media_engine.IMAGE_EXTS:
        return 'image'
    if m.startswith('audio/') or ext in media_engine.AUDIO_EXTS:
        return 'audio'
    if m.startswith('video/') or ext in media_engine.VIDEO_EXTS:
        return 'video'
    return 'other'


def _normalize_pipeline_steps(action_spec) -> list:
    if isinstance(action_spec, list):
        raw = action_spec
    elif isinstance(action_spec, dict):
        if isinstance(action_spec.get('steps'), list):
            raw = action_spec['steps']
        elif isinstance(action_spec.get('actions'), list):
            raw = action_spec['actions']
        elif action_spec.get('action'):
            raw = [action_spec]
        else:
            raw = []
    else:
        raw = []
    max_steps = getattr(cfg, 'MAX_AI_PIPELINE_STEPS', 5)
    return [s for s in raw if isinstance(s, dict) and s.get('action')][:max_steps]


def _resolve_step_files(file_ids, uploaded_files=None, step_paths=None):
    """Hem yuklenen dosya ID'lerini hem de $id/$last_image/$last_video/$last_audio referanslarini cozer."""
    step_paths = step_paths or {}
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
        key = str(fid).strip()
        if not key:
            continue
        if key.startswith('$'):
            ref_name = key[1:]
            ref_path = step_paths.get(ref_name)
            if ref_path and os.path.exists(ref_path):
                paths.append(ref_path)
            continue
        path = mapping.get(key)
        if not path:
            name = os.path.basename(key)
            candidate = os.path.join(MEDIA_TEMP_DIR, name)
            path = candidate if (os.path.exists(candidate) and os.path.isfile(candidate)) else None
        if path:
            paths.append(path)
    return paths


def _auto_select_media_files(op_name: str, step_paths: dict, uploaded_files=None) -> list:
    """Eger model files alanini bos biraktiysa mevcut oturum/adim medyalarindan otomatik secer."""
    all_imgs, all_vids, all_auds = [], [], []
    for meta in (uploaded_files or []):
        if not isinstance(meta, dict):
            continue
        p = meta.get('path')
        if not p or not os.path.exists(p):
            continue
        k = _classify_path_kind(p, meta.get('mime_type') or meta.get('type') or '')
        if k == 'image':
            all_imgs.append(p)
        elif k == 'video':
            all_vids.append(p)
        elif k == 'audio':
            all_auds.append(p)

    for k_ref, p in (step_paths or {}).items():
        if not p or not os.path.exists(p):
            continue
        if k_ref in ('prev', 'last', 'last_image', 'last_video', 'last_audio'):
            continue
        kind = _classify_path_kind(p)
        if kind == 'image' and p not in all_imgs:
            all_imgs.append(p)
        elif kind == 'video' and p not in all_vids:
            all_vids.append(p)
        elif kind == 'audio' and p not in all_auds:
            all_auds.append(p)

    if op_name in ('image_audio_video', 'combine_audio_video', 'overlay_audio'):
        primary = step_paths.get('last_video') or step_paths.get('last_image') or (all_vids[-1] if all_vids else (all_imgs[-1] if all_imgs else ''))
        aud = step_paths.get('last_audio') or (all_auds[-1] if all_auds else '')
        return [x for x in (primary, aud) if x]

    if op_name in ('slideshow_with_music', 'stitch_media'):
        out = list(all_imgs[-cfg.MAX_SLIDESHOW_PHOTOS:])
        if op_name == 'slideshow_with_music':
            aud = step_paths.get('last_audio') or (all_auds[-1] if all_auds else '')
            if aud:
                out.append(aud)
        elif len(out) < 2 and len(all_vids) >= 2:
            return all_vids
        return out

    if op_name == 'merge_video_image':
        v = step_paths.get('last_video') or (all_vids[-1] if all_vids else '')
        im = step_paths.get('last_image') or (all_imgs[-1] if all_imgs else '')
        return [x for x in (v, im) if x]

    if op_name == 'video_concat':
        return all_vids

    if op_name == 'merge_audio':
        return all_auds

    if op_name in ('add_text', 'apply_filter', 'crop_image', 'resize', 'convert'):
        cand = step_paths.get('prev') or step_paths.get('last_video') or step_paths.get('last_image')
        if not cand:
            cand = all_vids[-1] if all_vids else (all_imgs[-1] if all_imgs else '')
        return [cand] if cand else []

    if op_name in ('denoise_audio', 'compress_audio'):
        cand = step_paths.get('last_audio') or (all_auds[-1] if all_auds else '')
        return [cand] if cand else []

    cand = step_paths.get('last_video') or (all_vids[-1] if all_vids else (step_paths.get('prev') or ''))
    return [cand] if cand else []


def _write_generated_image_temp(b64_str: str, mime: str) -> str:
    import uuid
    ext = '.jpg' if 'jpeg' in str(mime).lower() or 'jpg' in str(mime).lower() else '.png'
    out_path = os.path.join(cfg.MEDIA_TEMP_DIR, 'ai_img_%s%s' % (uuid.uuid4().hex, ext))
    with open(out_path, 'wb') as fh:
        fh.write(base64.b64decode(b64_str))
    return out_path


def run_action_pipeline(action_spec, uploaded_files=None, device_id=None, username_lower=None):
    """
    Tek eylemi veya cok adimli AI -> FFmpeg/yt-dlp/TTS zincirini calistirir.

    Returns: (final_result_dict, generated_images_list, created_temp_paths_list)
    """
    from . import quota

    steps = _normalize_pipeline_steps(action_spec)
    if not steps:
        return {'success': False, 'operation': 'pipeline', 'error': 'Gecerli eylem adimi bulunamadi'}, [], []

    # Cok adimli gorsel uretimi icin calistirmadan once toplam aylik kotayi dogrula
    img_steps_count = sum(1 for s in steps if str(s.get('action', '')).strip() == 'generate_image')
    if img_steps_count > 0:
        if username_lower:
            allowed, status = database.check_user_image_quota(username_lower, amount=img_steps_count)
        else:
            allowed, status = quota.check_image(device_id or 'anonymous', amount=img_steps_count)
        if not allowed:
            return {
                'success': False,
                'operation': 'generate_image' if img_steps_count == 1 else 'pipeline',
                'quota_exceeded': True,
                'error': ('Bu islem icin %d gorsel hakki gerekiyor; bu ay kalan hakkiniz: %d/%d.'
                          % (img_steps_count, status.get('monthly_remaining', 0),
                             status.get('monthly_limit', cfg.IMAGE_MONTHLY_LIMIT))),
                'quota': status,
            }, [], []

    step_paths = {}
    for meta in (uploaded_files or []):
        if not isinstance(meta, dict):
            continue
        p = meta.get('path')
        if not p or not os.path.exists(p):
            continue
        kind = _classify_path_kind(p, meta.get('mime_type') or meta.get('type') or '')
        if kind == 'image':
            step_paths['last_image'] = p
        elif kind == 'video':
            step_paths['last_video'] = p
        elif kind == 'audio':
            step_paths['last_audio'] = p
        step_paths['last'] = p

    created_paths = []
    generated_images = []
    last_result = None

    for idx, step in enumerate(steps):
        name = str(step.get('action', '')).strip()
        step_id = str(step.get('id') or ('step_%d' % (idx + 1))).strip()
        params = step.get('params') if isinstance(step.get('params'), dict) else {}
        file_ids = step.get('files') if isinstance(step.get('files'), list) else []

        if name == 'tts':
            res = run_tts_action(params)
            last_result = res
            if res.get('success') and res.get('output_file'):
                p = os.path.join(cfg.MEDIA_TEMP_DIR, os.path.basename(res['output_file']))
                if os.path.exists(p):
                    created_paths.append(p)
                    step_paths[step_id] = p
                    step_paths['prev'] = p
                    step_paths['last'] = p
                    step_paths['last_audio'] = p
            if not res.get('success'):
                return res, generated_images, created_paths
            continue

        if name == 'tts_control':
            res = run_tts_control_action(params)
            last_result = res
            if not res.get('success'):
                return res, generated_images, created_paths
            continue

        if name == 'wa_bot_start_setup':
            from . import wa_bot
            res = wa_bot.action_start_setup(username_lower, params)
            last_result = res
            continue

        if name == 'wa_bot_pair':
            from . import wa_bot
            res = wa_bot.action_pair(username_lower, params)
            last_result = res
            continue

        if name == 'wa_bot_status':
            from . import wa_bot
            res = wa_bot.action_status(username_lower)
            last_result = res
            continue

        if name == 'wa_bot_update':
            from . import wa_bot
            res = wa_bot.apply_structured_update(username_lower, params)
            last_result = res
            continue

        if name == 'wa_bot_wake':
            from . import wa_bot
            res = wa_bot.action_wake(username_lower)
            last_result = res
            continue

        if name == 'wa_bot_stop':
            from . import wa_bot
            res = wa_bot.action_stop(username_lower, params)
            last_result = res
            continue

        if name == 'wa_bot_send_message':
            from . import wa_bot
            res = wa_bot.action_send_message(username_lower, params)
            last_result = res
            continue

        if name == 'wa_bot_cancel_setup':
            from . import wa_bot
            res = wa_bot.action_cancel_setup(username_lower)
            last_result = res
            continue

        if name == 'generate_image':
            prompt = str(params.get('prompt', '')).strip()
            ok, result = generate_image(
                prompt, device_id=device_id or 'anonymous', username_lower=username_lower)
            if not ok:
                err_res = {
                    'success': False,
                    'operation': 'generate_image',
                    'error': result.get('error', 'Gorsel uretilemedi'),
                    'quota': result.get('quota', {}),
                }
                return err_res, generated_images, created_paths
            b64 = result['image_base64']
            mime = result['mime_type']
            img_desc = {
                'success': True,
                'operation': 'generate_image',
                'label': 'Görsel üretimi',
                'image_base64': b64,
                'mime_type': mime,
                'data_url': 'data:%s;base64,%s' % (mime, b64),
                'output_file': 'aslan-gorsel-%d.png' % (len(generated_images) + 1),
                'prompt': result.get('prompt', ''),
                'engine': 'Aslan AI',
                'quota': result.get('quota', {}),
            }
            generated_images.append(img_desc)
            last_result = img_desc
            try:
                tmp_img = _write_generated_image_temp(b64, mime)
                created_paths.append(tmp_img)
                step_paths[step_id] = tmp_img
                step_paths['prev'] = tmp_img
                step_paths['last'] = tmp_img
                step_paths['last_image'] = tmp_img
            except Exception:
                pass
            continue

        if name == 'social_download':
            res = run_social_download_action(params)
            last_result = res
            if res.get('success') and res.get('output_file'):
                p = os.path.join(cfg.MEDIA_TEMP_DIR, os.path.basename(res['output_file']))
                if os.path.exists(p):
                    created_paths.append(p)
                    step_paths[step_id] = p
                    step_paths['prev'] = p
                    step_paths['last'] = p
                    kind = _classify_path_kind(p, res.get('mime_type', ''))
                    if kind == 'audio':
                        step_paths['last_audio'] = p
                    else:
                        step_paths['last_video'] = p
            if not res.get('success'):
                return res, generated_images, created_paths
            continue

        if name in media_engine.SUPPORTED_MEDIA_OPERATIONS:
            paths = _resolve_step_files(file_ids, uploaded_files=uploaded_files, step_paths=step_paths)
            if not paths:
                paths = _auto_select_media_files(name, step_paths, uploaded_files=uploaded_files)
            if not paths:
                return {
                    'success': False,
                    'operation': name,
                    'error': 'Bu islem icin gerekli dosya secilmedi.',
                }, generated_images, created_paths
            out_path = media_engine.new_output_path(operation=name, params=params)
            ok, result = media_engine.execute_media_operation(name, paths, out_path, params)
            if not ok:
                return {
                    'success': False,
                    'operation': name,
                    'error': api_utils.safe_error_message(result),
                }, generated_images, created_paths
            res_str = str(result)
            created_paths.append(res_str)
            descriptor = media_engine.result_descriptor(name, result)
            try:
                descriptor['summary'] = media_engine.media_summary(result)
            except Exception:
                pass
            attach_download(descriptor, result)
            step_paths[step_id] = res_str
            step_paths['prev'] = res_str
            step_paths['last'] = res_str
            kind = _classify_path_kind(res_str, descriptor.get('mime_type', ''))
            if kind == 'image':
                step_paths['last_image'] = res_str
            elif kind == 'audio':
                step_paths['last_audio'] = res_str
            else:
                step_paths['last_video'] = res_str
            last_result = descriptor
            continue

        return {
            'success': False,
            'operation': name,
            'error': 'Bilinmeyen islem: %s' % name,
        }, generated_images, created_paths

    return last_result, generated_images, created_paths


def run_action(action, uploaded_files=None, device_id=None, username_lower=None):
    """Tek veya cok adimli AI eylemini calistirir (geriye donuk uyumlu tek sonuc dondurur)."""
    final_res, _, _ = run_action_pipeline(
        action,
        uploaded_files=uploaded_files,
        device_id=device_id,
        username_lower=username_lower,
    )
    return final_res or {'success': False, 'operation': '', 'error': 'Islem calistirilamadi'}


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
                'error': 'Aslan AI ses uretimi su an tamamlanamadi.'}

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
    descriptor['engine'] = 'Aslan AI'
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
    if worker_client.is_enabled():
        return worker_client.call_social_download(url, params)

    try:
        import yt_dlp
    except ImportError:
        return {'success': False, 'operation': 'social_download',
                'error': 'Aslan AI video indirme ozelligi su an kullanilamiyor.'}

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
        'cachedir': False,
        'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
        'merge_output_format': 'mp4',
        'socket_timeout': 30,
        'retries': 2,
        'max_filesize': cfg.MAX_FILE_SIZE,
    }
    ff = media_engine.ffmpeg_path()
    if ff:
        options['ffmpeg_location'] = ff
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
                'error': api_utils.safe_error_message(e, 'Video indirilemedi. Lutfen baglantiyi kontrol edip tekrar deneyin.')}
