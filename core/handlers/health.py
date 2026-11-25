"""
Vercel sunucusuz fonksiyonu: Sistem saglik ve yetenek durumu.
"""
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ai_engine, api_utils, blob_store, keepalive, media_engine


class handler(BaseHTTPRequestHandler):
    """Saglik kontrolu ucu (GET /api/health)."""

    def do_GET(self):
        try:
            try:
                keepalive.touch_background_workers()
            except Exception:
                pass
            payload = {
                'status': 'ok',
                'service': 'Aslan AI',
                'capabilities': {
                    'auth': True,
                    'tts': True,
                    'free_ai': True,
                    'ai': bool(ai_engine.get_api_key() or True),
                    'image': True,
                    'media': media_engine.ffmpeg_available(),
                    'blob': blob_store.blob_enabled(),
                },
                'timestamp': int(time.time()),
            }
            api_utils.send_json(self, 200, payload, methods='GET, OPTIONS')
        except Exception as e:
            api_utils.send_json(
                self, 500, {'status': 'error', 'error': api_utils.safe_error_message(e)},
                methods='GET, OPTIONS',
            )

    def do_OPTIONS(self):
        api_utils.send_options(self, methods='GET, OPTIONS')

    def log_message(self, format, *args):
        pass
