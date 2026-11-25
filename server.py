#!/usr/bin/env python3
"""
ASLAN SITE - Yerel Flask Sunucusu
ASLAN Technologies · Kimlik Dogrulama · Ses Studyosu · Aslan AI · Medya Studyosu

Vercel sunucusuz fonksiyonlariyla ayni cekirdek (core/) paketini, kalici veritabanini
ve guvenlik katmanini kullanir. Statik dosyalar yalnizca `public/` altindan sunulur.
"""
import base64
import io
import json
import mimetypes
import os
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, make_response, request, send_file, send_from_directory

from core import (
    ai_engine,
    api_utils,
    auth,
    blob_store,
    config as cfg,
    database,
    keepalive,
    media_engine,
    pipeline,
    privacy,
    quota as quota_mod,
    wa_bot,
    worker_client,
)

cfg.ensure_dirs()

PUBLIC_DIR = Path(cfg.BASE_DIR) / 'public'

app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = cfg.MAX_FILE_SIZE + 5 * 1024 * 1024

# ==================== YAPILANDIRMA ====================
DEFAULT_VOICE = cfg.DEFAULT_VOICE
MEDIA_TEMP_DIR = cfg.MEDIA_TEMP_DIR
MAX_FILE_SIZE = cfg.MAX_FILE_SIZE
AI_DAILY_LIMIT = cfg.AI_DAILY_LIMIT
IMAGE_MONTHLY_LIMIT = cfg.IMAGE_MONTHLY_LIMIT
SUPPORTED_MEDIA_OPERATIONS = media_engine.SUPPORTED_MEDIA_OPERATIONS

# Test uyumluluk takma adlari
_extract_aslan_action = ai_engine.extract_action
_strip_aslan_action = ai_engine.strip_action


# ==================== GUVENLIK & CORS KANCALARI ====================
@app.before_request
def _enforce_origin_policy():
    if request.path.startswith('/api/') and request.method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
        if api_utils.reject_disallowed_origin(request.headers):
            return jsonify({'error': 'Kaynak izni reddedildi.'}), 403


@app.after_request
def _apply_security_and_cors(response):
    is_api = request.path.startswith('/api/')
    for k, v in api_utils.security_headers(is_api=is_api).items():
        response.headers.setdefault(k, v)
    if is_api:
        for k, v in api_utils.cors_headers(request.headers, methods='GET, POST, OPTIONS').items():
            response.headers[k] = v
    else:
        response.headers.setdefault(
            'Permissions-Policy',
            'camera=(), geolocation=(), payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=(), microphone=(self)',
        )
        csp = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob: https:; media-src 'self' data: blob: https:; "
            "connect-src 'self' https:; font-src 'self' data:; "
            "worker-src 'self'; manifest-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if api_utils.is_https_request(request.headers):
            csp += '; upgrade-insecure-requests'
        response.headers['Content-Security-Policy'] = csp
        if request.path == '/sw.js':
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Service-Worker-Allowed'] = '/'
        elif request.path == '/manifest.json':
            response.headers['Content-Type'] = 'application/manifest+json; charset=utf-8'
            response.headers['Cache-Control'] = 'public, max-age=3600'
        elif request.path.startswith('/assets/'):
            response.headers['Cache-Control'] = 'public, max-age=3600, must-revalidate'
        elif (response.mimetype or '').startswith('text/html'):
            response.headers['Content-Disposition'] = 'inline'
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


# ==================== YARDIMCI FONKSIYONLAR ====================
def get_client_ip(req=None):
    return api_utils.client_ip((req or request).headers)


def get_device_id(req=None):
    return api_utils.request_device_id((req or request).headers)


def run_ffmpeg(args, input_files=None, output_file=None, timeout=None):
    return media_engine.run_ffmpeg(args, input_files=input_files,
                                   output_file=output_file, timeout=timeout)


def get_media_info(filepath):
    return media_engine.get_media_info(filepath)


def check_rate_limit(device_id):
    return quota_mod.consume_ai(device_id)


def get_gemini_api_key():
    return ai_engine.get_api_key()


def _error(message, status=400, **extra):
    payload = {'error': message}
    payload.update(extra)
    return jsonify(payload), status


def _resolve_media_paths(file_ids):
    return ai_engine.resolve_file_paths(file_ids)


def io_bytes(data):
    return io.BytesIO(data)


class InlineFile:
    """Base64 ile gelen dosyalari Flask dosya nesnesi gibi sunan sadelestirici."""

    def __init__(self, filename, path, mimetype):
        self.filename = filename
        self.mimetype = mimetype
        self._path = path
        try:
            self.content_length = os.path.getsize(path)
        except OSError:
            self.content_length = 0

    def read(self):
        try:
            with open(self._path, 'rb') as f:
                return f.read()
        except OSError:
            return b''


# ==================== ROTALAR ====================
@app.route('/')
def index():
    """Ana sayfa - frontend'i public/ dizininden servis et."""
    return send_from_directory(str(PUBLIC_DIR), 'index.html')


@app.route('/api/ping', methods=['GET', 'HEAD', 'POST'])
@app.route('/api/whatsapp/keep-alive', methods=['GET', 'HEAD', 'POST'])
def api_ping():
    """Kota tuketmeyen hafif uyanik tutma (keep-alive) ucu."""
    if request.method == 'HEAD':
        keepalive.touch_background_workers()
        return '', 200
    return jsonify(keepalive.ping_payload())


@app.route('/api/health', methods=['GET'])
def health():
    """Sistem saglik ve yetenek durumu."""
    try:
        keepalive.touch_background_workers()
    except Exception:
        pass
    return jsonify({
        'status': 'ok',
        'service': 'Aslan AI',
        'capabilities': {
            'auth': True,
            'tts': True,
            'free_ai': True,
            'ai': True,
            'image': True,
            'media': media_engine.ffmpeg_available(),
            'blob': blob_store.blob_enabled(),
        },
        'timestamp': int(time.time()),
    })


# ==================== KIMLIK DOGRULAMA (CEREZSIZ BEARER TOKEN) ====================
@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    try:
        ip_ok, ip_usage = quota_mod.check_and_consume_ip(
            get_client_ip(), 'auth_reg', cfg.IP_AUTH_HOURLY_LIMIT, 3600)
        if not ip_ok:
            return _error(
                'Cok fazla kayit denemesi yapildi. Lutfen %d saniye sonra tekrar deneyin.'
                % ip_usage.get('reset_in', 60), 429)
        data = request.get_json(force=True, silent=True) or {}
        ok, payload_or_err, status = auth.register_user(
            data.get('username'),
            data.get('password'),
            data.get('password_confirm'),
        )
        if not ok:
            return _error(payload_or_err, status)
        return jsonify({'success': True, **payload_or_err}), status
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    try:
        ip_ok, ip_usage = quota_mod.check_and_consume_ip(
            get_client_ip(), 'auth_login', cfg.IP_AUTH_HOURLY_LIMIT, 3600)
        if not ip_ok:
            return _error(
                'Cok fazla giris denemesi yapildi. Lutfen %d saniye sonra tekrar deneyin.'
                % ip_usage.get('reset_in', 60), 429)
        data = request.get_json(force=True, silent=True) or {}
        ok, payload_or_err, status = auth.login_user(
            data.get('username'),
            data.get('password'),
        )
        if not ok:
            return _error(payload_or_err, status)
        return jsonify({'success': True, **payload_or_err}), status
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    try:
        auth.logout_request(request.headers)
        return jsonify({'success': True})
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({'authenticated': False, 'error': 'Oturum bulunamadi veya suresi doldu.'}), 401
        return jsonify({
            'authenticated': True,
            'user': {'username': user['username']},
            'quota': database.user_quota_snapshot(user),
        })
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


# ==================== TTS (GIRIS ZORUNLU) ====================
@app.route('/api/tts', methods=['POST'])
def tts():
    """Metni sese donustur (giris zorunlu; edge-tts + gTTS yedegi)."""
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({
                'error': 'Aslan AI Ses Stüdyosu özelliğini kullanmak için giriş yapmalısınız.',
                'auth_required': True,
            }), 401

        data = request.get_json(force=True, silent=True) or {}
        text = str(data.get('text', '') or '').strip()
        voice = ai_engine.normalize_tts_voice(data.get('voice'))
        rate = ai_engine.normalize_tts_rate(data.get('rate'))
        pitch = ai_engine.normalize_tts_pitch(data.get('pitch'))

        if not text:
            return _error('Metin bos', 400)
        if len(text) > cfg.TTS_MAX_CHARS:
            return _error(
                'Karakter sinirina takildi: %d karakter. Lutfen metni %d karakterden kisa tutun.'
                % (len(text), cfg.TTS_MAX_CHARS),
                413,
            )

        ip_ok, ip_usage = quota_mod.check_and_consume_ip(
            get_client_ip(), 'tts', cfg.IP_TTS_HOURLY_LIMIT, 3600)
        if not ip_ok:
            return _error(
                'Cok fazla ses istegi gonderildi. %d saniye sonra tekrar deneyin.'
                % ip_usage.get('reset_in', 60),
                429,
            )

        audio, engine_name = ai_engine.synthesize_speech(text, voice, rate=rate, pitch=pitch)
        if not audio:
            return _error('Ses uretilemedi. Lutfen daha sonra tekrar deneyin.', 503)

        response = send_file(io_bytes(audio), mimetype='audio/mpeg',
                             as_attachment=False, download_name='speech.mp3')
        response.headers['X-Engine'] = 'Aslan AI'
        response.headers['Cache-Control'] = 'private, max-age=300'
        return response
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/stt', methods=['POST'])
def stt():
    """Ses kaydini metne donusturur (giris zorunlu)."""
    import binascii
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({
                'error': 'Sesli mesaj dönüştürme için giriş yapmalısınız.',
                'auth_required': True,
            }), 401

        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return _error('Gecersiz istek govdesi', 400)
        audio_b64 = str(data.get('audio_base64') or data.get('data') or '').strip()
        mime_type = str(data.get('mime_type') or 'audio/webm').strip().lower()
        lang = str(data.get('lang') or 'tr-TR').strip()

        if audio_b64.startswith('data:'):
            audio_b64 = audio_b64.split(',', 1)[1] if ',' in audio_b64 else ''
        if not audio_b64:
            return _error('Ses verisi gerekli.', 400)
        try:
            audio_bytes = base64.b64decode(audio_b64, validate=False)
        except (binascii.Error, ValueError):
            return _error('Gecersiz ses verisi.', 400)
        if not audio_bytes or len(audio_bytes) > cfg.MAX_JSON_BODY_BYTES:
            return _error('Ses dosyasi bos veya cok buyuk.', 400)

        ip_ok, ip_usage = quota_mod.check_and_consume_ip(
            get_client_ip(), 'stt', cfg.IP_STT_HOURLY_LIMIT, 3600)
        if not ip_ok:
            return _error(
                'Cok fazla ses donusturme istegi. %d saniye sonra tekrar deneyin.'
                % ip_usage.get('reset_in', 60),
                429,
            )

        text, err = ai_engine.transcribe_audio(audio_bytes, mime_type=mime_type, lang=lang)
        if not text:
            return _error(api_utils.safe_error_message(err, 'Ses metne donusturulemedi.'), 502)
        return jsonify({'success': True, 'text': text})
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


# ==================== ASLAN AI (GIRIS ZORUNLU + HESABA BAGLI KOTA) ====================
@app.route('/api/ai', methods=['POST'])
def ai():
    """Aslan AI Asistan - giris zorunlu, hesaba bagli DB kotasi, coklu AI zinciri."""
    temp_inline_paths = []
    created_outputs = []
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({
                'error': 'Aslan AI sohbetini kullanmak için giriş yapmalısınız.',
                'auth_required': True,
            }), 401

        message = ''
        files = []
        available_files = []
        context_metas = []
        history = []

        if request.content_type and 'multipart/form-data' in request.content_type:
            message = (request.form.get('message', '') or '').strip()
            files = request.files.getlist('files')
            try:
                available_files = json.loads(request.form.get('available_files', '[]'))
            except (ValueError, TypeError):
                available_files = []
            try:
                history = json.loads(request.form.get('history', '[]'))
            except (ValueError, TypeError):
                history = []
        else:
            data = request.get_json(force=True, silent=True) or {}
            if not isinstance(data, dict):
                return _error('Gecersiz istek govdesi', 400)
            message = str(data.get('message', '') or '').strip()
            available_files = data.get('available_files')
            if available_files is None:
                available_files = []
            elif not isinstance(available_files, list):
                return _error('Gecersiz available_files bicimi', 400)
            else:
                available_files = list(available_files)
            history = data.get('history')
            if not isinstance(history, list):
                history = []
            inline_req_files = data.get('files')
            if inline_req_files is None:
                inline_req_files = []
            elif not isinstance(inline_req_files, list):
                return _error('Gecersiz dosya listesi', 400)
            context_inline = data.get('context_files')
            if context_inline is None:
                context_inline = []
            elif not isinstance(context_inline, list):
                return _error('Gecersiz baglam dosya listesi', 400)
            if len(inline_req_files) + len(context_inline) > cfg.MAX_INLINE_FILES:
                return _error('Tek istekte en fazla %d dosya eklenebilir.' % cfg.MAX_INLINE_FILES, 400)
            for i, entry in enumerate(inline_req_files):
                path, meta = pipeline.decode_inline_file(entry, i)
                if path:
                    temp_inline_paths.append(path)
                    files.append(InlineFile(meta['name'], path, meta['mime_type']))
                    available_files.append(meta)
            for i, entry in enumerate(context_inline):
                path, meta = pipeline.decode_inline_file(entry, i + len(inline_req_files))
                if path:
                    temp_inline_paths.append(path)
                    context_metas.append(meta)

        if len(message) > cfg.MAX_MESSAGE_CHARS:
            return _error('Mesaj en fazla %d karakter olabilir.' % cfg.MAX_MESSAGE_CHARS, 400)

        if not message and not files:
            return _error('Mesaj veya dosya bos', 400)

        # ---- Hesaba bagli DB kota kontrolu ve WhatsApp bot tek-hak akisi ----
        uname_lower = user['username_lower']
        wa_kind, needs_credit = wa_bot.should_consume_credit(uname_lower, message)
        if needs_credit:
            allowed, usage = database.check_user_ai_quota(uname_lower)
            if not allowed:
                snap = database.user_quota_snapshot(user)
                return jsonify({
                    'error': ('Gunluk %d mesaj hakkiniz doldu. Hakkiniz %d saniye sonra yenilenir.'
                              % (AI_DAILY_LIMIT, usage.get('reset_in', 0))),
                    'remaining': 0,
                    'quota': snap,
                }), 429

            ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                get_client_ip(), 'ai', cfg.IP_AI_DAILY_LIMIT, quota_mod.DAY)
            if not ip_ok:
                return _error(
                    'Gunluk ag istek sinirina ulasildi. %d saniye sonra tekrar deneyin.'
                    % ip_usage.get('reset_in', 60),
                    429,
                )

        if wa_kind is not None:
            def _charge_once():
                if needs_credit:
                    database.consume_user_ai_quota(uname_lower)
            handled, wa_reply, wa_event, _ = wa_bot.handle_chat_turn(
                uname_lower, message, consume_credit_fn=_charge_once)
            if handled:
                fresh_user = database.get_user_by_username(uname_lower) or user
                snap = database.user_quota_snapshot(fresh_user)
                return jsonify({
                    'reply': wa_reply,
                    'remaining': snap['ai']['remaining'],
                    'quota': snap,
                    'media_result': None,
                    'image_result': None,
                    'generated_images': [],
                    'wa_event': wa_event,
                })

        parts = []
        for file in files:
            if not file or not file.filename:
                continue
            if file.content_length and file.content_length > MAX_FILE_SIZE:
                return _error('Dosya boyutu cok buyuk: %s (en fazla %d MB)'
                              % (file.filename, MAX_FILE_SIZE // (1024 * 1024)), 413)

            file_data = file.read()
            mime_type = (file.mimetype
                         or mimetypes.guess_type(file.filename)[0]
                         or 'application/octet-stream')

            if mime_type.startswith(('image/', 'video/', 'audio/')) or mime_type == 'application/pdf':
                parts.append({'inline_data': {
                    'mime_type': mime_type,
                    'data': base64.b64encode(file_data).decode('utf-8'),
                }})
            elif mime_type.startswith('text/') or mime_type in ('application/json', 'text/csv'):
                parts.append({'text': '[Dosya icerigi: %s]\n%s'
                              % (file.filename, file_data.decode('utf-8', errors='ignore'))})
            else:
                parts.append({'text': '[Desteklenmeyen dosya turu: %s (%s)]'
                              % (file.filename, mime_type)})

        all_available = available_files + context_metas
        reply, error = ai_engine.generate_reply(
            message, parts=parts, available_files=all_available,
            api_key=get_gemini_api_key(), history=history)
        if not reply:
            return _error(
                api_utils.safe_error_message(error, 'Yanit alinamadi'),
                502,
                remaining=usage['remaining'],
            )

        # Yalnizca basarili yanit sonrasi hesaptan 1 hak dusulur
        _, usage = database.consume_user_ai_quota(uname_lower)

        media_result = None
        image_result = None
        generated_images = []
        wa_event = None
        action = ai_engine.extract_action(reply)
        if action:
            clean_reply = ai_engine.strip_action(reply)
            media_result, generated_images, pipe_paths = ai_engine.run_action_pipeline(
                action, uploaded_files=all_available, username_lower=uname_lower)
            if isinstance(media_result, dict):
                keep_final = (
                    not media_result.get('data_url')
                    and str(media_result.get('download_url', '')).startswith('/api/media/download/')
                    and media_result.get('output_file')
                )
                keep_base = os.path.basename(str(media_result.get('output_file', ''))) if keep_final else ''
                created_outputs.extend([p for p in pipe_paths if os.path.basename(p) != keep_base])
                if media_result.get('operation') == 'generate_image' and media_result.get('success'):
                    image_result = media_result
                elif media_result.get('operation') == 'wa_bot_update' and media_result.get('success'):
                    wa_event = {'type': 'config_updated', 'config_summary': media_result.get('config_summary', {})}
                    media_result = None
                if media_result and media_result.get('success'):
                    reply = clean_reply or 'Islem tamamlandi.'
                elif wa_event:
                    reply = clean_reply or 'WhatsApp bot ayarları güncellendi.'
            else:
                created_outputs.extend(pipe_paths)

        fresh_user = database.get_user_by_username(uname_lower) or user
        snap = database.user_quota_snapshot(fresh_user)
        return jsonify({
            'reply': reply,
            'remaining': usage['remaining'],
            'quota': snap,
            'media_result': media_result,
            'image_result': image_result,
            'generated_images': generated_images,
            'wa_event': wa_event,
        })
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)
    finally:
        pipeline.cleanup_files(temp_inline_paths + created_outputs)


@app.route('/api/image', methods=['POST'])
def image_generate():
    """Metinden gorsel uretir (giris zorunlu + hesaba bagli DB kotasi)."""
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({
                'error': 'Görsel üretimi için giriş yapmalısınız.',
                'auth_required': True,
            }), 401

        data = request.get_json(force=True, silent=True) or {}
        prompt = str(data.get('prompt', '') or '').strip()

        if not prompt:
            return _error('Gorsel icin aciklama gerekli', 400)
        if len(prompt) > cfg.MAX_PROMPT_CHARS:
            return _error('Aciklama en fazla %d karakter olabilir' % cfg.MAX_PROMPT_CHARS, 400)

        ip_ok, ip_usage = quota_mod.check_and_consume_ip(
            get_client_ip(), 'image', cfg.IP_IMAGE_DAILY_LIMIT, quota_mod.DAY)
        if not ip_ok:
            return _error(
                'Gunluk ag gorsel sinirina ulasildi. %d saniye sonra tekrar deneyin.'
                % ip_usage.get('reset_in', 60),
                429,
            )

        uname_lower = user['username_lower']
        ok, result = ai_engine.generate_image(prompt, username_lower=uname_lower)
        fresh_user = database.get_user_by_username(uname_lower) or user
        snap = database.user_quota_snapshot(fresh_user)

        if not ok:
            status = 429 if result.get('quota_exceeded') else 502
            return jsonify({
                'error': api_utils.safe_error_message(result.get('error'), 'Gorsel uretilemedi'),
                'quota': snap,
            }), status

        return jsonify({
            'success': True,
            'image_base64': result['image_base64'],
            'mime_type': result['mime_type'],
            'prompt': result['prompt'],
            'engine': 'Aslan AI',
            'quota': snap,
        })
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/quota', methods=['GET', 'POST'])
def quota_status():
    """Hesaba bagli kota durumunu dondurur (cerezsiz)."""
    try:
        user, _ = auth.authenticate_request(request.headers)
        if user:
            snap = database.user_quota_snapshot(user)
            return jsonify({'authenticated': True, 'user': {'username': user['username']}, **snap})
        return jsonify({
            'authenticated': False,
            'account_bound': False,
            'ai': {'limit': cfg.AI_DAILY_LIMIT, 'used': 0, 'remaining': cfg.AI_DAILY_LIMIT},
            'image': {
                'limit': cfg.IMAGE_MONTHLY_LIMIT,
                'used': 0,
                'remaining': cfg.IMAGE_MONTHLY_LIMIT,
                'monthly_limit': cfg.IMAGE_MONTHLY_LIMIT,
                'monthly_used': 0,
                'monthly_remaining': cfg.IMAGE_MONTHLY_LIMIT,
                'allowed': True,
            },
        })
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/cookie-consent', methods=['POST'])
def cookie_consent():
    """Cerez onayi ve kategori tercihlerini kalici olarak saklar (kabul sonrasi kilitlenir)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return _error('Gecersiz istek govdesi', 400)
        consent = str(data.get('consent', '') or '').strip()
        if consent not in ('accepted', 'rejected', 'custom'):
            return _error('Gecersiz onay degeri', 400)

        user, _ = auth.authenticate_request(request.headers)
        subject_id = ('user:' + user['username_lower']) if user else get_device_id()
        existing = database.get_consent_db(subject_id) or privacy.get_consent(subject_id)
        if existing.get('consent') == 'accepted':
            if consent != 'accepted':
                return jsonify({
                    'error': 'Çerez onayı daha önce kabul edildiği için değiştirilemez veya sıfırlanamaz.',
                    'consent': 'accepted',
                    'locked': True,
                }), 409
            existing_prefs = existing.get('preferences') or {
                'necessary': True, 'functional': True, 'analytics': True
            }
            return jsonify({
                'success': True,
                'consent': 'accepted',
                'preferences': existing_prefs,
                'device_id': subject_id,
                'timestamp': existing.get('timestamp'),
                'locked': True,
            })

        prefs = data.get('preferences') if isinstance(data.get('preferences'), dict) else {}
        clean_prefs = {
            'necessary': True,
            'functional': bool(prefs.get('functional', consent == 'accepted')),
            'analytics': bool(prefs.get('analytics', False)),
        }
        ip = get_client_ip()
        stored = database.store_consent_db(subject_id, consent, clean_prefs, ip=ip)
        privacy.store_consent(subject_id, consent, ip=ip)
        return jsonify({
            'success': True,
            'consent': consent,
            'preferences': clean_prefs,
            'device_id': subject_id,
            'timestamp': stored.get('timestamp'),
            'locked': (consent == 'accepted'),
        })
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/cookie-consent', methods=['GET'])
def cookie_consent_get():
    """Saklanmis cerez onayi tercihini dondurur."""
    try:
        user, _ = auth.authenticate_request(request.headers)
        subject_id = ('user:' + user['username_lower']) if user else get_device_id()
        stored = database.get_consent_db(subject_id) or privacy.get_consent(subject_id)
        consent_val = stored.get('consent', 'unknown')
        return jsonify({
            'consent': consent_val,
            'preferences': stored.get('preferences', {}),
            'device_id': subject_id,
            'timestamp': stored.get('timestamp'),
            'locked': (consent_val == 'accepted'),
        })
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


# ==================== Medya Isleme Uc noktalari ====================
@app.route('/api/media/upload', methods=['POST'])
def media_upload():
    """Islenecek medya dosyalarini gecici olarak yukler."""
    try:
        files = request.files.getlist('files')
        if not files:
            return _error('Dosya yuklenmedi', 400)

        uploaded = []
        for file in files:
            if not file or not file.filename:
                continue
            if file.content_length and file.content_length > MAX_FILE_SIZE:
                return _error('Dosya cok buyuk: %s' % os.path.basename(file.filename), 413)

            raw_suffix = Path(os.path.basename(file.filename)).suffix
            ext = api_utils.safe_extension(file.filename)
            if raw_suffix and not ext:
                return _error('Desteklenmeyen dosya uzantisi: %s' % raw_suffix, 400)

            filename = '%s%s' % (uuid.uuid4().hex, ext)
            filepath = Path(MEDIA_TEMP_DIR) / filename
            file.save(str(filepath))

            uploaded.append({
                'id': filename,
                'original_name': os.path.basename(file.filename),
                'path': str(filepath),
                'size': file.content_length or (filepath.stat().st_size if filepath.exists() else 0),
                'mime_type': file.mimetype or mimetypes.guess_type(file.filename)[0],
                'info': media_engine.get_media_info(str(filepath)),
            })

        if not uploaded:
            return _error('Gecerli dosya bulunamadi', 400)
        return jsonify({'files': uploaded})
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/media/process', methods=['POST'])
def media_process():
    """Secilen medya islemini arka planda calistirir."""
    temp_paths = []
    try:
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return _error('Gecersiz istek govdesi', 400)
        operation = data.get('operation')
        file_ids = data.get('files')
        params = data.get('params')
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            return _error('Gecersiz parametre bicimi', 400)
        if not isinstance(file_ids, list) or not file_ids:
            return _error('Islem ve dosya ID\'leri gerekli', 400)
        if len(file_ids) > cfg.MAX_INLINE_FILES:
            return _error('Tek istekte en fazla %d dosya islenebilir.' % cfg.MAX_INLINE_FILES, 400)

        if not operation:
            return _error('Islem ve dosya ID\'leri gerekli', 400)
        if operation not in SUPPORTED_MEDIA_OPERATIONS:
            return _error('Bilinmeyen islem: %s' % operation, 400)

        ip_ok, ip_usage = quota_mod.check_and_consume_ip(
            get_client_ip(), 'media', cfg.IP_MEDIA_HOURLY_LIMIT, 3600)
        if not ip_ok:
            return _error(
                'Saatlik medya isleme sinirina ulasildi. %d saniye sonra tekrar deneyin.'
                % ip_usage.get('reset_in', 60),
                429,
            )

        input_files = []
        if file_ids and isinstance(file_ids[0], dict):
            if worker_client.is_enabled():
                w_res = worker_client.call_media_process(operation, file_ids, params)
                if not w_res.get('success'):
                    return _error(api_utils.safe_error_message(w_res.get('error'), 'Aslan AI medya islemi tamamlanamadi.'), 502)
                return jsonify(w_res)
            for i, entry in enumerate(file_ids):
                path, _ = pipeline.decode_inline_file(entry, i)
                if path:
                    input_files.append(path)
                    temp_paths.append(path)
            if not input_files:
                return _error('Gecerli dosya gonderilmedi', 400)
        else:
            input_files = _resolve_media_paths(file_ids)
            if len(input_files) != len(file_ids):
                return _error('Dosya bulunamadi', 404)

        output_path = media_engine.new_output_path(operation=operation, params=params)
        ok, result = media_engine.execute_media_operation(operation, input_files,
                                                           output_path, params)
        if not ok:
            return _error('Islem basarisiz: %s' % api_utils.safe_error_message(result), 500)

        descriptor = media_engine.result_descriptor(operation, result)
        try:
            descriptor['summary'] = media_engine.media_summary(result)
        except Exception:
            pass
        try:
            size = Path(result).stat().st_size
            if size <= pipeline.MAX_INLINE_RESULT_BYTES:
                payload = base64.b64encode(Path(result).read_bytes()).decode('ascii')
                descriptor['data_url'] = 'data:%s;base64,%s' % (
                    descriptor.get('mime_type', 'application/octet-stream'), payload)
            else:
                uploaded = blob_store.upload_file(result)
                if uploaded and uploaded.get('url'):
                    descriptor['download_url'] = uploaded['url']
        except Exception:
            pass
        return jsonify(descriptor)
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)
    finally:
        pipeline.cleanup_files(temp_paths)


@app.route('/api/media/analyze', methods=['POST'])
def media_analyze():
    try:
        data = request.get_json(force=True, silent=True) or {}
        file_ids = data.get('files', []) or []
        paths = _resolve_media_paths(file_ids)
        if not paths:
            return _error('Dosya bulunamadi', 404)
        return jsonify({'analysis': [media_engine.media_summary(p) for p in paths]})
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/media/social', methods=['POST'])
def media_social_download():
    """TikTok / YouTube / Facebook / Instagram baglantilarindan video indirir (SSRF korumali)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        url = str(data.get('url', '') or '').strip()
        valid, clean_or_err = api_utils.validate_social_url(url)
        if not valid:
            return _error(clean_or_err, 400)

        ip_ok, ip_usage = quota_mod.check_and_consume_ip(
            get_client_ip(), 'social', cfg.IP_SOCIAL_DAILY_LIMIT, quota_mod.DAY)
        if not ip_ok:
            return _error(
                'Gunluk video indirme sinirina ulasildi. %d saniye sonra tekrar deneyin.'
                % ip_usage.get('reset_in', 60),
                429,
            )

        params = data.get('params') if isinstance(data.get('params'), dict) else {}
        result = ai_engine.download_social_media(clean_or_err, params)
        if not result.get('success'):
            return jsonify(result), 502
        return jsonify(result)
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/media/download/<path:filename>')
def media_download(filename):
    try:
        safe_name = os.path.basename(filename)
        filepath = Path(MEDIA_TEMP_DIR) / safe_name
        if not filepath.exists() or not filepath.is_file():
            return _error('Dosya bulunamadi', 404)

        download_name = safe_name
        if not Path(safe_name).suffix:
            download_name = 'aslan_%s.bin' % safe_name

        return send_file(str(filepath), as_attachment=True, download_name=download_name)
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/media/list')
def media_list():
    try:
        files = []
        for f in Path(MEDIA_TEMP_DIR).glob('*'):
            if f.is_file():
                stat = f.stat()
                files.append({'name': f.name, 'size': stat.st_size,
                              'modified': stat.st_mtime})
        return jsonify({'files': files})
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/media/cleanup', methods=['POST'])
def media_cleanup():
    try:
        data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return _error('Gecersiz istek govdesi', 400)
        raw_age = data.get('max_age_hours', 24)
        try:
            max_age = float(raw_age)
        except (TypeError, ValueError):
            return _error('Gecersiz max_age_hours degeri', 400)
        if max_age < 0 or max_age > 8760 or max_age != max_age:
            return _error('max_age_hours 0 ile 8760 arasinda olmalidir', 400)
        cutoff = time.time() - (max_age * 3600)

        removed = []
        for f in Path(MEDIA_TEMP_DIR).glob('*'):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed.append(f.name)

        return jsonify({'removed': removed, 'count': len(removed)})
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/wa/status', methods=['GET'])
def wa_status():
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({'error': 'Bu özellik için giriş yapmalısınız.', 'auth_required': True}), 401
        return jsonify(wa_bot.get_status_snapshot(user['username_lower']))
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/wa/confirm-connected', methods=['POST'])
def wa_confirm_connected():
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({'error': 'Bu özellik için giriş yapmalısınız.', 'auth_required': True}), 401
        res = wa_bot.confirm_connected(user['username_lower'])
        return jsonify(res), (200 if res.get('success') else 400)
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/wa/stop', methods=['POST'])
def wa_stop():
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({'error': 'Bu özellik için giriş yapmalısınız.', 'auth_required': True}), 401
        data = request.get_json(force=True, silent=True) or {}
        res = wa_bot.stop_user_bot(user['username_lower'], logout=bool(data.get('logout')))
        return jsonify(res)
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/wa/wake', methods=['POST'])
def wa_wake():
    try:
        user, _ = auth.authenticate_request(request.headers)
        if not user:
            return jsonify({'error': 'Bu özellik için giriş yapmalısınız.', 'auth_required': True}), 401
        data = request.get_json(force=True, silent=True) or {}
        res = wa_bot.wake_user_bot(user['username_lower'], force=bool(data.get('force', True)))
        return jsonify(res), (200 if res.get('success') else 502)
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/<path:_any>', methods=['OPTIONS'])
def options(_any):
    return '', 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    print('=' * 54)
    print('  ASLAN SITE Sunucusu Baslatiliyor...')
    print('  http://localhost:%d' % port)
    print('  Statik dizin: %s' % PUBLIC_DIR)
    print('  Veritabani dizini: %s' % database._data_dir())
    print('  Medya gecici dizini: %s' % MEDIA_TEMP_DIR)
    print('  Medya motoru: %s' % (media_engine.ffmpeg_path() or 'BULUNAMADI'))
    print('=' * 54)
    app.run(host='0.0.0.0', port=port, debug=debug)
