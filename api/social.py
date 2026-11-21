"""
Vercel sunucusuz fonksiyonu: Sosyal medya video indirme (SSRF korumali).
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ai_engine, api_utils, config as cfg, pipeline, quota as quota_mod

cfg.ensure_dirs()


class handler(BaseHTTPRequestHandler):
    """Sosyal medya video indirme ucu (POST /api/media/social)."""

    def do_POST(self):
        out_path = None
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='POST, OPTIONS')
                return

            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=cfg.MAX_JSON_BODY_BYTES)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg}, methods='POST, OPTIONS')
                return

            data = api_utils.json_body(raw, {})
            url = str(data.get('url', '') or '').strip()
            valid, clean_or_err = api_utils.validate_social_url(url)
            if not valid:
                api_utils.send_json(self, 400, {'error': clean_or_err}, methods='POST, OPTIONS')
                return

            ip = api_utils.client_ip(self.headers)
            ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                ip, 'social', cfg.IP_SOCIAL_DAILY_LIMIT, quota_mod.DAY)
            if not ip_ok:
                api_utils.send_json(
                    self, 429,
                    {'error': 'Gunluk video indirme sinirina ulasildi. %d saniye sonra tekrar deneyin.'
                              % ip_usage.get('reset_in', 60)},
                    methods='POST, OPTIONS',
                )
                return

            params = data.get('params') if isinstance(data.get('params'), dict) else {}
            result = ai_engine.download_social_media(clean_or_err, params)
            if isinstance(result, dict) and result.get('output_file'):
                out_path = os.path.join(
                    cfg.MEDIA_TEMP_DIR, os.path.basename(str(result['output_file'])))

            if not result.get('success'):
                api_utils.send_json(self, 502, result, methods='POST, OPTIONS')
                return

            api_utils.send_json(self, 200, result, methods='POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(
                self, 500, {'error': api_utils.safe_error_message(e)},
                methods='POST, OPTIONS',
            )
        finally:
            if out_path:
                pipeline.cleanup_files([out_path])

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='POST, OPTIONS')

    def log_message(self, format, *args):
        pass
