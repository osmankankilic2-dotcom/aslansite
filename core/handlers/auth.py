"""
Vercel sunucusuz fonksiyonu: Kullanici kayit, giris, cikis ve oturum dogrulama.

Rotalar:
  POST /api/auth/register
  POST /api/auth/login
  POST /api/auth/logout
  GET  /api/auth/me
"""
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import api_utils, auth, config as cfg, database, quota as quota_mod


class handler(BaseHTTPRequestHandler):
    """Cerezsiz Bearer token tabanli kimlik dogrulama ucu."""

    def _action_name(self) -> str:
        raw_path = self.path or ''
        path_part, _, qs = raw_path.partition('?')
        seg = path_part.rstrip('/').rsplit('/', 1)[-1].lower()
        if seg in ('', 'auth', 'auth.py') and qs:
            q_val = (urllib.parse.parse_qs(qs).get('path') or [''])[0]
            q_seg = q_val.strip('/').rsplit('/', 1)[-1].lower()
            if q_seg:
                return q_seg
        return seg

    def do_GET(self):
        try:
            action = self._action_name()
            if action != 'me':
                api_utils.send_json(self, 404, {'error': 'Bilinmeyen kimlik dogrulama yolu'},
                                    methods='GET, POST, OPTIONS')
                return

            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'authenticated': False,
                    'error': 'Oturum bulunamadi veya suresi doldu.',
                }, methods='GET, POST, OPTIONS')
                return

            api_utils.send_json(self, 200, {
                'authenticated': True,
                'user': {'username': user['username']},
                'quota': database.user_quota_snapshot(user),
            }, methods='GET, POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, OPTIONS')

    def do_POST(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='GET, POST, OPTIONS')
                return

            action = self._action_name()
            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=64 * 1024)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg},
                                    methods='GET, POST, OPTIONS')
                return
            data = api_utils.json_body(raw, {})

            if action == 'logout':
                auth.logout_request(self.headers)
                api_utils.send_json(self, 200, {'success': True}, methods='GET, POST, OPTIONS')
                return

            ip = api_utils.client_ip(self.headers)
            if action == 'register':
                ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                    ip, 'auth_reg', cfg.IP_AUTH_HOURLY_LIMIT, 3600)
                if not ip_ok:
                    api_utils.send_json(self, 429, {
                        'error': 'Cok fazla kayit denemesi yapildi. Lutfen %d saniye sonra tekrar deneyin.'
                                 % ip_usage.get('reset_in', 60)
                    }, methods='GET, POST, OPTIONS')
                    return
                ok, payload_or_err, status = auth.register_user(
                    data.get('username'),
                    data.get('password'),
                    data.get('password_confirm'),
                )
                if not ok:
                    api_utils.send_json(self, status, {'error': payload_or_err},
                                        methods='GET, POST, OPTIONS')
                    return
                api_utils.send_json(self, status, {'success': True, **payload_or_err},
                                    methods='GET, POST, OPTIONS')
                return

            if action == 'login':
                ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                    ip, 'auth_login', cfg.IP_AUTH_HOURLY_LIMIT, 3600)
                if not ip_ok:
                    api_utils.send_json(self, 429, {
                        'error': 'Cok fazla giris denemesi yapildi. Lutfen %d saniye sonra tekrar deneyin.'
                                 % ip_usage.get('reset_in', 60)
                    }, methods='GET, POST, OPTIONS')
                    return
                ok, payload_or_err, status = auth.login_user(
                    data.get('username'),
                    data.get('password'),
                )
                if not ok:
                    api_utils.send_json(self, status, {'error': payload_or_err},
                                        methods='GET, POST, OPTIONS')
                    return
                api_utils.send_json(self, status, {'success': True, **payload_or_err},
                                    methods='GET, POST, OPTIONS')
                return

            api_utils.send_json(self, 404, {'error': 'Bilinmeyen kimlik dogrulama yolu'},
                                methods='GET, POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, OPTIONS')

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='GET, POST, OPTIONS')

    def log_message(self, format, *args):
        pass
