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
    media_engine,
    pipeline,
    privacy,
    quota as quota_mod,
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
IMAGE_DAILY_LIMIT = cfg.IMAGE_DAILY_LIMIT
IMAGE_WEEKLY_LIMIT = cfg.IMAGE_WEEKLY_LIMIT
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
            'camera=(), geolocation=(), payment=(), usb=(), microphone=(self)',
        )
        response.headers.setdefault(
            'Content-Security-Policy',
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; media-src 'self' data: blob: https:; "
            "connect-src 'self' https:; font-src 'self' data:; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
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


@app.route('/api/health', methods=['GET'])
def health():
    """Sistem saglik ve yetenek durumu."""
    return jsonify({
        'status': 'ok',
        'service': 'ASLAN Technologies',
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
                'error': 'Metinden sese (TTS) özelliğini kullanmak için giriş yapmalısınız.',
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
        response.headers['X-Engine'] = engine_name or 'unknown'
        response.headers['Cache-Control'] = 'private, max-age=300'
        return response
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


# ==================== ASLAN AI (GIRIS ZORUNLU + HESABA BAGLI KOTA) ====================
@app.route('/api/ai', methods=['POST'])
def ai():
    """Aslan AI Asistan - giris zorunlu, hesaba bagli DB kotasi, coklu AI zinciri."""
    temp_inline_paths = []
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
            message = str(data.get('message', '') or '').strip()
            available_files = list(data.get('available_files') or [])
            history = data.get('history') or []
            for i, entry in enumerate(data.get('files') or []):
                path, meta = pipeline.decode_inline_file(entry, i)
                if path:
                    temp_inline_paths.append(path)
                    files.append(InlineFile(meta['name'], path, meta['mime_type']))
                    available_files.append(meta)

        if len(message) > cfg.MAX_MESSAGE_CHARS:
            return _error('Mesaj en fazla %d karakter olabilir.' % cfg.MAX_MESSAGE_CHARS, 400)

        if not message and not files:
            return _error('Mesaj veya dosya bos', 400)

        # ---- Hesaba bagli DB kota kontrolu (harcamadan once kontrol) ----
        uname_lower = user['username_lower']
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

        reply, error = ai_engine.generate_reply(
            message, parts=parts, available_files=available_files,
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
        action = ai_engine.extract_action(reply)
        if action:
            clean_reply = ai_engine.strip_action(reply)
            media_result = ai_engine.run_action(
                action, uploaded_files=available_files, username_lower=uname_lower)
            if isinstance(media_result, dict):
                if media_result.get('operation') == 'generate_image' and media_result.get('success'):
                    image_result = media_result
                if media_result.get('success'):
                    reply = clean_reply or 'Islem tamamlandi.'

        fresh_user = database.get_user_by_username(uname_lower) or user
        snap = database.user_quota_snapshot(fresh_user)
        return jsonify({
            'reply': reply,
            'remaining': usage['remaining'],
            'quota': snap,
            'media_result': media_result,
            'image_result': image_result,
        })
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)
    finally:
        pipeline.cleanup_files(temp_inline_paths)


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
            'engine': result.get('engine', 'gemini'),
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
                'daily_limit': cfg.IMAGE_DAILY_LIMIT,
                'weekly_limit': cfg.IMAGE_WEEKLY_LIMIT,
                'monthly_limit': cfg.IMAGE_MONTHLY_LIMIT,
                'daily_remaining': cfg.IMAGE_DAILY_LIMIT,
                'weekly_remaining': cfg.IMAGE_WEEKLY_LIMIT,
                'monthly_remaining': cfg.IMAGE_MONTHLY_LIMIT,
            },
        })
    except Exception as e:
        return _error(api_utils.safe_error_message(e), 500)


@app.route('/api/cookie-consent', methods=['POST'])
def cookie_consent():
    """Cerez onayi ve kategori tercihlerini kalici olarak saklar."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        consent = str(data.get('consent', '') or '').strip()
        if consent not in ('accepted', 'rejected', 'custom'):
            return _error('Gecersiz onay degeri', 400)

        prefs = data.get('preferences') if isinstance(data.get('preferences'), dict) else {}
        clean_prefs = {
            'necessary': True,
            'functional': bool(prefs.get('functional', consent == 'accepted')),
            'analytics': bool(prefs.get('analytics', False)),
        }
        user, _ = auth.authenticate_request(request.headers)
        subject_id = ('user:' + user['username_lower']) if user else get_device_id()
        ip = get_client_ip()
        stored = database.store_consent_db(subject_id, consent, clean_prefs, ip=ip)
        privacy.store_consent(subject_id, consent, ip=ip)
        return jsonify({
            'success': True,
            'consent': consent,
            'preferences': clean_prefs,
            'device_id': subject_id,
            'timestamp': stored.get('timestamp'),
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
        return jsonify({
            'consent': stored.get('consent', 'unknown'),
            'preferences': stored.get('preferences', {}),
            'device_id': subject_id,
            'timestamp': stored.get('timestamp'),
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
        operation = data.get('operation')
        file_ids = data.get('files', [])
        params = data.get('params', {}) or {}

        if not operation or not file_ids:
            return _error('Islem ve dosya ID\'leri gerekli', 400)
        if operation not in SUPPORTED_MEDIA_OPERATIONS:
            return _error('Bilinmeyen islem: %s' % operation, 400)

        input_files = []
        if file_ids and isinstance(file_ids[0], dict):
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
        max_age = data.get('max_age_hours', 24)
        cutoff = time.time() - (max_age * 3600)

        removed = []
        for f in Path(MEDIA_TEMP_DIR).glob('*'):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed.append(f.name)

        return jsonify({'removed': removed, 'count': len(removed)})
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
