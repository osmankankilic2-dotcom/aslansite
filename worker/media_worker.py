#!/usr/bin/env python3
"""
ASLAN Technologies - Bagimsiz Harici FFmpeg & yt-dlp Worker Sunucusu.

Vercel Serverless 60s/4.5MB limitlerinin otesindeki uzun video/FFmpeg/yt-dlp
islemleri icin Render, Railway, Fly.io veya VPS uzerinde calistirilabilir.
Ana uygulamada `MEDIA_WORKER_URL` ve `MEDIA_WORKER_TOKEN` tanimlanarak baglanir.
"""
import hmac
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ai_engine, api_utils, config as cfg, media_engine, pipeline

cfg.ensure_dirs()


def _authorized(headers) -> bool:
    expected = (os.environ.get('MEDIA_WORKER_TOKEN') or cfg.MEDIA_WORKER_TOKEN or '').strip()
    if not expected:
        return True
    provided = api_utils._header(headers, 'X-Worker-Token').strip()
    return bool(provided) and hmac.compare_digest(provided, expected)


class WorkerHandler(BaseHTTPRequestHandler):
    """Harici FFmpeg & yt-dlp worker HTTP isleyicisi."""

    def do_GET(self):
        path = self.path.split('?', 1)[0].rstrip('/')
        if path == '/ping':
            api_utils.send_json(self, 200, {'ok': True, 'ts': int(time.time())})
            return
        if path == '/health':
            api_utils.send_json(self, 200, {
                'status': 'ok',
                'ffmpeg': media_engine.ffmpeg_available(),
            })
            return
        api_utils.send_json(self, 404, {'error': 'Not found'})

    def do_HEAD(self):
        path = self.path.split('?', 1)[0].rstrip('/')
        if path == '/ping':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not _authorized(self.headers):
            api_utils.send_json(self, 401, {'error': 'Yetkisiz worker istegi'})
            return

        path = self.path.split('?', 1)[0].rstrip('/')
        raw, err_status, err_msg = api_utils.read_body_limited(
            self.rfile, self.headers, max_bytes=cfg.MAX_FILE_SIZE)
        if err_status:
            api_utils.send_json(self, err_status, {'error': err_msg})
            return
        data = api_utils.json_body(raw, {})

        if path == '/worker/media/process':
            self._handle_media(data)
            return
        if path == '/worker/media/social':
            self._handle_social(data)
            return
        api_utils.send_json(self, 404, {'error': 'Bilinmeyen worker yolu'})

    def _handle_media(self, data: dict):
        paths = []
        result_path = None
        try:
            op = str(data.get('operation') or '').strip()
            params = data.get('params') if isinstance(data.get('params'), dict) else {}
            inline = data.get('files') if isinstance(data.get('files'), list) else []
            if op not in media_engine.SUPPORTED_MEDIA_OPERATIONS:
                api_utils.send_json(self, 400, {'success': False, 'error': 'Bilinmeyen islem'})
                return
            for i, entry in enumerate(inline):
                p, _ = pipeline.decode_inline_file(entry, i)
                if p:
                    paths.append(p)
            if not paths:
                api_utils.send_json(self, 400, {'success': False, 'error': 'Gecerli dosya yok'})
                return
            out_path = media_engine.new_output_path(operation=op, params=params)
            ok, result = media_engine.execute_media_operation(op, paths, out_path, params)
            if not ok:
                api_utils.send_json(self, 500, {'success': False, 'error': api_utils.safe_error_message(result)})
                return
            result_path = str(result)
            desc = media_engine.result_descriptor(op, result)
            try:
                desc['summary'] = media_engine.media_summary(result)
            except Exception:
                pass
            ai_engine.attach_download(desc, result_path)
            api_utils.send_json(self, 200, desc)
        finally:
            if result_path:
                paths.append(result_path)
            pipeline.cleanup_files(paths)

    def _handle_social(self, data: dict):
        url = str(data.get('url') or '').strip()
        params = data.get('params') if isinstance(data.get('params'), dict) else {}
        res = ai_engine.download_social_media(url, params)
        status = 200 if res.get('success') else 502
        if isinstance(res, dict) and res.get('output_file'):
            p = os.path.join(cfg.MEDIA_TEMP_DIR, os.path.basename(str(res['output_file'])))
            pipeline.cleanup_files([p])
        api_utils.send_json(self, status, res)

    def log_message(self, format, *args):
        pass


def _start_keepalive_loop():
    if os.environ.get('KEEPALIVE_DISABLED') == '1':
        return
    target = (
        os.environ.get('KEEPALIVE_TARGET_URL')
        or os.environ.get('VERCEL_KEEPALIVE_URL')
        or 'https://aslansite.vercel.app/api/ping'
    ).strip()
    if not target:
        return

    def _run():
        while True:
            time.sleep(60)
            try:
                req = urllib.request.Request(target, headers={'User-Agent': 'Aslan-AI-KeepAlive/1.0'}, method='GET')
                with urllib.request.urlopen(req, timeout=8):
                    pass
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


if __name__ == '__main__':
    port = int(os.environ.get('WORKER_PORT', os.environ.get('PORT', 5055)))
    _start_keepalive_loop()
    srv = ThreadingHTTPServer(('0.0.0.0', port), WorkerHandler)
    print('ASLAN Media Worker listening on :%d' % port)
    srv.serve_forever()
