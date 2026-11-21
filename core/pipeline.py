"""
Ortak islem hatti (pipeline).

Vercel sunucusuz fonksiyonlari durumsuzdur: bir istekte yuklenen dosya
sonraki istekte bulunamayabilir. Bu yuzden medya isleme akisi
"tek istek, tam icerik" modeliyle kurulur:

    1) Istemci dosyalari base64 olarak gonderir.
    2) Sunucu dosyalari kendi gecici dizinine yazar.
    3) Islemin tamami ayni istek icinde calisir.
    4) Sonuc base64 (data URL) olarak geri doner; boylece indirme baglantisi
       farkli bir sunucu ornegine dusse bile calisir.

Yerel Flask sunucusu da ayni hatti kullanir; boylece iki ortam birebir ayni
davranisi gosterir.
"""
import base64
import binascii
import os
import uuid
from pathlib import Path

from .config import MEDIA_TEMP_DIR, MAX_FILE_SIZE
from . import api_utils

# Sonucun base64 olarak gonderilebilecegi maksimum boyut (~3 MB)
MAX_INLINE_RESULT_BYTES = 3 * 1024 * 1024


def decode_inline_file(entry, index=0):
    """
    Istemciden gelen base64 dosya kaydini gecici dizine yazar.

    Returns: (dosya_yolu, meta_sozlugu) veya (None, hata_mesaji)
    """
    if not isinstance(entry, dict):
        return None, 'Gecersiz dosya kaydi'

    name = str(entry.get('name') or 'dosya_%d' % index)
    mime_type = str(entry.get('mime_type') or entry.get('type') or
                    'application/octet-stream')
    raw = entry.get('data') or ''

    if isinstance(raw, str) and raw.startswith('data:'):
        raw = raw.split(',', 1)[1] if ',' in raw else ''

    try:
        data = base64.b64decode(raw, validate=False) if raw else b''
    except (binascii.Error, ValueError):
        return None, 'Dosya verisi cozulemedi (gecersiz base64)'

    if not data:
        return None, 'Dosya bos'

    if len(data) > MAX_FILE_SIZE:
        return None, 'Dosya cok buyuk (en fazla %d MB)' % (MAX_FILE_SIZE // (1024 * 1024))

    ext = api_utils.safe_extension(name)
    filename = '%s%s' % (uuid.uuid4().hex, ext)
    target = Path(MEDIA_TEMP_DIR) / filename
    try:
        target.write_bytes(data)
    except Exception as e:
        return None, 'Dosya yazilamadi: %s' % api_utils.safe_error_message(e)

    meta = {
        'id': filename,
        'name': os.path.basename(name),
        'mime_type': mime_type,
        'path': str(target),
        'size': len(data),
    }
    return str(target), meta


def materialize(files_inline):
    """
    Inline dosya listesini gecici dosya yollarina cevirir.

    Returns: (yollar, aciklamalar, hata_mesaji)
    """
    paths, metas, error = [], [], None
    for i, entry in enumerate(files_inline or []):
        path, meta = decode_inline_file(entry, i)
        if path is None:
            error = error or meta
            continue
        paths.append(path)
        metas.append(meta)
    return paths, metas, error


def encode_result(path, inline_limit=MAX_INLINE_RESULT_BYTES):
    """
    Islenmis dosyayi indirilmeye hazir hale getirir.

    Returns: (aciklama_sozlugu, base64_veya_None)
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except Exception:
        size = 0

    payload = None
    if size <= inline_limit:
        try:
            payload = base64.b64encode(p.read_bytes()).decode('ascii')
        except Exception:
            payload = None

    return {
        'output_file': p.name,
        'size': size,
        'download_url': '/api/media/download/%s' % p.name,
    }, payload


def build_data_url(payload, mime_type):
    """Base64 veriyi data URL'ye cevirir."""
    return 'data:%s;base64,%s' % (mime_type or 'application/octet-stream', payload)


def cleanup_files(paths):
    """Gecici dosyalari siler."""
    for path in paths or []:
        try:
            p = Path(path)
            if p.is_file():
                p.unlink()
        except Exception:
            pass
