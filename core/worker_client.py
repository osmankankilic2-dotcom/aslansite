"""
Harici FFmpeg ve yt-dlp medya isleme worker istemcisi.

MEDIA_WORKER_URL yapilandirildiginda, Vercel serverless sure/boyut sinirlarini
asan FFmpeg ve yt-dlp islemlerini guvenli sekilde harici worker sunucusuna iletir.
"""
import json
import urllib.error
import urllib.request

from . import api_utils, config as cfg


def is_enabled() -> bool:
    """Harici medya worker sunucusu yapilandirilmis mi?"""
    return bool(cfg.MEDIA_WORKER_URL)


def _post_worker(path: str, payload: dict, timeout: int = 55) -> dict:
    url = '%s%s' % (cfg.MEDIA_WORKER_URL, path)
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'ASLAN-Worker-Client/1.0',
    }
    if cfg.MEDIA_WORKER_TOKEN:
        headers['X-Worker-Token'] = cfg.MEDIA_WORKER_TOKEN

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode('utf-8'))
            return parsed if isinstance(parsed, dict) else {'success': False, 'error': 'Aslan AI medya servisinden gecersiz yanit alindi.'}
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', errors='replace')
            parsed_err = json.loads(body)
            if isinstance(parsed_err, dict) and parsed_err.get('error'):
                return {'success': False, 'error': api_utils.safe_error_message(parsed_err['error'])}
        except Exception:
            pass
        return {'success': False, 'error': 'Aslan AI medya servisi gecici olarak yanit veremedi.'}
    except Exception as e:
        return {'success': False, 'error': api_utils.safe_error_message(e, 'Aslan AI medya servisine ulasilamadi.')}


def call_media_process(operation: str, files_inline: list, params: dict = None, timeout: int = 55) -> dict:
    """FFmpeg islemini harici worker uzerinden calistirir."""
    return _post_worker(
        '/worker/media/process',
        {'operation': operation, 'files': files_inline or [], 'params': params or {}},
        timeout=timeout,
    )


def call_social_download(url: str, params: dict = None, timeout: int = 55) -> dict:
    """yt-dlp video indirme islemini harici worker uzerinden calistirir."""
    return _post_worker(
        '/worker/media/social',
        {'url': url, 'params': params or {}},
        timeout=timeout,
    )
