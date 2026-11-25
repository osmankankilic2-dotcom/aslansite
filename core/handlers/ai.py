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

from core import ai_engine, ai_settings, api_utils, auth, config as cfg, database, pipeline, quota as quota_mod, wa_bot

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

            files_inline = data.get('files')
            if files_inline is None:
                files_inline = []
            if not isinstance(files_inline, list):
                api_utils.send_json(self, 400, {'error': 'Gecersiz dosya listesi'},
                                    methods='POST, OPTIONS')
                return
            context_inline = data.get('context_files')
            if context_inline is None:
                context_inline = []
            if not isinstance(context_inline, list):
                api_utils.send_json(self, 400, {'error': 'Gecersiz baglam dosya listesi'},
                                    methods='POST, OPTIONS')
                return
            if len(files_inline) + len(context_inline) > cfg.MAX_INLINE_FILES:
                api_utils.send_json(
                    self, 400,
                    {'error': 'Tek istekte en fazla %d dosya eklenebilir.' % cfg.MAX_INLINE_FILES},
                    methods='POST, OPTIONS',
                )
                return
            history = data.get('history')
            if not isinstance(history, list):
                history = []
            chat_id = str(data.get('chat_id', '') or '').strip()

            metas = []
            for i, entry in enumerate(files_inline):
                path, meta = self._decode_part(entry, i)
                if path:
                    paths.append(path)
                    metas.append(meta)

            context_metas = []
            for i, entry in enumerate(context_inline):
                path, meta = self._decode_part(entry, i + len(metas))
                if path:
                    paths.append(path)
                    context_metas.append(meta)

            if not message and not metas:
                api_utils.send_json(self, 400, {'error': 'Mesaj veya dosya bos'},
                                    methods='POST, OPTIONS')
                return

            # Hesaba bagli DB kota kontrolu:
            # - WhatsApp bot kurulumu ucretsizdir (standart hak dusulmez)
            # - Kurulum sonrasi WhatsApp bot hizmeti ayri 10 haklik kotadan dusulur
            # - Normal sohbetler standart gunluk AI hakkiyla devam eder
            uname_lower = user['username_lower']
            credit_mode, credit_bucket = wa_bot.classify_credit_policy(uname_lower, message)
            usage = database.user_quota_snapshot(user)['ai']
            ip = api_utils.client_ip(self.headers)

            if credit_bucket == 'ai':
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
            elif credit_bucket == 'wa_bot':
                wa_allowed, wa_usage = database.check_user_wa_quota(uname_lower)
                if not wa_allowed:
                    snap = database.user_quota_snapshot(user)
                    api_utils.send_json(
                        self, 429,
                        {
                            'error': ('WhatsApp bot hizmeti için tanımlı %d kullanım hakkınız doldu.'
                                      % wa_usage.get('limit', cfg.WA_BOT_QUOTA_LIMIT)),
                            'remaining': snap['ai']['remaining'],
                            'quota': snap,
                        },
                        methods='POST, OPTIONS',
                    )
                    return

                ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                    ip, 'wa', cfg.IP_WA_HOURLY_LIMIT, 3600)
                if not ip_ok:
                    api_utils.send_json(
                        self, 429,
                        {'error': 'Bu agdan cok fazla bot istegi gonderildi. Lutfen %d saniye sonra tekrar deneyin.'
                                  % ip_usage.get('reset_in', 60)},
                        methods='POST, OPTIONS',
                    )
                    return

            wa_state_cur = database.get_user_wa_bot(uname_lower)
            extra_ctx = ''
            if credit_bucket in ('free', 'wa_bot') or wa_bot.is_wa_related_message(message, wa_state_cur):
                extra_ctx = wa_bot.build_ai_context(uname_lower, message)

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

            all_available = metas + context_metas
            user_ai_cfg = ai_settings.build_runtime_ai_settings(uname_lower)
            reply, error = ai_engine.generate_reply(
                message, parts=parts, available_files=all_available,
                api_key=ai_engine.get_api_key(), history=history, extra_context=extra_ctx,
                user_ai_config=user_ai_cfg)
            if not reply:
                api_utils.send_json(
                    self, 502,
                    {'error': api_utils.safe_error_message(error, 'Yanit alinamadi')},
                    methods='POST, OPTIONS',
                )
                return

            media_result = None
            image_result = None
            generated_images = []
            wa_event = None
            action = ai_engine.extract_action(reply)
            if action:
                clean_reply = ai_engine.strip_action(reply)
                if isinstance(action, dict) and action.get('action') and not action.get('files'):
                    action = dict(action)
                    action['files'] = [m['id'] for m in metas]
                media_result, generated_images, pipe_paths = ai_engine.run_action_pipeline(
                    action, uploaded_files=all_available, username_lower=uname_lower)
                created_outputs.extend(pipe_paths)
                wa_handled, media_result, reply, wa_event = wa_bot.process_action_outcome(
                    media_result, clean_reply, reply)
                if wa_handled and credit_bucket == 'ai' and isinstance(action, dict):
                    if action.get('action') in ('wa_bot_send_message', 'wa_bot_update', 'wa_bot_stop'):
                        credit_bucket = 'wa_bot'
                if not wa_handled and isinstance(media_result, dict):
                    if media_result.get('operation') == 'generate_image' and media_result.get('success'):
                        image_result = media_result
                    if media_result.get('success'):
                        reply = clean_reply or 'Islem tamamlandi.'

            reply = wa_bot.scrub_sensitive_reply(reply)

            # Kota dusumu: kurulum ucretsiz, kurulum sonrasi WA bot hizmeti ayri 10 hak, normal sohbet gunluk AI hakki
            if credit_bucket == 'ai':
                _, usage = database.consume_user_ai_quota(uname_lower)
            elif credit_bucket == 'wa_bot':
                database.consume_user_wa_quota(uname_lower)

            chat_saved = database.upsert_user_chat_turn(
                uname_lower, chat_id, message or '(dosya gönderildi)', reply)
            saved_chat = chat_saved.get('chat') or {}

            fresh_user = database.get_user_by_username(uname_lower) or user
            snap = database.user_quota_snapshot(fresh_user)
            api_utils.send_json(
                self, 200,
                {
                    'reply': reply,
                    'remaining': snap['ai']['remaining'],
                    'quota': snap,
                    'chat_id': saved_chat.get('id') or chat_id,
                    'chat_title': saved_chat.get('title') or '',
                    'media_result': media_result,
                    'image_result': image_result,
                    'generated_images': generated_images,
                    'wa_event': wa_event,
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
