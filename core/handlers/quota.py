"""
Vercel sunucusuz fonksiyonu: Hesap bazli kota bilgisi.

Giris yapmis kullanici varsa dogrudan veritabanindaki hesaba bagli haklari
dondurur; cerezsiz calisir.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import api_utils, auth, config as cfg, database


def _unauth_snapshot() -> dict:
    return {
        'authenticated': False,
        'account_bound': False,
        'ai': {'limit': cfg.AI_DAILY_LIMIT, 'used': 0, 'remaining': cfg.AI_DAILY_LIMIT},
        'image': {
            'daily_limit': cfg.IMAGE_DAILY_LIMIT,
            'weekly_limit': cfg.IMAGE_WEEKLY_LIMIT,
            'monthly_limit': cfg.IMAGE_MONTHLY_LIMIT,
            'daily_remaining': cfg.IMAGE_DAILY_LIMIT,
            'weekly_remaining': cfg.IMAGE_WEEKLY_LIMIT,
            'monthly_remaining': cfg.IMAGE_MONTHLY_LIMIT,
        },
    }


class handler(BaseHTTPRequestHandler):
    """Kota durumu ucu."""

    def _respond(self):
        user, _ = auth.authenticate_request(self.headers)
        if user:
            snap = database.user_quota_snapshot(user)
            api_utils.send_json(
                self, 200,
                {'authenticated': True, 'user': {'username': user['username']}, **snap},
                methods='GET, POST, OPTIONS',
            )
            return
        api_utils.send_json(self, 200, _unauth_snapshot(), methods='GET, POST, OPTIONS')

    def do_GET(self):
        try:
            self._respond()
        except Exception as e:
            api_utils.send_json(
                self, 500, {'error': api_utils.safe_error_message(e)},
                methods='GET, POST, OPTIONS',
            )

    def do_POST(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='GET, POST, OPTIONS')
                return
            self._respond()
        except Exception as e:
            api_utils.send_json(
                self, 500, {'error': api_utils.safe_error_message(e)},
                methods='GET, POST, OPTIONS',
            )

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='GET, POST, OPTIONS')

    def log_message(self, format, *args):
        pass
