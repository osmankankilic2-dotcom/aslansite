"""
Vercel sunucusuz tek giris noktasi ve API yonlendiricisi.

Tum /api/* isteklerini core/handlers/ altindaki ilgili isleyici sinifina yonlendirir.
Tek fonksiyon yapisi, coklu Python derleme (.venv) cakismalarini onler.
"""
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import api_utils
from core.handlers import ai, auth, consent, health, image, media, ping, quota, social, stt, tts, wa

_INDEX_PATHS = frozenset({'', '/api', '/api/index', '/api/index.py'})

_EXACT_ROUTES = {
    '/api/ping': ping.handler,
    '/api/health': health.handler,
    '/api/tts': tts.handler,
    '/api/stt': stt.handler,
    '/api/ai': ai.handler,
    '/api/media/process': media.handler,
    '/api/media/social': social.handler,
    '/api/image': image.handler,
    '/api/quota': quota.handler,
    '/api/cookie-consent': consent.handler,
}


def _normalize_route(raw_path: str) -> str:
    """Vercel rewrite parametrelerini (/api/index.py?__route=...) standart /api/... yoluna cevirir."""
    path_part, _, qs = (raw_path or '/').partition('?')
    params = urllib.parse.parse_qs(qs, keep_blank_values=True)
    clean_base = path_part.rstrip('/')

    if clean_base in _INDEX_PATHS:
        route_vals = params.pop('__route', None) or params.pop('path', None)
        val = (route_vals[0] if route_vals else '').strip('/')
        if val:
            norm_path = ('/' + val) if val.startswith('api/') or val == 'api' else ('/api/' + val)
        else:
            norm_path = clean_base or '/'
    else:
        params.pop('__route', None)
        norm_path = path_part or '/'

    rem = urllib.parse.urlencode(params, doseq=True)
    return '%s?%s' % (norm_path, rem) if rem else norm_path


def _resolve_handler(norm_path: str):
    """Normalize edilmis API yoluna karsilik gelen isleyici sinifini dondurur."""
    p = (norm_path or '/').split('?', 1)[0].rstrip('/').lower()
    if p == '/api/auth' or p.startswith('/api/auth/'):
        return auth.handler
    if p == '/api/wa' or p.startswith('/api/wa/'):
        return wa.handler
    return _EXACT_ROUTES.get(p)


class handler(BaseHTTPRequestHandler):
    """Tum API rotalari icin birlesik BaseHTTPRequestHandler yonlendiricisi."""

    def _dispatch(self, method: str) -> None:
        try:
            norm_path = _normalize_route(self.path)
            target_cls = _resolve_handler(norm_path)
            if target_cls is None:
                api_utils.send_json(
                    self, 404,
                    {'error': 'Bilinmeyen API yolu'},
                    methods='GET, POST, OPTIONS',
                )
                return

            fn = getattr(target_cls, 'do_' + method, None)
            if fn is None:
                api_utils.send_json(
                    self, 405,
                    {'error': 'Desteklenmeyen HTTP yontemi'},
                    methods='GET, POST, OPTIONS',
                )
                return

            sub = object.__new__(target_cls)
            sub.rfile = self.rfile
            sub.wfile = self.wfile
            sub.headers = self.headers
            sub.path = norm_path
            sub.command = getattr(self, 'command', method)
            sub.client_address = getattr(self, 'client_address', ('127.0.0.1', 0))
            sub.request_version = getattr(self, 'request_version', 'HTTP/1.1')
            sub.send_response = self.send_response
            sub.send_header = self.send_header
            sub.end_headers = self.end_headers
            fn(sub)
        except Exception as e:
            api_utils.send_json(
                self, 500,
                {'error': api_utils.safe_error_message(e)},
                methods='GET, POST, OPTIONS',
            )

    def do_GET(self):
        self._dispatch('GET')

    def do_HEAD(self):
        self._dispatch('HEAD')

    def do_POST(self):
        self._dispatch('POST')

    def do_OPTIONS(self):
        self._dispatch('OPTIONS')

    def log_message(self, format, *args):
        pass
