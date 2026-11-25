"""
Vercel sunucusuz fonksiyonu: Metinden sese (TTS).

Zorunlu kullanici girisi kontrolu (401), edge-tts birincil motor (hiz ve ton destekli),
gTTS yedek motordur.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ai_engine, api_utils, auth, config as cfg, quota as quota_mod

EDGE_TTS_MAX_CHARS = cfg.TTS_MAX_CHARS


class handler(BaseHTTPRequestHandler):
    """Metni sese donusturur (giris zorunlu)."""

    def _send_audio(self, audio_bytes, engine_name):
        self.send_response(200)
        headers_map = {
            'Content-Type': 'audio/mpeg',
            'Content-Length': str(len(audio_bytes)),
            'X-Engine': 'Aslan AI',
            'Cache-Control': 'private, max-age=300',
        }
        headers_map.update(api_utils.security_headers(is_api=False))
        headers_map.update(api_utils.cors_headers(self.headers, methods='POST, OPTIONS'))
        for k, v in headers_map.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(audio_bytes)

    def do_POST(self):
        try:
            if api_utils.reject_disallowed_origin(self.headers):
                api_utils.send_json(self, 403, {'error': 'Kaynak izni reddedildi.'},
                                    methods='POST, OPTIONS')
                return

            user, _ = auth.authenticate_request(self.headers)
            if not user:
                api_utils.send_json(self, 401, {
                    'error': 'Aslan AI Ses Stüdyosu özelliğini kullanmak için giriş yapmalısınız.',
                    'auth_required': True,
                }, methods='POST, OPTIONS')
                return

            raw, err_status, err_msg = api_utils.read_body_limited(
                self.rfile, self.headers, max_bytes=cfg.MAX_JSON_BODY_BYTES)
            if err_status:
                api_utils.send_json(self, err_status, {'error': err_msg}, methods='POST, OPTIONS')
                return

            data = api_utils.json_body(raw, None)
            if data is None:
                api_utils.send_json(self, 400, {'error': 'Gecersiz JSON formati'},
                                    methods='POST, OPTIONS')
                return

            text = str(data.get('text', '') or '').strip()
            voice = ai_engine.normalize_tts_voice(data.get('voice'))
            rate = ai_engine.normalize_tts_rate(data.get('rate'))
            pitch = ai_engine.normalize_tts_pitch(data.get('pitch'))

            if not text:
                api_utils.send_json(self, 400, {'error': 'Metin bos'}, methods='POST, OPTIONS')
                return

            if len(text) > EDGE_TTS_MAX_CHARS:
                api_utils.send_json(
                    self, 413,
                    {'error': 'Karakter sinirina takildi: %d karakter. Lutfen metni %d karakterden kisa tutun.'
                              % (len(text), EDGE_TTS_MAX_CHARS)},
                    methods='POST, OPTIONS',
                )
                return

            ip = api_utils.client_ip(self.headers)
            ip_ok, ip_usage = quota_mod.check_and_consume_ip(
                ip, 'tts', cfg.IP_TTS_HOURLY_LIMIT, 3600)
            if not ip_ok:
                api_utils.send_json(
                    self, 429,
                    {'error': 'Cok fazla ses istegi gonderildi. Lutfen %d saniye sonra tekrar deneyin.'
                              % ip_usage.get('reset_in', 60)},
                    methods='POST, OPTIONS',
                )
                return

            audio, engine_name = ai_engine.synthesize_speech(text, voice, rate=rate, pitch=pitch)
            if not audio:
                api_utils.send_json(
                    self, 503,
                    {'error': 'Ses uretilemedi. Lutfen daha sonra tekrar deneyin.'},
                    methods='POST, OPTIONS',
                )
                return

            self._send_audio(audio, engine_name)
        except Exception as e:
            api_utils.send_json(
                self, 500,
                {'error': 'Beklenmedik hata: %s' % api_utils.safe_error_message(e)},
                methods='POST, OPTIONS',
            )

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='POST, OPTIONS')

    def log_message(self, format, *args):
        pass
