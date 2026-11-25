"""
Sessiz arka plan uyanik tutma (keep-alive) yardimcisi.
Kota veya oturum hakki tuketmeden aktif servisleri ve baglantilari canli tutar.
"""
import os
import threading
import time
import urllib.request

from . import config as cfg

_WAKE_LOCK = threading.Lock()
_LAST_WAKE_TS = 0.0
_MIN_WAKE_INTERVAL = 30.0


def _collect_worker_targets() -> list:
    targets = []
    wa_url = (os.environ.get('WA_BOT_WORKER_URL') or cfg.WA_BOT_WORKER_URL or '').strip().rstrip('/')
    if not wa_url and not os.environ.get('VERCEL'):
        wa_port = int(os.environ.get('WA_WORKER_PORT') or cfg.WA_BOT_LOCAL_PORT or 5056)
        wa_url = 'http://127.0.0.1:%d' % wa_port
    if wa_url:
        wa_tok = (
            os.environ.get('WA_BOT_WORKER_TOKEN')
            or os.environ.get('MEDIA_WORKER_TOKEN')
            or cfg.WA_BOT_WORKER_TOKEN
            or ''
        ).strip()
        targets.append((wa_url + '/ping', wa_tok))

    media_url = (os.environ.get('MEDIA_WORKER_URL') or cfg.MEDIA_WORKER_URL or '').strip().rstrip('/')
    if media_url:
        m_tok = (os.environ.get('MEDIA_WORKER_TOKEN') or cfg.MEDIA_WORKER_TOKEN or '').strip()
        targets.append((media_url + '/ping', m_tok))

    return targets


def _ping_targets_sync(targets: list) -> None:
    for url, tok in targets:
        try:
            headers = {'User-Agent': 'Aslan-AI-KeepAlive/1.0'}
            if tok:
                headers['X-Worker-Token'] = tok
            req = urllib.request.Request(url, headers=headers, method='GET')
            with urllib.request.urlopen(req, timeout=1.5):
                pass
        except Exception:
            pass


def touch_background_workers() -> bool:
    """Arka plan servislerini kisa aralik siniriyla sessizce uyandirir."""
    global _LAST_WAKE_TS
    now = time.time()
    with _WAKE_LOCK:
        if (now - _LAST_WAKE_TS) < _MIN_WAKE_INTERVAL:
            return False
        _LAST_WAKE_TS = now

    targets = _collect_worker_targets()
    if not targets:
        return True

    if os.environ.get('VERCEL'):
        _ping_targets_sync(targets)
    else:
        t = threading.Thread(target=_ping_targets_sync, args=(targets,), daemon=True)
        t.start()
    return True


def ping_payload() -> dict:
    """Kota tuketmeyen ultra hafif /api/ping yanitini dondurur."""
    touch_background_workers()
    return {'ok': True, 'ts': int(time.time())}
