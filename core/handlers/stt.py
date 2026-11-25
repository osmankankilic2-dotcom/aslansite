"""
Vercel sunucusuz fonksiyonu: Ses kaydindan metne (Speech-to-Text).
"""
import base64
import binascii
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ai_engine, api_utils, auth, config as cfg, quota as quota_mod


class handler(BaseHTTPRequestHandler):
    """Ses-metin donusum ucu (POST /api/stt)."""

    def do_POST(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='POST, OPTIONS')
                return

            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Sesli mesaj dönüstürme için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='POST, OPTIONS')
                return

            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=cfg.MAX_JSON_BODY_BYTES)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg}, methods='POST, OPTIONS')
                return

            data = api_utils.json_body(raw, {})
            audio_b64 = str(data.get('audio_base64') or data.get('data') or '').strip()
            mime_type = str(data.get('mime_type') or 'audio/webm').strip().lower()
            lang = str(data.get('lang') or 'tr-TR').strip()

            if audio_b64.startswith('data:'):
                audio_b64 = audio_b64.split(',', 1)[1] if ',' in audio_b64 else ''
            if not audio_b64:
                api_utils.send_json(self, 400, {'error': 'Ses verisi gerekli.'}, methods='POST, OPTIONS')
                return
            try:
                audio_bytes = base64.b64decode(audio_b64, validate=False)
            except (binascii.Error, ValueError):
                api_utils.send_json(self, 400, {'error': 'Gecersiz ses verisi.'}, methods='POST, OPTIONS')
                return
            if not audio_bytes or len(audio_bytes) > cfg.MAX_JSON_BODY_BYTES:
                api_utils.send_json(self, 400, {'error': 'Ses dosyasi bos veya cok buyuk.'},
                                    methods='POST, OPTIONS')
                return

            ip = api_utils.client_ip(self.headers)
            ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                ip, 'stt', cfg.IP_STT_HOURLY_LIMIT, 3600)
            if not ip_ok:
                api_utils.send_json(
                    self, 429,
                    {'error': 'Cok fazla ses donusturme istegi. %d saniye sonra tekrar deneyin.'
                              % ip_usage.get('reset_in', 60)},
                    methods='POST, OPTIONS',
                )
                return

            text, err = ai_engine.transcribe_audio(audio_bytes, mime_type=mime_type, lang=lang)
            if not text:
                api_utils.send_json(
                    self, 502,
                    {'error': api_utils.safe_error_message(err, 'Ses metne donusturulemedi.')},
                    methods='POST, OPTIONS',
                )
                return

            api_utils.send_json(self, 200, {'success': True, 'text': text}, methods='POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(self, 500, {'error': api_utils.safe_error_message(e)},
                                methods='POST, OPTIONS')

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='POST, OPTIONS')

    def log_message(self, format, *args):
        pass
