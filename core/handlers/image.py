"""
Vercel sunucusuz fonksiyonu: Metinden gorsel uretimi.

Zorunlu kullanici girisi (401) ve hesaba bagli kalici DB kotasi (gunluk 3 / haftalik 5 / aylik 5).
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ai_engine, api_utils, auth, config as cfg, database, quota as quota_mod

IMAGE_MONTHLY_LIMIT = cfg.IMAGE_MONTHLY_LIMIT


class handler(BaseHTTPRequestHandler):
    """Gorsel uretimi ucu (giris ve kota kontrollu)."""

    def do_POST(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='POST, OPTIONS')
                return

            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Görsel üretimi için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='POST, OPTIONS')
                return

            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=cfg.MAX_JSON_BODY_BYTES)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg}, methods='POST, OPTIONS')
                return

            data = api_utils.json_body(raw, {})
            prompt = str(data.get('prompt', '') or '').strip()
            if not prompt:
                api_utils.send_json(self, 400, {'error': 'Gorsel icin aciklama gerekli'},
                                    methods='POST, OPTIONS')
                return
            if len(prompt) > cfg.MAX_PROMPT_CHARS:
                api_utils.send_json(
                    self, 400,
                    {'error': 'Aciklama en fazla %d karakter olabilir' % cfg.MAX_PROMPT_CHARS},
                    methods='POST, OPTIONS',
                )
                return

            ip = api_utils.client_ip(self.headers)
            ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                ip, 'image', cfg.IP_IMAGE_DAILY_LIMIT, quota_mod.DAY)
            if not ip_ok:
                api_utils.send_json(
                    self, 429,
                    {'error': 'Gunluk ag gorsel sinirina ulasildi. %d saniye sonra tekrar deneyin.'
                              % ip_usage.get('reset_in', 60)},
                    methods='POST, OPTIONS',
                )
                return

            uname_lower = user['username_lower']
            ok, result = ai_engine.generate_image(prompt, username_lower=uname_lower)
            fresh_user = database.get_user_by_username(uname_lower) or user
            snap = database.user_quota_snapshot(fresh_user)

            if not ok:
                status = 429 if result.get('quota_exceeded') else 502
                api_utils.send_json(
                    self, status,
                    {
                        'error': api_utils.safe_error_message(
                            result.get('error'), 'Gorsel uretilemedi'),
                        'quota': snap,
                    },
                    methods='POST, OPTIONS',
                )
                return

            api_utils.send_json(
                self, 200,
                {
                    'success': True,
                    'image_base64': result['image_base64'],
                    'mime_type': result['mime_type'],
                    'prompt': result['prompt'],
                    'engine': 'Aslan AI',
                    'quota': snap,
                },
                methods='POST, OPTIONS',
            )
        except Exception as e:
            api_utils.send_json(
                self, 500, {'error': api_utils.safe_error_message(e)},
                methods='POST, OPTIONS',
            )

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='POST, OPTIONS')

    def log_message(self, format, *args):
        pass
