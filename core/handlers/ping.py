"""
Vercel sunucusuz fonksiyonu: Kota tuketmeyen hafif uyanik tutma (/api/ping) ucu.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import api_utils, keepalive


class handler(BaseHTTPRequestHandler):
    """Sessiz 60 saniyelik keep-alive saglik ucu (GET/HEAD /api/ping)."""

    def do_GET(self):
        try:
            api_utils.send_json(
                self, 200,
                keepalive.ping_payload(),
                methods='GET, HEAD, OPTIONS',
            )
        except Exception:
            api_utils.send_json(self, 200, {'ok': True}, methods='GET, HEAD, OPTIONS')

    def do_HEAD(self):
        try:
            keepalive.touch_background_workers()
        except Exception:
            pass
        self.send_response(200)
        hdrs = {
            'Content-Type': 'application/json; charset=utf-8',
            'Content-Length': '0',
        }
        hdrs.update(api_utils.security_headers(is_api=True))
        hdrs.update(api_utils.cors_headers(getattr(self, 'headers', None), methods='GET, HEAD, OPTIONS'))
        for k, v in hdrs.items():
            self.send_header(k, str(v))
        self.end_headers()

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='GET, HEAD, OPTIONS')

    def log_message(self, format, *args):
        pass
