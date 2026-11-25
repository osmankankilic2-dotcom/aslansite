"""
Vercel sunucusuz fonksiyonu: Medya isleme (arka plan motoru).

Dosyalar govde icinde base64 olarak gonderilir; islem sunucusuz ortamda
tek seferde calistirilir, sonuc inline data URL veya Vercel Blob olarak doner,
ardindan gecici dosyalar temizlenir.
"""
import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import api_utils, blob_store, config as cfg, media_engine, pipeline, quota as quota_mod, worker_client

cfg.ensure_dirs()

SUPPORTED = media_engine.SUPPORTED_MEDIA_OPERATIONS
MAX_INLINE_RESULT = cfg.MAX_INLINE_RESULT_BYTES


class handler(BaseHTTPRequestHandler):
    """Medya isleme ucu."""

    def _decode_part(self, entry, index=0):
        path, meta = pipeline.decode_inline_file(entry, index)
        if not path:
            return None, None
        return path, meta

    def do_POST(self):
        paths = []
        result_path = None
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

            try:
                data = json.loads(raw.decode('utf-8')) if raw else {}
            except (ValueError, UnicodeDecodeError):
                api_utils.send_json(self, 400, {'error': 'Gecersiz JSON formati'},
                                    methods='POST, OPTIONS')
                return
            if not isinstance(data, dict):
                api_utils.send_json(self, 400, {'error': 'Gecersiz istek govdesi'},
                                    methods='POST, OPTIONS')
                return

            operation = str(data.get('operation') or '').strip()
            params = data.get('params')
            if params is None:
                params = {}
            elif not isinstance(params, dict):
                api_utils.send_json(self, 400, {'error': 'Gecersiz parametre bicimi'},
                                    methods='POST, OPTIONS')
                return
            inline = data.get('files')
            if inline is None:
                inline = []
            elif not isinstance(inline, list):
                api_utils.send_json(self, 400, {'error': 'Gecersiz dosya listesi'},
                                    methods='POST, OPTIONS')
                return
            if len(inline) > cfg.MAX_INLINE_FILES:
                api_utils.send_json(
                    self, 400,
                    {'error': 'Tek istekte en fazla %d dosya islenebilir.' % cfg.MAX_INLINE_FILES},
                    methods='POST, OPTIONS',
                )
                return

            if not operation:
                api_utils.send_json(self, 400, {'error': 'Islem gerekli'},
                                    methods='POST, OPTIONS')
                return
            if operation not in SUPPORTED:
                api_utils.send_json(self, 400, {'error': 'Bilinmeyen islem: %s' % operation},
                                    methods='POST, OPTIONS')
                return

            ip = api_utils.client_ip(self.headers)
            ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                ip, 'media', cfg.IP_MEDIA_HOURLY_LIMIT, 3600)
            if not ip_ok:
                api_utils.send_json(
                    self, 429,
                    {'error': 'Saatlik medya isleme sinirina ulasildi. %d saniye sonra tekrar deneyin.'
                              % ip_usage.get('reset_in', 60)},
                    methods='POST, OPTIONS',
                )
                return

            if worker_client.is_enabled():
                w_res = worker_client.call_media_process(operation, inline, params)
                if not w_res.get('success'):
                    api_utils.send_json(
                        self, 502,
                        {'error': api_utils.safe_error_message(w_res.get('error'), 'Aslan AI medya islemi tamamlanamadi.')},
                        methods='POST, OPTIONS',
                    )
                    return
                api_utils.send_json(self, 200, w_res, methods='POST, OPTIONS')
                return

            for i, entry in enumerate(inline):
                path, _ = self._decode_part(entry, i)
                if path:
                    paths.append(path)

            if not paths:
                api_utils.send_json(self, 400, {'error': 'Gecerli dosya gonderilmedi'},
                                    methods='POST, OPTIONS')
                return

            output = media_engine.new_output_path(operation=operation, params=params)
            ok, result = media_engine.execute_media_operation(operation, paths, output, params)
            if not ok:
                api_utils.send_json(
                    self, 500,
                    {'error': 'Islem basarisiz: %s' % api_utils.safe_error_message(result)},
                    methods='POST, OPTIONS',
                )
                return

            result_path = str(result)
            descriptor = media_engine.result_descriptor(operation, result)
            try:
                descriptor['summary'] = media_engine.media_summary(result)
            except Exception:
                pass

            try:
                size = Path(result).stat().st_size
                if size <= MAX_INLINE_RESULT:
                    payload = base64.b64encode(Path(result).read_bytes()).decode('ascii')
                    descriptor['data_url'] = 'data:%s;base64,%s' % (
                        descriptor.get('mime_type', 'application/octet-stream'), payload)
                else:
                    uploaded = blob_store.upload_file(result)
                    if uploaded and uploaded.get('url'):
                        descriptor['download_url'] = uploaded['url']
            except Exception:
                pass

            api_utils.send_json(self, 200, descriptor, methods='POST, OPTIONS')
        except Exception as e:
            api_utils.send_json(
                self, 500, {'error': api_utils.safe_error_message(e)},
                methods='POST, OPTIONS',
            )
        finally:
            to_clean = list(paths)
            if result_path:
                to_clean.append(result_path)
            pipeline.cleanup_files(to_clean)

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='POST, OPTIONS')

    def log_message(self, format, *args):
        pass
