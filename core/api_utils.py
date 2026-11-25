"""
Sunucusuz fonksiyonlar (Vercel) ve Flask icin ortak guvenlik ve istek yardimcilari.
"""
import ipaddress
import json
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from . import config as cfg
from .privacy import device_identifier, fingerprint_seed

_DEVICE_ID_RE = re.compile(r'^[a-zA-Z0-9_.:-]{4,128}$')
_SAFE_EXT_RE = re.compile(r'^\.[a-z0-9]{1,8}$')
_LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1', '[::1]'})
_BACKEND_LEAK_RE = re.compile(
    r'\b(?:gemini|imagen|nano[-_ ]?banana|pollinations|googleapis|generativelanguage|'
    r'duckduckgo|duckchat|deepinfra|openai|gpt-[0-9a-z.-]+|chatgpt|mistral|deepseek|llama|claude|anthropic|groq|'
    r'hugging ?face|flux|stability|sdxl|ffmpeg|ffprobe|libav\w*|libx26\d|'
    r'yt[-_ ]?dlp|youtube[-_ ]?dl|baileys|whiskeysockets|edge[-_ ]?tts|gtts|pillow|imageio|'
    r'static[-_ ]?ffmpeg|vercel|flask|werkzeug|gunicorn|uvicorn|urllib|sqlite|'
    r'turso|libsql|worker|pbkdf2|whisper|openrouter|replicate|cohere|perplexity)\b',
    re.IGNORECASE,
)
_RAW_TECH_ERR_RE = re.compile(
    r'(?:https?://\S+|traceback\s*\(|http error\s+\d{3}|urlerror|'
    r'modulenotfounderror|importerror|filenotfounderror|calledprocesserror|\[errno\s+\d+\])',
    re.IGNORECASE,
)


def _header(headers, name: str) -> str:
    """Baslik degerini buyuk/kucuk harfe duyarsiz sekilde okur."""
    if not headers:
        return ''
    try:
        val = headers.get(name)
        if val:
            return str(val)
        lower_name = name.lower()
        for k, v in headers.items():
            if str(k).lower() == lower_name and v:
                return str(v)
    except Exception:
        pass
    return ''


def _valid_ip(candidate: str) -> str:
    """Aday metin gecerli bir IPv4/IPv6 adresi ise temiz metni dondurur, degilse ''."""
    s = str(candidate or '').strip()
    if not s:
        return ''
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return ''


def client_ip(headers) -> str:
    """Istegin gercek IP adresini dondurur (Vercel/Cloudflare basliklari desteklenir)."""
    try:
        for header_name in ('X-Vercel-Forwarded-For', 'X-Forwarded-For', 'X-Real-IP'):
            raw = _header(headers, header_name)
            if not raw:
                continue
            for part in raw.split(','):
                ip = _valid_ip(part)
                if ip:
                    return ip
        return '127.0.0.1'
    except Exception:
        return '127.0.0.1'


def is_https_request(headers) -> bool:
    """Istek HTTPS uzerinden mi geldi?"""
    proto = _header(headers, 'X-Forwarded-Proto').lower()
    if proto:
        return proto.split(',')[0].strip() == 'https'
    origin = _header(headers, 'Origin').lower()
    if origin.startswith('https://'):
        return True
    host = _header(headers, 'Host').lower().split(':')[0]
    return host not in _LOCAL_HOSTS and bool(host)


def request_device_id(headers) -> str:
    """
    Istekten kalici cihaz kimligini turetir.

    Oncelik sirasi:
      1) Gecerli bicimli X-Device-Id basligi
      2) X-Device-Seed basligi + User-Agent (sunucu tarafi parmak izi)
    """
    try:
        provided = _header(headers, 'X-Device-Id').strip()
        if provided and not _DEVICE_ID_RE.fullmatch(provided):
            provided = ''
        seed_header = _header(headers, 'X-Device-Seed').strip()[:128]
        ua = _header(headers, 'User-Agent')[:256]
        seed = fingerprint_seed(ua, seed_header)
        return device_identifier(provided=provided, extra_seed=seed)
    except Exception:
        return device_identifier()


def _parse_env_host(raw_val: str) -> str:
    """VERCEL_URL gibi ortam degiskeninden alan adini (host) guvenli sekilde cikarir."""
    val = str(raw_val or '').strip()
    if not val:
        return ''
    if '://' in val:
        try:
            return (urlparse(val).hostname or '').lower().rstrip('.')
        except Exception:
            return ''
    return val.split('/')[0].split(':')[0].lower().rstrip('.')


def is_origin_allowed(origin: str, host: str = '') -> bool:
    """Istegin Origin/Referer basliginin izinli kaynaklardan gelip gelmedigini denetler."""
    origin = str(origin or '').strip()
    if not origin:
        return True

    try:
        parsed = urlparse(origin)
    except Exception:
        return False

    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False

    origin_host = (parsed.hostname or '').lower().rstrip('.')
    origin_netloc = parsed.netloc.lower()
    if not origin_host:
        return False

    req_host = str(host or '').split(',')[0].strip().lower()
    req_hostname = req_host.split(':')[0]
    if req_host and origin_netloc == req_host:
        return True
    if req_hostname and origin_host == req_hostname:
        return True

    if origin_host in _LOCAL_HOSTS and (not os.environ.get('VERCEL') or req_hostname in _LOCAL_HOSTS):
        return True

    if origin_host == 'aslansite.vercel.app':
        return True

    for env_key in ('VERCEL_URL', 'VERCEL_BRANCH_URL', 'VERCEL_PROJECT_PRODUCTION_URL'):
        env_host = _parse_env_host(os.environ.get(env_key, ''))
        if env_host and origin_host == env_host:
            return True

    extra = os.environ.get('ALLOWED_ORIGINS', '')
    for item in extra.split(','):
        candidate = item.strip().lower()
        if not candidate:
            continue
        if candidate == origin.lower() or candidate == origin_host:
            return True

    return False


def reject_disallowed_origin(headers) -> bool:
    """Cross-site veya izin verilmeyen Origin/Referer baslikli istekleri reddeder."""
    sec_fetch_site = _header(headers, 'Sec-Fetch-Site').strip().lower()
    if sec_fetch_site == 'cross-site':
        return True

    host = _header(headers, 'X-Forwarded-Host') or _header(headers, 'Host')
    origin = _header(headers, 'Origin').strip()
    if origin:
        return not is_origin_allowed(origin, host)

    referer = _header(headers, 'Referer').strip()
    if referer:
        return not is_origin_allowed(referer, host)

    return False


def reject_cross_site_request(headers) -> bool:
    """reject_disallowed_origin icin aciklayici takma ad."""
    return reject_disallowed_origin(headers)


def cors_headers(headers, methods: str = 'GET, POST, OPTIONS') -> dict:
    """Yalnizca izinli kaynaklar icin kontrollu CORS basliklari uretir."""
    out = {
        'Access-Control-Allow-Methods': methods,
        'Access-Control-Allow-Headers': (
            'Content-Type, Authorization, X-Auth-Token, '
            'X-Device-Id, X-Device-Seed, X-Requested-With'
        ),
        'Vary': 'Origin',
    }
    origin = _header(headers, 'Origin').strip()
    host = _header(headers, 'X-Forwarded-Host') or _header(headers, 'Host')
    if origin and is_origin_allowed(origin, host):
        out['Access-Control-Allow-Origin'] = origin
        out['Access-Control-Allow-Credentials'] = 'true'
    return out


def security_headers(is_api: bool = True) -> dict:
    """Ortak HTTP guvenlik basliklarini dondurur."""
    headers = {
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'strict-origin-when-cross-origin',
        'Cross-Origin-Opener-Policy': 'same-origin',
        'Cross-Origin-Resource-Policy': 'same-origin',
        'X-Permitted-Cross-Domain-Policies': 'none',
        'X-DNS-Prefetch-Control': 'off',
        'X-Download-Options': 'noopen',
    }
    if is_api:
        headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
        headers['Pragma'] = 'no-cache'
    return headers


def read_body_limited(rfile, headers, max_bytes: int = cfg.MAX_JSON_BODY_BYTES):
    """
    HTTP govdesini boyut sinirini asmadan okur.

    Returns: (raw_bytes, error_status, error_message)
    """
    raw_len = _header(headers, 'Content-Length').strip()
    if raw_len:
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            return None, 400, 'Gecersiz Content-Length basligi.'
        if length < 0:
            return None, 400, 'Gecersiz Content-Length basligi.'
        if length > max_bytes:
            return None, 413, 'Istek boyutu siniri (%d MB) asildi.' % (max_bytes // (1024 * 1024))
        if length == 0:
            return b'', None, None
        data = rfile.read(length) if rfile else b''
        if len(data) > max_bytes:
            return None, 413, 'Istek boyutu siniri asildi.'
        return data, None, None

    if 'chunked' in _header(headers, 'Transfer-Encoding').lower() and rfile:
        data = rfile.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None, 413, 'Istek boyutu siniri asildi.'
        return data, None, None

    return b'', None, None


def send_json(handler_obj, status: int, payload: dict,
              methods: str = 'GET, POST, OPTIONS', extra_headers=None) -> None:
    """BaseHTTPRequestHandler uzerinden guvenli JSON yaniti gonderir."""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler_obj.send_response(status)
    headers_map = {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': str(len(body)),
    }
    headers_map.update(security_headers(is_api=True))
    headers_map.update(cors_headers(getattr(handler_obj, 'headers', None), methods=methods))
    for k, v in headers_map.items():
        handler_obj.send_header(k, v)
    if extra_headers:
        items = extra_headers.items() if isinstance(extra_headers, dict) else extra_headers
        for k, v in items:
            if v is not None:
                handler_obj.send_header(k, str(v))
    handler_obj.end_headers()
    handler_obj.wfile.write(body)


def send_options(handler_obj, methods: str = 'GET, POST, OPTIONS') -> None:
    """BaseHTTPRequestHandler uzerinden OPTIONS preflight yaniti gonderir."""
    if reject_disallowed_origin(getattr(handler_obj, 'headers', None)):
        send_json(handler_obj, 403, {'error': 'Kaynak izni reddedildi.'}, methods=methods)
        return
    handler_obj.send_response(200)
    headers_map = {}
    headers_map.update(security_headers(is_api=True))
    headers_map.update(cors_headers(getattr(handler_obj, 'headers', None), methods=methods))
    for k, v in headers_map.items():
        handler_obj.send_header(k, v)
    handler_obj.end_headers()


def safe_extension(filename: str, allowed_exts=cfg.ALLOWED_UPLOAD_EXTS) -> str:
    """Guvenli ve izin verilen dosya uzantisini dondurur (gecersizse '')."""
    base = os.path.basename(str(filename or '').strip())
    ext = Path(base).suffix.lower()
    if not ext or not _SAFE_EXT_RE.fullmatch(ext):
        return ''
    if allowed_exts is not None and ext not in allowed_exts:
        return ''
    return ext


def safe_error_message(err, fallback: str = 'Aslan AI islemi sirasinda beklenmeyen bir hata olustu.') -> str:
    """Hata metninden API anahtarlari, sunucu yollari ve arka plan teknoloji adlarini temizler."""
    text = str(err or '').strip()
    if not text:
        return fallback
    text = re.sub(r'([?&]key=)[^&\s"\']+', r'\1[gizli]', text, flags=re.IGNORECASE)
    text = re.sub(r'AIza[0-9A-Za-z_-]{10,}', '[gizli-anahtar]', text)
    text = re.sub(r'gsk_[0-9A-Za-z_-]{10,}', '[gizli-anahtar]', text)
    text = re.sub(r'sk-[0-9A-Za-z_-]{12,}', '[gizli-anahtar]', text)
    text = re.sub(r'vercel_blob_[0-9A-Za-z_-]+', '[gizli-anahtar]', text)
    text = re.sub(r'aslan1\.[A-Za-z0-9_.-]+', '[gizli-jeton]', text)
    text = re.sub(r'(Bearer\s+)[0-9A-Za-z._-]+', r'\1[gizli]', text, flags=re.IGNORECASE)
    text = re.sub(r'(?:/data|/var/task|/tmp|/home)/[^\s\'"<>]+', '[yol]', text)
    if _BACKEND_LEAK_RE.search(text) or _RAW_TECH_ERR_RE.search(text):
        return fallback
    return text[:240] or fallback


def validate_social_url(url: str):
    """
    Sosyal medya video indirme baglantisini SSRF ve etki alani acisindan dogrular.

    Returns: (gecerli_mi, hata_mesaji_veya_temiz_url)
    """
    cleaned = str(url or '').strip()
    if not cleaned:
        return False, 'Video baglantisi gerekli.'
    if len(cleaned) > 2048 or any(ch.isspace() for ch in cleaned):
        return False, 'Gecersiz baglanti bicimi.'

    try:
        parsed = urlparse(cleaned)
    except Exception:
        return False, 'Gecersiz baglanti.'

    if parsed.scheme.lower() not in ('http', 'https'):
        return False, 'Yalnizca http veya https baglantilari desteklenir.'
    if parsed.username or parsed.password:
        return False, 'Kimlik bilgisi iceren baglantilara izin verilmiyor.'

    hostname = (parsed.hostname or '').lower().rstrip('.')
    if not hostname:
        return False, 'Gecersiz alan adi.'

    try:
        ipaddress.ip_address(hostname)
        return False, 'Dogrudan IP adreslerine izin verilmiyor.'
    except ValueError:
        pass

    if parsed.port is not None and parsed.port not in (80, 443):
        return False, 'Gecersiz baglanti noktasi.'

    allowed = any(
        hostname == dom or hostname.endswith('.' + dom)
        for dom in cfg.SUPPORTED_SOCIAL_PLATFORMS
    )
    if not allowed:
        return False, (
            'Desteklenmeyen platform. TikTok, YouTube, Facebook, '
            'Instagram, X, Reddit, Twitch ve SoundCloud desteklenir.'
        )

    try:
        infos = socket.getaddrinfo(hostname, None)
        for item in infos:
            addr = item[4][0].split('%', 1)[0]
            ip_obj = ipaddress.ip_address(addr)
            if not ip_obj.is_global or ip_obj.is_multicast:
                return False, 'Guvenlik nedeniyle bu hedef adresine izin verilmiyor.'
    except socket.gaierror:
        # DNS gecici erisilemezse hostname zaten katı allowlist'ten gecmistir.
        pass
    except Exception:
        return False, 'Hedef alan adi dogrulanamadi.'

    return True, cleaned


def json_body(raw, fallback=None):
    """Govdeyi JSON olarak cozer (hatali durumda fallback dondurur)."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8', errors='replace')
        if not raw:
            return fallback if fallback is not None else {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else (fallback if fallback is not None else {})
    except Exception:
        return fallback if fallback is not None else {}


def json_response(status: int, payload: dict, headers=None):
    """JSON yanit dondurur (geriye donuk uyumluluk yardimcisi)."""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    base = {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': str(len(body)),
        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Access-Control-Allow-Headers': (
            'Content-Type, Authorization, X-Auth-Token, '
            'X-Device-Id, X-Device-Seed, X-Requested-With'
        ),
    }
    base.update(security_headers(is_api=True))
    if headers:
        base.update(headers)
    return status, base, body
