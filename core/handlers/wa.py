"""
Vercel sunucusuz fonksiyonu: Aslan AI WhatsApp bot durumu, onay ve durdurma.
"""
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import api_utils, auth, config as cfg, wa_bot


class handler(BaseHTTPRequestHandler):
    """WhatsApp bot yonetim uclari (/api/wa/status, /api/wa/confirm-connected, /api/wa/stop)."""

    def _action_name(self) -> str:
        raw_path = self.path or ''
        path_part, _, qs = raw_path.partition('?')
        seg = path_part.rstrip('/').rsplit('/', 1)[-1].lower()
        if seg in ('', 'wa', 'wa.py') and qs:
            q_val = (urllib.parse.parse_qs(qs).get('path') or [''])[0]
            q_seg = q_val.strip('/').rsplit('/', 1)[-1].lower()
            if q_seg:
                return q_seg
        return seg

    def do_GET(self):
        try:
            action = self._action_name()
            if action != 'status':
                api_utils.send_json(self, 404, {'error': 'Bilinmeyen WhatsApp yolu'},
                                    methods='GET, POST, OPTIONS')
                return
            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Bu özellik için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='GET, POST, OPTIONS')
                return
            snap = wa_bot.get_status_snapshot(user['username_lower'])
            api_utils.send_json(self, 200, snap, methods='GET, POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, OPTIONS')

    def do_POST(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='GET, POST, OPTIONS')
                return
            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Bu özellik için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='GET, POST, OPTIONS')
                return

            action = self._action_name()
            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=64 * 1024)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg},
                                    methods='GET, POST, OPTIONS')
                return
            data = api_utils.json_body(raw, {})
            uname_lower = user['username_lower']

            if action == 'confirm-connected':
                res = wa_bot.confirm_connected(uname_lower)
                api_utils.send_json(self, 200 if res.get('success') else 400, res,
                                    methods='GET, POST, OPTIONS')
                return
            if action == 'stop':
                res = wa_bot.stop_user_bot(uname_lower, logout=bool(data.get('logout')))
                api_utils.send_json(self, 200, res, methods='GET, POST, OPTIONS')
                return

            api_utils.send_json(self, 404, {'error': 'Bilinmeyen WhatsApp yolu'},
                                methods='GET, POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, OPTIONS')

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='GET, POST, OPTIONS')

    def log_message(self, format, *args):
        pass
