"""
Vercel Blob deposu entegrasyonu.

Hem buyuk medya sonuclari hem de sunucusuz ortamda sifrelenmis kullanici
veritabani kayitlari icin kullanilir.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config as cfg

API_VERSION = '12'
UPLOAD_TIMEOUT = 180


def _token() -> str:
    return (os.environ.get('BLOB_READ_WRITE_TOKEN') or cfg.BLOB_READ_WRITE_TOKEN or '').strip()


def _api_url() -> str:
    return (os.environ.get('VERCEL_BLOB_API_URL') or cfg.BLOB_API_URL or 'https://blob.vercel-storage.com').rstrip('/')


def _store_id() -> str:
    """Depo kimligini bulur (env veya erisim anahtarindan turetilir)."""
    sid = (os.environ.get('BLOB_STORE_ID') or cfg.BLOB_STORE_ID or '').strip()
    if sid:
        return sid
    token = _token()
    parts = token.split('_')
    # vercel_blob_rw_<storeId>_<secret>
    if len(parts) >= 5 and parts[0] == 'vercel' and parts[1] == 'blob':
        return parts[3]
    return ''


def blob_enabled() -> bool:
    """Blob deposu kullanilabilir mi?"""
    return bool(_token() and _store_id())


def put_exact_bytes(pathname: str, data: bytes, mime_type: str = 'application/octet-stream',
                    allow_overwrite: bool = True, timeout: int = 20):
    """
    Belirli bir pathname altina bayt yazar.

    Returns: (True, payload_dict) veya (False, reason_str)
      reason_str == 'already_exists' ise overwrite=False nedeniyle cakisma vardir.
    """
    token = _token()
    store_id = _store_id()
    if not token or not store_id or data is None:
        return False, 'disabled'

    encoded_path = urllib.parse.quote(str(pathname), safe='/')
    url = '%s/?pathname=%s' % (_api_url(), encoded_path)
    headers = {
        'authorization': 'Bearer %s' % token,
        'x-api-version': API_VERSION,
        'x-vercel-blob-store-id': store_id,
        'x-vercel-blob-access': 'public',
        'x-add-random-suffix': '0',
        'x-allow-overwrite': '1' if allow_overwrite else '0',
        'content-type': mime_type or 'application/octet-stream',
        'content-length': str(len(data)),
    }
    request = urllib.request.Request(url, data=data, headers=headers, method='PUT')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode('utf-8'))
            if isinstance(payload, dict) and payload.get('url'):
                return True, payload
            return False, 'invalid_response'
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', errors='replace').lower()
        except Exception:
            pass
        if e.code in (409, 412) or 'already exists' in body:
            return False, 'already_exists'
        return False, 'http_%s' % e.code
    except (urllib.error.URLError, ValueError, OSError) as e:
        return False, str(e)[:120]


def list_blobs(prefix: str, limit: int = 25, timeout: int = 15) -> list:
    """Belirli bir onek altindaki Vercel Blob kayitlarini listeler."""
    token = _token()
    store_id = _store_id()
    if not token or not store_id or not prefix:
        return []

    qs = urllib.parse.urlencode({'limit': str(int(limit)), 'prefix': str(prefix)})
    url = '%s/?%s' % (_api_url(), qs)
    headers = {
        'authorization': 'Bearer %s' % token,
        'x-api-version': API_VERSION,
        'x-vercel-blob-store-id': store_id,
    }
    try:
        req = urllib.request.Request(url, headers=headers, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            listing = json.loads(resp.read().decode('utf-8'))
            if isinstance(listing, dict):
                blobs = listing.get('blobs')
                return blobs if isinstance(blobs, list) else []
    except Exception:
        pass
    return []


def fetch_url_bytes(url: str, timeout: int = 15):
    """Verilen Blob URL adresinden baytlari indirir (hata durumunda None)."""
    if not url:
        return None
    sep = '&' if '?' in url else '?'
    fetch_url = '%s%s_cb=%d' % (url, sep, int(time.time() * 1000))
    try:
        req = urllib.request.Request(fetch_url, headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def delete_blob_urls(urls: list, timeout: int = 15) -> bool:
    """Verilen Blob URL listesini siler."""
    token = _token()
    store_id = _store_id()
    clean_urls = [str(u) for u in (urls or []) if u]
    if not token or not store_id or not clean_urls:
        return False

    del_url = '%s/delete' % _api_url()
    del_headers = {
        'authorization': 'Bearer %s' % token,
        'x-api-version': API_VERSION,
        'x-vercel-blob-store-id': store_id,
        'content-type': 'application/json',
    }
    try:
        body = json.dumps({'urls': clean_urls}).encode('utf-8')
        req = urllib.request.Request(del_url, data=body, headers=del_headers, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


def delete_blob_prefix(prefix: str, timeout: int = 15) -> bool:
    """Belirli onekle baslayan tum Vercel Blob kayitlarini siler."""
    if not prefix:
        return False
    blobs = list_blobs(prefix, limit=100, timeout=timeout)
    urls = [
        item.get('url') for item in blobs
        if str(item.get('pathname', '')).startswith(prefix) and item.get('url')
    ]
    if not urls:
        return False
    return delete_blob_urls(urls, timeout=timeout)


def get_blob_bytes(pathname: str, timeout: int = 15):
    """Belirli bir pathname'deki blob verisini indirir (yoksa None)."""
    if not pathname:
        return None
    for item in list_blobs(pathname, limit=5, timeout=timeout):
        if item.get('pathname') == pathname:
            target_url = item.get('url') or item.get('downloadUrl') or ''
            if target_url:
                return fetch_url_bytes(target_url, timeout=timeout)
    return None


def delete_blob_pathname(pathname: str, timeout: int = 15) -> bool:
    """Belirli bir pathname altindaki blob dosyasini siler."""
    if not pathname:
        return False
    urls = [
        item.get('url') for item in list_blobs(pathname, limit=5, timeout=timeout)
        if item.get('pathname') == pathname and item.get('url')
    ]
    if not urls:
        return False
    return delete_blob_urls(urls, timeout=timeout)


def upload_bytes(data, filename, mime_type='application/octet-stream', prefix='aslan'):
    """
    Baytlari Vercel Blob deposuna yukler.

    Returns: {'url': ..., 'download_url': ..., 'pathname': ...} veya None
    """
    if not blob_enabled() or not data:
        return None

    safe_name = os.path.basename(str(filename or 'dosya'))
    pathname = '%s/%s-%s' % (prefix, uuid.uuid4().hex[:12], safe_name)
    ok, payload = put_exact_bytes(pathname, data, mime_type=mime_type,
                                  allow_overwrite=False, timeout=UPLOAD_TIMEOUT)
    if not ok or not isinstance(payload, dict):
        return None
    return {
        'url': payload.get('url'),
        'download_url': payload.get('downloadUrl') or payload.get('url'),
        'pathname': payload.get('pathname', pathname),
        'content_type': payload.get('contentType', mime_type),
        'size': len(data),
    }


def upload_file(path, mime_type=None, prefix='aslan'):
    """Diskteki dosyayi Blob deposuna yukler."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    from .media_engine import guess_mime
    return upload_bytes(data, p.name, mime_type or guess_mime(p), prefix=prefix)
