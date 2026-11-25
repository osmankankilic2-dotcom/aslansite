"""
Vercel sunucusuz fonksiyonu: 30 Gunluk Devam Edilebilir Sohbet Gecmisi ve Sabitleme.

Rotalar:
  GET    /api/chats
  GET    /api/chats/<chat_id>
  POST   /api/chats
  POST   /api/chats/pin   | POST /api/chats/<chat_id>/pin
  POST   /api/chats/delete | DELETE /api/chats/<chat_id>
"""
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import api_utils, auth, database


class handler(BaseHTTPRequestHandler):
    """Sohbet gecmisi ve maksimum 2 sabitleme koruma ucu."""

    def _sub_parts(self) -> list:
        path_part = (self.path or '/').split('?', 1)[0].strip('/')
        segs = [urllib.parse.unquote(s) for s in path_part.split('/') if s]
        if len(segs) >= 2 and segs[0].lower() == 'api' and segs[1].lower() == 'chats':
            return segs[2:]
        return []

    def do_GET(self):
        try:
            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Sohbet geçmişi için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='GET, POST, DELETE, OPTIONS')
                return
            uname_lower = user['username_lower']
            parts = self._sub_parts()
            if not parts:
                api_utils.send_json(self, 200, database.list_user_chats(uname_lower),
                                    methods='GET, POST, DELETE, OPTIONS')
                return
            chat_obj = database.get_user_chat(uname_lower, parts[0])
            if not chat_obj:
                api_utils.send_json(self, 404, {'error': 'Sohbet bulunamadı.'},
                                    methods='GET, POST, DELETE, OPTIONS')
                return
            api_utils.send_json(self, 200, {'chat': chat_obj},
                                methods='GET, POST, DELETE, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, DELETE, OPTIONS')

    def do_POST(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='GET, POST, DELETE, OPTIONS')
                return
            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Sohbet geçmişi için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='GET, POST, DELETE, OPTIONS')
                return

            uname_lower = user['username_lower']
            parts = self._sub_parts()
            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=2 * 1024 * 1024)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg},
                                    methods='GET, POST, DELETE, OPTIONS')
                return
            data = api_utils.json_body(raw, {})

            if len(parts) == 1 and parts[0].lower() == 'pin':
                ok, res_or_err, status = database.set_user_chat_pinned(
                    uname_lower, data.get('chat_id') or data.get('id') or '', bool(data.get('pinned')))
                payload = res_or_err if isinstance(res_or_err, dict) else {'error': res_or_err}
                api_utils.send_json(self, status, payload, methods='GET, POST, DELETE, OPTIONS')
                return

            if len(parts) == 2 and parts[1].lower() == 'pin':
                ok, res_or_err, status = database.set_user_chat_pinned(
                    uname_lower, parts[0], bool(data.get('pinned')))
                payload = res_or_err if isinstance(res_or_err, dict) else {'error': res_or_err}
                api_utils.send_json(self, status, payload, methods='GET, POST, DELETE, OPTIONS')
                return

            if len(parts) == 1 and parts[0].lower() == 'delete':
                ok, res = database.delete_user_chat(
                    uname_lower, data.get('chat_id') or data.get('id') or '')
                api_utils.send_json(self, 200 if ok else 404, res,
                                    methods='GET, POST, DELETE, OPTIONS')
                return

            if not parts:
                res = database.create_or_save_user_chat(
                    uname_lower,
                    chat_id=data.get('chat_id') or data.get('id') or '',
                    title=data.get('title') or '',
                    messages=data.get('messages') if isinstance(data.get('messages'), list) else None,
                )
                api_utils.send_json(self, 200, res, methods='GET, POST, DELETE, OPTIONS')
                return

            api_utils.send_json(self, 404, {'error': 'Bilinmeyen sohbet yolu.'},
                                methods='GET, POST, DELETE, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, DELETE, OPTIONS')

    def do_DELETE(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='GET, POST, DELETE, OPTIONS')
                return
            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Sohbet silmek için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='GET, POST, DELETE, OPTIONS')
                return
            parts = self._sub_parts()
            if not parts:
                api_utils.send_json(self, 400, {'error': 'Sohbet kimliği gerekli.'},
                                    methods='GET, POST, DELETE, OPTIONS')
                return
            ok, res = database.delete_user_chat(user['username_lower'], parts[0])
            api_utils.send_json(self, 200 if ok else 404, res,
                                methods='GET, POST, DELETE, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='GET, POST, DELETE, OPTIONS')

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='GET, POST, DELETE, OPTIONS')

    def log_message(self, format, *args):
        pass
