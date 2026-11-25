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

from core import api_utils, auth, config as cfg, database, quota as quota_mod, wa_bot


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
            if action not in ('status', 'contacts'):
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
            if action == 'contacts':
                contacts = wa_bot.get_known_contacts(user['username_lower'])
                api_utils.send_json(self, 200, {'success': True, 'contacts': contacts},
                                    methods='GET, POST, OPTIONS')
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
            if action == 'wake':
                res = wa_bot.wake_user_bot(uname_lower, force=bool(data.get('force', True)))
                api_utils.send_json(self, 200 if res.get('success') else 502, res,
                                    methods='GET, POST, OPTIONS')
                return
            if action == 'send':
                wa_allowed, wa_usage = database.check_user_wa_quota(uname_lower)
                if not wa_allowed:
                    snap = database.user_quota_snapshot(user)
                    api_utils.send_json(self, 429, {
                        'error': 'WhatsApp bot hizmeti için tanımlı %d kullanım hakkınız doldu.'
                                 % wa_usage.get('limit', cfg.WA_BOT_QUOTA_LIMIT),
                        'quota': snap,
                    }, methods='GET, POST, OPTIONS')
                    return
                ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                    api_utils.client_ip(self.headers), 'wa', cfg.IP_WA_HOURLY_LIMIT, 3600)
                if not ip_ok:
                    api_utils.send_json(self, 429, {
                        'error': 'Saatlik bot istek sınırına ulaşıldı. %d saniye sonra tekrar deneyin.'
                                 % ip_usage.get('reset_in', 60)
                    }, methods='GET, POST, OPTIONS')
                    return
                res = wa_bot.send_user_message(
                    uname_lower,
                    target=data.get('target') or data.get('to') or data.get('phone') or '',
                    message=data.get('message') or data.get('text') or '',
                )
                if res.get('success'):
                    database.consume_user_wa_quota(uname_lower)
                fresh_u = database.get_user_by_username(uname_lower) or user
                res['quota'] = database.user_quota_snapshot(fresh_u)
                api_utils.send_json(self, 200 if res.get('success') else 400, res,
                                    methods='GET, POST, OPTIONS')
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
