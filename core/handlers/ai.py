"""
Vercel sunucusuz fonksiyonu: Aslan AI Asistan.

Zorunlu kullanici dogrulamasi (401), hesaba bagli kalici DB kotasi,
ucretsiz acik uclar oncelikli + Gemini son durak zinciri ve otonom eylemler.
"""
import base64
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ai_engine, api_utils, auth, config as cfg, database, pipeline, quota as quota_mod

cfg.ensure_dirs()
AI_DAILY_LIMIT = cfg.AI_DAILY_LIMIT


class handler(BaseHTTPRequestHandler):
    """Aslan AI sohbet ucu (giris zorunlu + multimodal + otonom eylemler)."""

    def _decode_part(self, entry, index=0):
        path, meta = pipeline.decode_inline_file(entry, index)
        if not path:
            return None, None
        return path, meta

    def do_POST(self):
        paths = []
        created_outputs = []
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='POST, OPTIONS')
                return

            # Zorunlu erisim kontrolu: giris yapmamis kullanici AI kullanamaz
            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Aslan AI sohbetini kullanmak için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='POST, OPTIONS')
                return

            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=cfg.MAX_JSON_BODY_BYTES)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg}, methods='POST, OPTIONS')
                return

            data = api_utils.json_body(raw, {})
            message = str(data.get('message', '') or '').strip()
            if len(message) > cfg.MAX_MESSAGE_CHARS:
                api_utils.send_json(
                    self, 400,
                    {'error': 'Mesaj en fazla %d karakter olabilir.' % cfg.MAX_MESSAGE_CHARS},
                    methods='POST, OPTIONS',
                )
                return

            files_inline = data.get('files') or []
            history = data.get('history') or []

            metas = []
            for i, entry in enumerate(files_inline):
                path, meta = self._decode_part(entry, i)
                if path:
                    paths.append(path)
                    metas.append(meta)

            if not message and not paths:
                api_utils.send_json(self, 400, {'error': 'Mesaj veya dosya bos'},
                                    methods='POST, OPTIONS')
                return

            # Hesaba bagli kalici DB kota kontrolu (harcamadan once kontrol)
            uname_lower = user['username_lower']
            allowed, usage = database.check_user_ai_quota(uname_lower)
            if not allowed:
                snap = database.user_quota_snapshot(user)
                api_utils.send_json(
                    self, 429,
                    {
                        'error': ('Gunluk %d mesaj hakkiniz doldu. Hakkiniz %d saniye sonra yenilenir.'
                                  % (AI_DAILY_LIMIT, usage.get('reset_in', 0))),
                        'remaining': 0,
                        'quota': snap,
                    },
                    methods='POST, OPTIONS',
                )
                return

            ip = api_utils.client_ip(self.headers)
            ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                ip, 'ai', cfg.IP_AI_DAILY_LIMIT, quota_mod.DAY)
            if not ip_ok:
                api_utils.send_json(
                    self, 429,
                    {'error': 'Bu agdan gunluk istek sinirina ulasildi. Lutfen %d saniye sonra tekrar deneyin.'
                              % ip_usage.get('reset_in', 60)},
                    methods='POST, OPTIONS',
                )
                return

            parts = []
            for meta in metas:
                try:
                    blob = Path(meta['path']).read_bytes()
                except Exception:
                    continue
                mime_type = (
                    meta.get('mime_type')
                    or mimetypes.guess_type(meta.get('name', ''))[0]
                    or 'application/octet-stream'
                )
                if (mime_type.startswith(('image/', 'video/', 'audio/'))
                        or mime_type == 'application/pdf'):
                    parts.append({'inline_data': {
                        'mime_type': mime_type,
                        'data': base64.b64encode(blob).decode('utf-8'),
                    }})
                elif (mime_type.startswith('text/')
                      or mime_type in ('application/json', 'text/csv')):
                    parts.append({'text': '[Dosya icerigi: %s]\n%s'
                                  % (meta['name'], blob.decode('utf-8', errors='ignore'))})
                else:
                    parts.append({'text': '[Desteklenmeyen dosya turu: %s (%s)]'
                                  % (meta['name'], mime_type)})

            reply, error = ai_engine.generate_reply(
                message, parts=parts, available_files=metas,
                api_key=ai_engine.get_api_key(), history=history)
            if not reply:
                api_utils.send_json(
                    self, 502,
                    {'error': api_utils.safe_error_message(error, 'Yanit alinamadi')},
                    methods='POST, OPTIONS',
                )
                return

            # Yalnizca basarili yanit sonrasi hesaptan 1 hak dusulur
            _, usage = database.consume_user_ai_quota(uname_lower)

            media_result = None
            image_result = None
            action = ai_engine.extract_action(reply)
            if action:
                clean_reply = ai_engine.strip_action(reply)
                action = dict(action)
                if not action.get('files'):
                    action['files'] = [m['id'] for m in metas]
                media_result = ai_engine.run_action(
                    action, uploaded_files=metas, username_lower=uname_lower)
                if isinstance(media_result, dict):
                    out_name = media_result.get('output_file')
                    if out_name and out_name != 'aslan-gorsel.png':
                        created_outputs.append(os.path.join(cfg.MEDIA_TEMP_DIR, os.path.basename(out_name)))
                    if media_result.get('operation') == 'generate_image' and media_result.get('success'):
                        image_result = media_result
                    if media_result.get('success'):
                        reply = clean_reply or 'Islem tamamlandi.'

            fresh_user = database.get_user_by_username(uname_lower) or user
            snap = database.user_quota_snapshot(fresh_user)
            api_utils.send_json(
                self, 200,
                {
                    'reply': reply,
                    'remaining': usage['remaining'],
                    'quota': snap,
                    'media_result': media_result,
                    'image_result': image_result,
                },
                methods='POST, OPTIONS',
            )
        except Exception as e:
            api_utils.send_json(
                self, 500, {'error': api_utils.safe_error_message(e)},
                methods='POST, OPTIONS',
            )
        finally:
            pipeline.cleanup_files(paths + created_outputs)

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='POST, OPTIONS')

    def log_message(self, format, *args):
        pass
