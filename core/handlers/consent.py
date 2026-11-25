"""
Vercel sunucusuz fonksiyonu: Cerez onayi ve kategori tercihlerinin saklanmasi.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import api_utils, auth, config as cfg, database, privacy


class handler(BaseHTTPRequestHandler):
    """Cerez onayi ucu (GET: oku, POST: kaydet)."""

    def _subject_id(self) -> str:
        user, _ = auth.authenticate_request(self.headers)
        if user:
            return 'user:' + user['username_lower']
        return api_utils.request_device_id(self.headers)

    def do_GET(self):
        try:
            subject_id = self._subject_id()
            stored = database.get_consent_db(subject_id) or privacy.get_consent(subject_id)
            consent_val = stored.get('consent', 'unknown')
            api_utils.send_json(
                self, 200,
                {
                    'consent': consent_val,
                    'preferences': stored.get('preferences', {}),
                    'device_id': subject_id,
                    'timestamp': stored.get('timestamp'),
                    'locked': (consent_val == 'accepted'),
                },
                methods='GET, POST, OPTIONS',
            )
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

            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=cfg.MAX_JSON_BODY_BYTES)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg},
                                    methods='GET, POST, OPTIONS')
                return

            data = api_utils.json_body(raw, {})
            consent = str(data.get('consent', '') or '').strip()
            if consent not in ('accepted', 'rejected', 'custom'):
                api_utils.send_json(self, 400, {'error': 'Gecersiz onay degeri'},
                                    methods='GET, POST, OPTIONS')
                return

            subject_id = self._subject_id()
            existing = database.get_consent_db(subject_id) or privacy.get_consent(subject_id)
            if existing.get('consent') == 'accepted':
                if consent != 'accepted':
                    api_utils.send_json(
                        self, 409,
                        {
                            'error': 'Çerez onayı daha önce kabul edildiği için değiştirilemez veya sıfırlanamaz.',
                            'consent': 'accepted',
                            'locked': True,
                        },
                        methods='GET, POST, OPTIONS',
                    )
                    return
                existing_prefs = existing.get('preferences') or {
                    'necessary': True, 'functional': True, 'analytics': True
                }
                api_utils.send_json(
                    self, 200,
                    {
                        'success': True,
                        'consent': 'accepted',
                        'preferences': existing_prefs,
                        'device_id': subject_id,
                        'timestamp': existing.get('timestamp'),
                        'locked': True,
                    },
                    methods='GET, POST, OPTIONS',
                )
                return

            prefs = data.get('preferences') if isinstance(data.get('preferences'), dict) else {}
            clean_prefs = {
                'necessary': True,
                'functional': bool(prefs.get('functional', consent == 'accepted')),
                'analytics': bool(prefs.get('analytics', False)),
            }
            ip = api_utils.client_ip(self.headers)
            stored = database.store_consent_db(subject_id, consent, clean_prefs, ip=ip)
            privacy.store_consent(subject_id, consent, ip=ip)
            api_utils.send_json(
                self, 200,
                {
                    'success': True,
                    'consent': consent,
                    'preferences': clean_prefs,
                    'device_id': subject_id,
                    'timestamp': stored.get('timestamp'),
                    'locked': (consent == 'accepted'),
                },
                methods='GET, POST, OPTIONS',
            )
        except Exception as e:
            api_utils.send_json(
                self, 500, {'error': api_utils.safe_error_message(e)},
                methods='GET, POST, OPTIONS',
            )

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='GET, POST, OPTIONS')

    def log_message(self, format, *args):
        pass
