"""
Vercel sunucusuz fonksiyonu: Birlesik Yapay Zeka Saglayici & Model Ayarlari.

Rotalar:
  GET  /api/settings/ai
  POST /api/settings/ai
"""
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ai_settings, api_utils, auth, config as cfg, quota as quota_mod


class handler(BaseHTTPRequestHandler):
    """Kullanici AI saglayici / model / ozel HTTPS uc nokta ayarlari ucu."""

    def _sub_action(self) -> str:
        raw_path = self.path or ''
        path_part, _, qs = raw_path.partition('?')
        seg = path_part.rstrip('/').rsplit('/', 1)[-1].lower()
        if seg in ('', 'settings') and qs:
            q_val = (urllib.parse.parse_qs(qs).get('path') or [''])[0]
            q_seg = q_val.strip('/').rsplit('/', 1)[-1].lower()
            if q_seg:
                return q_seg
        return seg

    def do_GET(self):
        try:
            action = self._sub_action()
            if action not in ('ai', 'settings'):
                api_utils.send_json(self, 404, {'error': 'Bilinmeyen ayar yolu'},
                                    methods='GET, POST, OPTIONS')
                return
            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Ayarları görüntülemek için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='GET, POST, OPTIONS')
                return
            payload = ai_settings.get_client_settings_payload(user['username_lower'])
            api_utils.send_json(self, 200, payload, methods='GET, POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, OPTIONS')

    def do_POST(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='GET, POST, OPTIONS')
                return
            action = self._sub_action()
            if action not in ('ai', 'settings'):
                api_utils.send_json(self, 404, {'error': 'Bilinmeyen ayar yolu'},
                                    methods='GET, POST, OPTIONS')
                return
            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Ayarları kaydetmek için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='GET, POST, OPTIONS')
                return

            ip = api_utils.client_ip(self.headers)
            ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                ip, 'settings', getattr(cfg, 'IP_SETTINGS_HOURLY_LIMIT', 40), 3600)
            if not ip_ok:
                api_utils.send_json(self, 429, {
                    'error': 'Çok fazla ayar güncelleme isteği. %d saniye sonra tekrar deneyin.'
                             % ip_usage.get('reset_in', 60)
                }, methods='GET, POST, OPTIONS')
                return

            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=32 * 1024)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg},
                                    methods='GET, POST, OPTIONS')
                return
            data = api_utils.json_body(raw, {})
            ok, res_or_err, status = ai_settings.update_user_ai_settings(
                user['username_lower'], data)
            if not ok:
                api_utils.send_json(self, status, {'error': res_or_err},
                                    methods='GET, POST, OPTIONS')
                return
            api_utils.send_json(self, status, res_or_err, methods='GET, POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, OPTIONS')

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='GET, POST, OPTIONS')

    def log_message(self, format, *args):
        pass
